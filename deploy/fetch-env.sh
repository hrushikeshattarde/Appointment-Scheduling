#!/bin/bash
# Write the board's settings from Parameter Store to /etc/pickup-booking/app.env (root only).
#
# Every parameter under SSM_PATH (/pickup-booking/) becomes NAME=value, with the last part of its
# name as NAME: /pickup-booking/TPRO_PASSWORD -> TPRO_PASSWORD=... Secrets are SecureString
# parameters, decrypted here with the instance's IAM role and never written anywhere else.
set -euo pipefail

source /etc/pickup-booking/compose.env
path="${SSM_PATH:-/pickup-booking/}"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

aws ssm get-parameters-by-path --region "$AWS_REGION" --path "$path" --recursive \
    --with-decryption --query 'Parameters[].[Name,Value]' --output text |
    while IFS=$'\t' read -r name value; do
        printf '%s=%s\n' "${name##*/}" "$value"
    done >"$tmp"

if ! grep -q '^TPRO_PASSWORD=' "$tmp"; then
    echo "fetch-env: no TPRO_PASSWORD under $path; put the settings first (deploy/README.md)" >&2
    exit 1
fi
install -m 600 -o root -g root "$tmp" /etc/pickup-booking/app.env
echo "fetch-env: $(wc -l <"$tmp") settings from $path"
