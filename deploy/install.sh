#!/bin/bash
# Set the server up once (the instance's user data runs it; running it again is harmless):
#   install.sh <host name> <backup bucket> <region>
# It records where things are, writes the settings, gives the board its store and starts the
# service that runs docker compose at every boot.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: install.sh <host name> <backup bucket> <region>" >&2
    exit 2
fi
here=$(cd "$(dirname "$0")" && pwd)

install -d -m 755 /etc/pickup-booking
cat >/etc/pickup-booking/compose.env <<EOF
HOST_NAME=$1
BACKUP_BUCKET=$2
AWS_REGION=$3
SSM_PATH=/pickup-booking/
EOF
install -d -o 10001 -g 10001 -m 750 /var/lib/pickup-booking

"$here/fetch-env.sh"
"$here/restore.sh"

install -m 644 "$here/pickup-booking.service" /etc/systemd/system/pickup-booking.service
systemctl daemon-reload
systemctl enable --now pickup-booking.service
echo "install: the board is starting at https://$1 (first start builds the image: a few minutes)"
