"""Build, deploy and operate the group-mail archive collector in AWS.

Modelled on the doc-intake bot's deploy script: one zip, one Lambda, one EventBridge Scheduler
schedule, least-privilege roles. All commands need `aws sso login --profile <profile>` first and
read FP_MAIL_ARCHIVE_* from .env (or the matching options).

    python scripts/deploy_lidl_mail_archive.py bucket                 # create the archive bucket
    python scripts/deploy_lidl_mail_archive.py build                  # zip only, touches nothing in AWS
    python scripts/deploy_lidl_mail_archive.py deploy                 # build zip, role, function, schedule
    python scripts/deploy_lidl_mail_archive.py invoke                 # run one pass now, print its log
    python scripts/deploy_lidl_mail_archive.py status                 # function, schedule, last pass
    python scripts/deploy_lidl_mail_archive.py schedule on|off

Nothing here reads mail; the collector does that, as the Lambda or through
`facility-profiles mail-archive collect`.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
BUILD = HERE / "build" / "lidl-mail-lambda"
ZIP = HERE / "build" / "lidl-mail-collector.zip"
DEFAULT_BUCKET = "circle-lidl-appointments"
FUNCTION = "circle-lidl-mail-collector"
SCHEDULE = "circle-lidl-mail-every-15-min"
SCHEDULER_ROLE = "circle-lidl-mail-scheduler"
GMAIL_SECRET = "circle-doc-intake/gmail-service-account"  # noqa: S105 - a secret's name, same key as the doc-intake bot
CODE_KEY = "deploy/lidl-mail-collector.zip"
REGION = "us-east-1"
DEPS = ["google-auth>=2.30,<3"]  # the JWT signer; pulls cryptography, which is compiled
TAGS = {"project": "facility-profiles", "component": "lidl-mail-archive"}


# ------------------------------------------------------------------------------- helpers ----


def load_env() -> dict[str, str]:
    """KEY=VALUE lines from .env, shell variables winning."""
    out: dict[str, str] = {}
    env_file = HERE / ".env"
    if env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            if value:
                out[key.strip()] = value
    out.update({k: v for k, v in os.environ.items() if k.startswith("FP_MAIL_ARCHIVE_")})
    return out


def session(profile: str | None):  # type: ignore[no-untyped-def]
    import boto3

    return boto3.session.Session(profile_name=profile, region_name=REGION)


def account_id(sess) -> str:  # type: ignore[no-untyped-def]
    return str(sess.client("sts").get_caller_identity()["Account"])


def secret_arn_pattern(account: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{account}:secret:{GMAIL_SECRET}-??????"


def build() -> Path:
    """Linux wheels for the signer plus the facility_profiles package, zipped.

    cryptography is compiled, so the wheels are fetched for Lambda's platform (Amazon Linux 2023,
    x86_64, glibc 2.34) rather than this machine; the zip cannot be imported here and does not
    need to be.
    """
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(BUILD),
            "--platform",
            "manylinux2014_x86_64",
            "--platform",
            "manylinux_2_28_x86_64",
            "--implementation",
            "cp",
            "--python-version",
            "3.12",
            "--only-binary=:all:",
            *DEPS,
        ],
        check=True,
    )
    shutil.copytree(
        HERE / "src" / "facility_profiles",
        BUILD / "facility_profiles",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    ZIP.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(BUILD.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(BUILD).as_posix())
    print(f"built {ZIP.name}: {ZIP.stat().st_size / 1e6:.1f} MB")
    return ZIP


def ensure_role(  # type: ignore[no-untyped-def]
    iam, name: str, service: str, policy: dict, *, managed: list[str], account: str
) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        fresh = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )["Role"]["Arn"]
        fresh = True
    iam.put_role_policy(RoleName=name, PolicyName="access", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    print(f"role      {name}: {'created' if fresh else 'updated'}")
    if fresh:
        time.sleep(12)  # a new role is not assumable for a few seconds
    return str(arn)


def function_env(bucket: str, prefix: str, user: str, group: str) -> dict[str, str]:
    return {
        "LIDL_MAIL_BUCKET": bucket,
        "LIDL_MAIL_PREFIX": prefix,
        "LIDL_GMAIL_SECRET": GMAIL_SECRET,
        "LIDL_GMAIL_USER": user,
        "LIDL_GROUP": group,
        "LIDL_WINDOW_DAYS": "3",
        "LIDL_MAX_MESSAGES": "300",
    }


# ------------------------------------------------------------------------------ commands ----


def cmd_build(args) -> int:  # type: ignore[no-untyped-def]
    del args
    build()
    return 0


def cmd_bucket(args) -> int:  # type: ignore[no-untyped-def]
    sess = session(args.profile)
    s3 = sess.client("s3")
    try:
        s3.head_bucket(Bucket=args.bucket)
        print(f"bucket    {args.bucket}: exists")
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=args.bucket)  # us-east-1 takes no LocationConstraint
        s3.get_waiter("bucket_exists").wait(Bucket=args.bucket)
        print(f"bucket    {args.bucket}: created")
    s3.put_public_access_block(
        Bucket=args.bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=args.bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    s3.put_bucket_versioning(Bucket=args.bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(
        Bucket=args.bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]}
    )
    print(f"bucket    {args.bucket}: public access blocked, SSE-S3, versioning on, tagged")
    return 0


def cmd_deploy(args) -> int:  # type: ignore[no-untyped-def]
    env = load_env()
    user = args.user or env.get("FP_MAIL_ARCHIVE_GMAIL_USER")
    if not user:
        raise SystemExit("give --user or set FP_MAIL_ARCHIVE_GMAIL_USER in .env")
    prefix = env.get("FP_MAIL_ARCHIVE_PREFIX", "")
    sess = session(args.profile)
    account = account_id(sess)
    zpath = build()
    code = zpath.read_bytes()
    sess.client("s3").put_object(Bucket=args.bucket, Key=CODE_KEY, Body=code)
    print(f"uploaded  s3://{args.bucket}/{CODE_KEY}")

    bucket_arn = f"arn:aws:s3:::{args.bucket}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Archive",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"{bucket_arn}/*",
            },
            {"Sid": "List", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": bucket_arn},
            {
                "Sid": "GmailKey",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": secret_arn_pattern(account),
            },
        ],
    }
    role = ensure_role(
        sess.client("iam"),
        FUNCTION,
        "lambda.amazonaws.com",
        policy,
        managed=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
        account=account,
    )

    lam = sess.client("lambda")
    config = {
        "Role": role,
        "Handler": "facility_profiles.mailarchive.aws_lambda.handler",
        "Runtime": "python3.12",
        "Timeout": 600,
        "MemorySize": 512,
        "Environment": {"Variables": function_env(args.bucket, prefix, user, args.group)},
        "Description": "Every 15 min: the lidl@ group's pickup-appointment threads from Gmail into S3.",
    }
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        print(f"function  {FUNCTION}: code and configuration updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNCTION,
                    Code={"ZipFile": code},
                    Architectures=["x86_64"],
                    Tags=TAGS,
                    **config,
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assumed" not in str(e) or attempt == 5:
                    raise
                time.sleep(5)
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
        print(f"function  {FUNCTION}: created")
    # One pass at a time, no retries: the next quarter-hour is the retry.
    lam.put_function_concurrency(FunctionName=FUNCTION, ReservedConcurrentExecutions=1)
    lam.put_function_event_invoke_config(
        FunctionName=FUNCTION, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60
    )
    logs = sess.client("logs")
    group = f"/aws/lambda/{FUNCTION}"
    with contextlib.suppress(logs.exceptions.ResourceAlreadyExistsException):
        logs.create_log_group(logGroupName=group, tags=TAGS)
    logs.put_retention_policy(logGroupName=group, retentionInDays=90)
    fn_arn = lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]

    sched_role = ensure_role(
        sess.client("iam"),
        SCHEDULER_ROLE,
        "scheduler.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_arn}
            ],
        },
        managed=[],
        account=account,
    )
    sch = sess.client("scheduler")
    try:
        state = sch.get_schedule(Name=SCHEDULE)["State"]
        exists = True
    except sch.exceptions.ResourceNotFoundException:
        state, exists = "ENABLED", False
    body = {
        "Name": SCHEDULE,
        "ScheduleExpression": "rate(15 minutes)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": fn_arn,
            "RoleArn": sched_role,
            "RetryPolicy": {"MaximumRetryAttempts": 0},
        },
        "State": state,
        "Description": "Runs the lidl@ mail archive collector every 15 minutes",
    }
    for attempt in range(6):
        try:
            (sch.update_schedule if exists else sch.create_schedule)(**body)
            break
        except sch.exceptions.ValidationException:
            if attempt == 5:
                raise
            time.sleep(5)
    print(f"schedule  {SCHEDULE}: {'updated' if exists else 'created'}, {state}")
    return 0


def cmd_invoke(args) -> int:  # type: ignore[no-untyped-def]
    lam = session(args.profile).client("lambda")
    r = lam.invoke(FunctionName=FUNCTION, LogType="Tail", Payload=b"{}")
    print(base64.b64decode(r.get("LogResult", "")).decode("utf-8", "replace"))
    payload = r["Payload"].read().decode("utf-8", "replace")
    print(payload)
    return 1 if r.get("FunctionError") else 0


def cmd_status(args) -> int:  # type: ignore[no-untyped-def]
    sess = session(args.profile)
    lam, sch, s3 = sess.client("lambda"), sess.client("scheduler"), sess.client("s3")
    try:
        cfg = lam.get_function_configuration(FunctionName=FUNCTION)
        print(
            f"function  {FUNCTION}: {cfg['State']}, {cfg['Runtime']}, last modified {cfg['LastModified']}"
        )
    except lam.exceptions.ResourceNotFoundException:
        print(f"function  {FUNCTION}: not deployed")
    try:
        print(f"schedule  {SCHEDULE}: {sch.get_schedule(Name=SCHEDULE)['State']}")
    except sch.exceptions.ResourceNotFoundException:
        print(f"schedule  {SCHEDULE}: none")
    try:
        last = json.loads(
            s3.get_object(Bucket=args.bucket, Key="state/last-run.json")["Body"].read()
        )
        print(f"last pass {last.get('at')} as {last.get('mailbox')}\n  {last.get('line')}")
    except s3.exceptions.NoSuchKey:
        print("last pass: none recorded")
    return 0


def cmd_schedule(args) -> int:  # type: ignore[no-untyped-def]
    sch = session(args.profile).client("scheduler")
    current = sch.get_schedule(Name=SCHEDULE)
    sch.update_schedule(
        Name=SCHEDULE,
        ScheduleExpression=current["ScheduleExpression"],
        FlexibleTimeWindow=current["FlexibleTimeWindow"],
        Target=current["Target"],
        State="ENABLED" if args.state == "on" else "DISABLED",
    )
    print(f"schedule  {SCHEDULE}: {'ENABLED' if args.state == 'on' else 'DISABLED'}")
    return 0


def main() -> int:
    env = load_env()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--profile", default=os.environ.get("AWS_PROFILE"), help="AWS CLI profile (SSO)"
    )
    ap.add_argument("--bucket", default=env.get("FP_MAIL_ARCHIVE_BUCKET", DEFAULT_BUCKET))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="build the Lambda zip only, no AWS").set_defaults(fn=cmd_build)
    sub.add_parser("bucket").set_defaults(fn=cmd_bucket)
    d = sub.add_parser("deploy")
    d.add_argument("--user", default=None, help="group member whose mailbox is read")
    d.add_argument("--group", default="lidl@circledelivers.com")
    d.set_defaults(fn=cmd_deploy)
    sub.add_parser("invoke").set_defaults(fn=cmd_invoke)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("schedule")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_schedule)
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
