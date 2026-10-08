#!/bin/sh
# Start the board with its loops, from the environment (deploy/README.md, "Settings").
#
#   SCAN_CUSTOMERS  customer files whose loads are checked, comma separated (lidl)
#   SCAN_EVERY      minutes between Transport Pro checks (30)
#   MAIL_EVERY      minutes between mail reads, when FP_BOOKING_INBOX is set (5)
#   WRITEBACK_EVERY minutes between Transport Pro writes, only with FP_BOOKING_TPRO_WRITEBACK=true (1)
#   TIMERS_EVERY    minutes between the no-reply and missed-pickup timers (15)
#   HOST, PORT      where the board listens (127.0.0.1:8000; Caddy in front serves HTTPS)
set -eu

set -- serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --timers-every "${TIMERS_EVERY:-15}"

if [ -n "${SCAN_CUSTOMERS:-}" ]; then
    for customer in $(echo "$SCAN_CUSTOMERS" | tr ',' ' '); do
        set -- "$@" --scan-customer "$customer"
    done
    set -- "$@" --scan-every "${SCAN_EVERY:-30}"
fi

if [ -n "${FP_BOOKING_INBOX:-}" ]; then
    set -- "$@" --mail-every "${MAIL_EVERY:-5}"
fi

case "${FP_BOOKING_TPRO_WRITEBACK:-false}" in
    true | True | TRUE | 1 | yes) set -- "$@" --writeback-every "${WRITEBACK_EVERY:-1}" ;;
esac

echo "facility-profiles $*"
exec facility-profiles "$@"
