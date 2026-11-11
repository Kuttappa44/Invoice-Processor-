#!/bin/bash
# Mailbox B — nightly previous-day invoice processing (separate IMAP + MySQL table).
#
# Credentials: copy .env.mailbox_b.example → .env.mailbox_b and fill:
#   GMAIL_EMAIL_1, GMAIL_PASSWORD_1, INVOICE_TABLE_NAME
#
# Cron:
#   30 0 * * * TZ=Asia/Kolkata /home/ubuntu/Invoice-processor/invoice/run_mailbox_b.sh

set -euo pipefail

SCRIPT_DIR="/home/ubuntu/Invoice-processor/invoice"
INSTANCE="b"
ENV_FILE="$SCRIPT_DIR/.env.mailbox_b"

export SCHEDULED_INSTANCE="$INSTANCE"
export INVOICE_TABLE_NAME="${INVOICE_TABLE_NAME:-invoice_data_b}"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: Missing $ENV_FILE" >&2
    echo "Copy .env.mailbox_b.example to .env.mailbox_b and set GMAIL_EMAIL_1 / GMAIL_PASSWORD_1 / INVOICE_TABLE_NAME." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [ -z "${GMAIL_EMAIL_1:-}" ] || [ -z "${GMAIL_PASSWORD_1:-}" ]; then
    echo "ERROR: GMAIL_EMAIL_1 and GMAIL_PASSWORD_1 must be set in $ENV_FILE" >&2
    exit 1
fi

exec bash "$SCRIPT_DIR/run_scheduled.sh" "$@"
