#!/bin/bash
# Mailbox A (current / primary) — nightly previous-day invoice processing.
#
# Credentials: copy .env.mailbox_a.example → .env.mailbox_a and fill values,
# OR leave .env.mailbox_a absent and use root .env (GMAIL_EMAIL_1 / GMAIL_PASSWORD_1).
#
# Cron:
#   30 0 * * * TZ=Asia/Kolkata /home/ubuntu/Invoice-processor/invoice/run_mailbox_a.sh

set -euo pipefail

SCRIPT_DIR="/home/ubuntu/Invoice-processor/invoice"
INSTANCE="a"
ENV_FILE="$SCRIPT_DIR/.env.mailbox_a"

export SCHEDULED_INSTANCE="$INSTANCE"
# Default table for mailbox A (override in .env.mailbox_a if needed)
export INVOICE_TABLE_NAME="${INVOICE_TABLE_NAME:-invoice_data}"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

exec bash "$SCRIPT_DIR/run_scheduled.sh" "$@"
