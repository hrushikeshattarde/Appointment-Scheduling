#!/bin/bash
# Move the server to another version of the code and restart the board:
#   update.sh            the latest main
#   update.sh <ref>      a branch, a tag or a commit (an earlier commit rolls back)
# The settings are read again from Parameter Store on the restart, and the store stays.
set -euo pipefail

ref="${1:-main}"
repo=/opt/pickup-booking/src
git -C "$repo" fetch --quiet --tags origin
if git -C "$repo" rev-parse --verify --quiet "origin/$ref" >/dev/null; then
    target="origin/$ref"
else
    target="$ref"
fi
git -C "$repo" checkout --quiet --detach "$target"
echo "update: now at $(git -C "$repo" log --oneline -1)"
systemctl restart pickup-booking.service
systemctl --no-pager --lines=0 status pickup-booking.service
