#!/bin/bash

# Scheduled Invoice Processing — runs daily at 12:30 AM IST (previous day's emails).
# After each successful pass, checks for remaining UNSEEN mail in the same range;
# if any remain, queues another invocation (blocking flock — waits if busy).
#
# Multi-mailbox: run via run_mailbox_{a,b,c}.sh so each process has its own
# IMAP credentials, INVOICE_TABLE_NAME, lock file, and logs.
# SCHEDULED_INSTANCE (default: a) isolates flock/logs so the three can run in parallel.
#
# Cron (three nightly jobs — previous day):
#   30 0 * * * TZ=Asia/Kolkata /home/ubuntu/Invoice-processor/invoice/run_mailbox_a.sh
#   30 0 * * * TZ=Asia/Kolkata /home/ubuntu/Invoice-processor/invoice/run_mailbox_b.sh
#   30 0 * * * TZ=Asia/Kolkata /home/ubuntu/Invoice-processor/invoice/run_mailbox_c.sh
#
# Follow-up (manual):
#   ./run_mailbox_a.sh --from-date 2026-06-29 --to-date 2026-06-29 --pass 2
#   ./run_scheduled.sh --from-date 2026-06-29 --to-date 2026-06-29 --pass 2

SCRIPT_DIR="/home/ubuntu/Invoice-processor/invoice"
SCHEDULED_SCRIPT="$SCRIPT_DIR/run_scheduled.sh"
VENV="$SCRIPT_DIR/.venv/bin/activate"
LOG_DIR="$SCRIPT_DIR/logs"

# Isolate parallel mailbox runs (inherited by follow-up passes).
INSTANCE_RAW="${SCHEDULED_INSTANCE:-a}"
INSTANCE="$(printf '%s' "$INSTANCE_RAW" | tr -cd 'A-Za-z0-9_')"
INSTANCE="${INSTANCE:-a}"
export SCHEDULED_INSTANCE="$INSTANCE"

LOCK_FILE="$LOG_DIR/scheduled_${INSTANCE}.lock"

FROM_DATE=""
TO_DATE=""
PASS=1
MAX_PASSES="${MAX_PASSES:-10}"

while [ $# -gt 0 ]; do
    case "$1" in
        --from-date)
            FROM_DATE="$2"
            shift 2
            ;;
        --to-date)
            TO_DATE="$2"
            shift 2
            ;;
        --pass)
            PASS="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

export TZ=Asia/Kolkata

if [ -z "$FROM_DATE" ]; then
    FROM_DATE=$(date -d 'yesterday' +%Y-%m-%d)
fi
if [ -z "$TO_DATE" ]; then
    TO_DATE="$FROM_DATE"
fi

LOG_FILE="$LOG_DIR/scheduled_${INSTANCE}_$(date +%Y%m%d_%H%M%S)_pass${PASS}.log"

mkdir -p "$LOG_DIR"

log() {
    echo "$1" | tee -a "$LOG_FILE"
}

# ── Prevent overlapping runs for THIS instance (wait if busy) ───
exec 200>"$LOCK_FILE"
log "⏳ Acquiring run lock for instance '$INSTANCE' (will wait if this mailbox is in progress)..."
flock 200
log "✅ Lock acquired ($LOCK_FILE)"

log "========================================"
log "Invoice Processor - Scheduled Run"
log "Instance: $INSTANCE | pass $PASS / max $MAX_PASSES"
log "Started at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
log "========================================"
log ""
log "📅 Date range: $FROM_DATE → $TO_DATE"
log "📬 IMAP user: ${GMAIL_EMAIL_1:-(from .env)}"
log "🗄️  Table: ${INVOICE_TABLE_NAME:-invoice_data (default)}"
log ""

cd "$SCRIPT_DIR" || {
    log "❌ Cannot cd to $SCRIPT_DIR"
    exit 1
}

if [ ! -f "$VENV" ]; then
    log "❌ Virtualenv not found at $VENV"
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV"

log "🚀 Starting process_invoices (instance $INSTANCE, pass $PASS)..."
log "   (mailbox email/PDF count runs first inside process_invoices)"
log ""

if python -c "
from main import process_invoices
process_invoices('$FROM_DATE', '$TO_DATE')
" >> "$LOG_FILE" 2>&1; then
    EXIT_CODE=0
    log ""
    log "Status: ✅ SUCCESS (instance $INSTANCE, pass $PASS)"
else
    EXIT_CODE=1
    log ""
    log "Status: ❌ FAILED (instance $INSTANCE, pass $PASS — see log above for details)"
fi

# ── Queue follow-up if unread remain (only after success) ──
# Child inherits SCHEDULED_INSTANCE, GMAIL_*, INVOICE_TABLE_NAME from this environment.
REMAINING=0
if [ "$EXIT_CODE" -eq 0 ]; then
    log ""
    log "📬 Checking inbox for remaining unread in date range..."
    if REMAINING=$(python -c "
from main import count_unread_emails_for_date_range
print(count_unread_emails_for_date_range('$FROM_DATE', '$TO_DATE'))
" 2>>"$LOG_FILE"); then
        REMAINING="${REMAINING//[^0-9]/}"
        REMAINING="${REMAINING:-0}"
        log "   Unread remaining: $REMAINING"
    else
        log "   ⚠️  Could not count unread emails — skipping follow-up queue"
        REMAINING=0
        EXIT_CODE=1
    fi
fi

if [ "$EXIT_CODE" -eq 0 ] && [ "$REMAINING" -gt 0 ]; then
    if [ "$PASS" -lt "$MAX_PASSES" ]; then
        NEXT_PASS=$((PASS + 1))
        log ""
        log "🔄 Queueing follow-up pass $NEXT_PASS ($REMAINING unread)..."
        # Background job blocks on flock until this shell exits and releases the lock.
        bash "$SCHEDULED_SCRIPT" \
            --from-date "$FROM_DATE" \
            --to-date "$TO_DATE" \
            --pass "$NEXT_PASS" &
        log "   Follow-up queued (will start when lock is free)"
    else
        log ""
        log "⚠️  Max passes ($MAX_PASSES) reached with $REMAINING unread still in range"
        EXIT_CODE=1
    fi
elif [ "$EXIT_CODE" -eq 0 ] && [ "$REMAINING" -eq 0 ]; then
    log ""
    log "✅ No unread emails left for $FROM_DATE → $TO_DATE"
fi

log ""
log "========================================"
log "Completed at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
log "Log file: $LOG_FILE"
log "========================================"

find "$LOG_DIR" -name "scheduled_*.log" -type f -mtime +30 -delete

exit "$EXIT_CODE"
