"""Read vendor replies from a mail pull through the reply classifier and validator, store untouched.

Shows, for every non-internal message in a ``messages.jsonl`` (a real pull or the fixture under
``tests/fixtures``), what the model read, what the validator kept, and why anything was dropped.
Nothing is matched to a case and nothing is written, so it is safe on any pull.

    python scripts/replay_reply_classifier.py tests/fixtures/lidl_morgan_foods_thread.jsonl \
        --po 115802102660 --po 115802102661 --requested "2026-10-05 09:00"

    python scripts/replay_reply_classifier.py data/lidl-mail/messages.jsonl --sender morganfoods.com

``--fake`` skips the model and only shows how each message splits into its own words, the quoted
history and the cleaned text the validator sees (no API cost).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from facility_profiles.booking.classify import (
    OpenRouterReplyClassifier,
    ReplyContext,
    clean_mail_text,
    validate_classification,
)
from facility_profiles.booking.mail import load_messages_jsonl
from facility_profiles.config import get_settings

SHOW = ("status", "pickup_date", "pickup_time", "pickup_time_end", "pickup_number", "question")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("file", type=Path, help="messages.jsonl from scripts/lidl_mail_patterns.py")
    parser.add_argument("--po", action="append", default=[], help="PO number(s) of the request")
    parser.add_argument("--requested", default=None, help='requested slot, e.g. "2026-10-05 09:00"')
    parser.add_argument("--vendor", default="the vendor", help="vendor name for the prompt")
    parser.add_argument(
        "--sender", default=None, help="only messages whose sender domain contains this"
    )
    parser.add_argument("--internal-domain", default="circledelivers.com")
    parser.add_argument("--fake", action="store_true", help="no model call; show the split only")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles choke on narrow spaces

    messages = [m for m in load_messages_jsonl(args.file) if m.from_domain != args.internal_domain]
    if args.sender:
        messages = [m for m in messages if args.sender in m.from_domain]
    classifier = None
    if not args.fake:
        settings = get_settings()
        if settings.llm_provider != "openrouter" or settings.openrouter_api_key is None:
            sys.exit("set FP_LLM_PROVIDER=openrouter and OPENROUTER_API_KEY, or pass --fake")
        classifier = OpenRouterReplyClassifier(
            settings.openrouter_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.openrouter_base_url,
        )
    print(f"{len(messages)} vendor message(s) in {args.file}\n")
    for m in messages:
        own = clean_mail_text(m.body)
        print("=" * 100)
        print(f"{m.sent_at:%Y-%m-%d %H:%M}Z  from {m.from_email}  subject {m.subject!r}")
        print("own words :", json.dumps(own.strip()[:220], ensure_ascii=False))
        print("quoted    :", f"{len(m.quoted)} chars underneath" if m.quoted else "none")
        if classifier is None:
            continue
        context = ReplyContext(
            vendor_name=args.vendor,
            po_numbers=args.po,
            requested_local=args.requested,
            reply_sent_at=m.sent_at,
            subject=m.subject,
            body=m.body,
            quoted=m.quoted,
        )
        output = classifier.classify(context)
        kept, issues = validate_classification(output.result, m.body, m.quoted)
        raw = output.result.model_dump()
        print("model read:", json.dumps({k: raw[k] for k in SHOW if raw.get(k) is not None}))
        print("quotes    :", json.dumps(raw["quotes"], ensure_ascii=False))
        kept_d = kept.model_dump()
        print("kept      :", json.dumps({k: kept_d[k] for k in SHOW if kept_d.get(k) is not None}))
        for issue in issues:
            print(
                f"dropped   : {issue.field_name}: {issue.reason}"
                + (f" ({issue.value!r})" if issue.value else "")
            )
        print(
            f"tokens    : {output.usage.input_tokens} in / {output.usage.output_tokens} out  ({output.model})"
        )
    if classifier is not None:
        classifier.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
