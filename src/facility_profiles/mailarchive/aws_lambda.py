"""The collector as an AWS Lambda, scheduled every 15 minutes.

One invocation is one pass of :func:`facility_profiles.mailarchive.collector.run`. The Gmail
service-account key comes from Secrets Manager and stays in memory. Environment:

    LIDL_MAIL_BUCKET      the archive bucket (required)
    LIDL_MAIL_PREFIX      key prefix inside it (default none)
    LIDL_GMAIL_SECRET     Secrets Manager id of the service account's JSON key (required)
    LIDL_GMAIL_USER       the group member whose mailbox is read (required)
    LIDL_GROUP            the group address (default lidl@circledelivers.com)
    LIDL_WINDOW_DAYS      how many days back each pass lists (default 3)
    LIDL_MAX_MESSAGES     per-pass cap (default 300)
    LIDL_DESKS            extra appointment-desk addresses, comma-separated
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from facility_profiles.mailarchive import filters
from facility_profiles.mailarchive.collector import DEFAULT_GROUP, run
from facility_profiles.mailarchive.gmail import Delegated
from facility_profiles.mailarchive.store import Store

RESERVE_S = 30  # kept back from the timeout so a pass stopped by its deadline still writes state


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:  # pragma: no cover
    """Lambda entry point."""
    del event
    started = time.monotonic()
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 600.0
    env = os.environ
    import boto3  # noqa: PLC0415 - provided by the Lambda runtime

    store = Store(
        env["LIDL_MAIL_BUCKET"], env.get("LIDL_MAIL_PREFIX", ""), client=boto3.client("s3")
    )
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=env["LIDL_GMAIL_SECRET"])
    info = json.loads(secret["SecretString"])
    if not info.get("client_email") or not info.get("private_key"):
        msg = f"secret {env['LIDL_GMAIL_SECRET']} must hold the service account's JSON key"
        raise ValueError(msg)
    gmail = Delegated(info, subject=env["LIDL_GMAIL_USER"])
    extra = {d.strip().lower() for d in env.get("LIDL_DESKS", "").split(",") if d.strip()}
    st = run(
        gmail,
        store,
        mailbox=env["LIDL_GMAIL_USER"],
        group=env.get("LIDL_GROUP", DEFAULT_GROUP),
        days=int(env.get("LIDL_WINDOW_DAYS", "3")),
        desks=filters.DEFAULT_DESKS | extra,
        max_messages=int(env.get("LIDL_MAX_MESSAGES", "300")),
        deadline=started + remaining - RESERVE_S,
    )
    summary = {
        "collect": st.line(),
        "gmail_calls": gmail.calls,
        "seconds": round(time.monotonic() - started),
    }
    print(json.dumps(summary))  # noqa: T201 - the CloudWatch log line
    if st.error:
        # Raised after state is saved, so the Errors metric sees a bad run without it costing the
        # messages that did go through.
        raise RuntimeError(f"collector stopped on an error: {st.error}")
    return summary
