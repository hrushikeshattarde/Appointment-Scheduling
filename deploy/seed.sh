#!/bin/bash
# Replace the server's store with the one uploaded to s3://<backup bucket>/seed/board.db.
# For the first setup only (moving the laptop board's data over): the server's own store and its
# continuous S3 copy are deleted first, so the seed is what the board and the backups carry on from.
#   sudo /opt/pickup-booking/src/deploy/seed.sh --yes
set -euo pipefail

if [ "${1:-}" != "--yes" ]; then
    echo "seed: this deletes the server's store and its S3 copy; run it with --yes" >&2
    exit 2
fi
here=$(cd "$(dirname "$0")" && pwd)
source /etc/pickup-booking/compose.env

aws s3api head-object --region "$AWS_REGION" --bucket "$BACKUP_BUCKET" --key seed/board.db >/dev/null
systemctl stop pickup-booking.service
rm -f /var/lib/pickup-booking/board.db /var/lib/pickup-booking/board.db-wal \
    /var/lib/pickup-booking/board.db-shm
rm -rf /var/lib/pickup-booking/.board.db-litestream
aws s3 rm --region "$AWS_REGION" --recursive --quiet "s3://$BACKUP_BUCKET/litestream/"
"$here/restore.sh"
systemctl start pickup-booking.service
echo "seed: the board runs on the seeded store"
