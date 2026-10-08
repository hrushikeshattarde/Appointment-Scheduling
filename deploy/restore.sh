#!/bin/bash
# Give the server its store when it has none: the continuous S3 copy first (a rebuilt server picks
# up where the old one stopped), else the seed a person uploaded (the first start), else nothing
# (the board starts empty and its scan fills it).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
source /etc/pickup-booking/compose.env
store=/var/lib/pickup-booking/board.db

if [ ! -f "$store" ]; then
    docker run --rm --network host \
        -e BACKUP_BUCKET="$BACKUP_BUCKET" -e AWS_REGION="$AWS_REGION" \
        -v /var/lib/pickup-booking:/data \
        -v "$here/litestream.yml:/etc/litestream.yml:ro" \
        litestream/litestream:0.3.13 restore -if-db-not-exists -if-replica-exists /data/board.db
    [ -f "$store" ] && echo "restore: store restored from s3://$BACKUP_BUCKET/litestream/"
fi

if [ ! -f "$store" ] && aws s3api head-object --region "$AWS_REGION" \
    --bucket "$BACKUP_BUCKET" --key seed/board.db >/dev/null 2>&1; then
    aws s3 cp --region "$AWS_REGION" "s3://$BACKUP_BUCKET/seed/board.db" "$store"
    echo "restore: store seeded from s3://$BACKUP_BUCKET/seed/board.db"
fi

[ -f "$store" ] || echo "restore: no store yet; the board starts empty"
chown -R 10001:10001 /var/lib/pickup-booking
