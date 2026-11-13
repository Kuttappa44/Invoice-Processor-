#!/usr/bin/env bash
# Bring the backend on THIS server up to a given commit of main, and restart the service - safely.
#
# Runs ON THE SERVER, started by cicd-pipeline/deploy_backend_over_ssh.sh.
# This folder is also where people work, so nothing is changed until every guard passes,
# and a failed restart is rolled back.
#
# Needs:    REPO_DIR       the project folder on this server
#           TARGET_SHA     the commit to deploy (full SHA)
# Optional: TOKEN_FILE     file holding a GitHub read token (to fetch from a private repo)
#           SERVICE_NAME   default invoice-processor
#           RESTART_CMD    default "sudo systemctl restart $SERVICE_NAME"
#           STATUS_URL     default http://localhost:9005/api/health
#           RUNNING_URL    default http://localhost:9005/api/tasks/running-process
#           PYTHON_BIN / PIP_BIN   default $REPO_DIR/.venv/bin/python and pip
#           HEALTH_WAIT_SECS       how long to wait for the restarted service (default 60)
#           SERVICE_STARTED_CMD    prints when the service started (epoch seconds)

set -euo pipefail

REPO_DIR="${REPO_DIR:?REPO_DIR is not set}"
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is not set}"
SERVICE_NAME="${SERVICE_NAME:-invoice-processor}"
RESTART_CMD="${RESTART_CMD:-sudo systemctl restart $SERVICE_NAME}"
STATUS_URL="${STATUS_URL:-http://localhost:9005/api/health}"
RUNNING_URL="${RUNNING_URL:-http://localhost:9005/api/tasks/running-process}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
PIP_BIN="${PIP_BIN:-$REPO_DIR/.venv/bin/pip}"
HEALTH_WAIT_SECS="${HEALTH_WAIT_SECS:-60}"

say()  { echo "[backend] $*"; }
fail() { echo "[backend] FAILED: $*" >&2; exit 1; }

cd "$REPO_DIR" || fail "project folder not found: $REPO_DIR"
export GIT_TERMINAL_PROMPT=0

git_fetch() {
  if [ -n "${TOKEN_FILE:-}" ] && [ -s "$TOKEN_FILE" ]; then
    local auth; auth=$(printf 'x-access-token:%s' "$(cat "$TOKEN_FILE")" | base64 -w0)
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
      GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth" git fetch --quiet "$@"
  else
    git fetch --quiet "$@"
  fi
}

status_body() { curl -fsS --max-time 10 "$STATUS_URL" 2>/dev/null; }
running_body() { curl -fsS --max-time 10 "$RUNNING_URL" 2>/dev/null; }

wait_for_service() {
  local deadline=$((SECONDS + HEALTH_WAIT_SECS)) body
  while [ $SECONDS -lt $deadline ]; do
    body=$(status_body || true)
    if grep -q '"healthy"' <<<"$body"; then return 0; fi
    sleep 2
  done
  return 1
}

# ---- 1. get the commit --------------------------------------------------------------------------
say "target commit ${TARGET_SHA:0:10}"
git_fetch origin "$TARGET_SHA" || git_fetch origin main || fail "could not fetch from GitHub"
git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null || fail "commit $TARGET_SHA is not available after fetching"

old=$(git rev-parse HEAD)

# ---- 2. does anything need doing? ---------------------------------------------------------------
EXCLUDE_RE='^(frontend/|backend/tests/|\.github/|scripts/|cicd-pipeline/|cicd-pipeline-pack/)|^\.gitignore$|\.md$'
if [ "$old" = "$TARGET_SHA" ]; then changed=""; else changed=$(git diff --name-only "$old" "$TARGET_SHA"); fi
relevant=$(grep -vE "$EXCLUDE_RE" <<<"$changed" || true)

service_started() {
  if [ -n "${SERVICE_STARTED_CMD:-}" ]; then bash -c "$SERVICE_STARTED_CMD" 2>/dev/null; return; fi
  local pid; pid=$(systemctl show "$SERVICE_NAME" -p MainPID --value 2>/dev/null) || return 1
  { [ -n "$pid" ] && [ "$pid" != 0 ]; } || return 1
  stat -c %.9Y "/proc/$pid" 2>/dev/null
}
newest_code_mtime() { git ls-files | grep -vE "$EXCLUDE_RE" | xargs -r -d '\n' stat -c %.9Y 2>/dev/null | sort -n | tail -1; }
service_is_stale() {
  local started newest
  started=$(service_started || true); [ -n "$started" ] || return 1
  newest=$(newest_code_mtime || true); [ -n "$newest" ] || return 1
  awk -v a="$newest" -v b="$started" 'BEGIN { exit !(a > b) }'
}
stale=0; if service_is_stale; then stale=1; fi

branch=$(git symbolic-ref --short -q HEAD || echo "(detached)")
dirty=$(git status --porcelain --untracked-files=no)
can_fast_forward() { git merge-base --is-ancestor "$old" "$TARGET_SHA"; }

if [ -z "$relevant" ] && [ "$stale" -eq 0 ]; then
  if [ "$old" = "$TARGET_SHA" ]; then say "already up to date - nothing to do"; exit 0; fi
  say "no backend code changed (only frontend / tests / workflow / docs) - no restart needed"
  if [ "$branch" = main ] && [ -z "$dirty" ] && can_fast_forward; then
    git merge --ff-only --quiet "$TARGET_SHA" && say "checkout brought up to date"
  else
    say "checkout left as it is (branch: $branch${dirty:+, has uncommitted changes})"
  fi
  exit 0
fi

if [ -n "$relevant" ]; then
  mode=deploy
  say "backend code changed:"; sed 's/^/[backend]   /' <<<"$relevant"
else
  mode=heal
  say "no new backend commits, but the running service is OLDER than the code on this server (the code was changed or synced here without a restart) - it needs a restart"
fi

# ---- 3. guards: nothing has been touched yet ----------------------------------------------------
guard_failure() {
  if [ "$branch" != main ]; then
    echo "this server's folder is on branch '$branch', not main. Switch it back to main (and commit or stash your work), then re-run this deploy."; return
  fi
  if [ -n "$dirty" ]; then
    printf 'tracked files here have uncommitted changes, so they would be overwritten or restarted into the live service:\n%s\nCommit or stash them, then re-run this deploy.\n' "$(sed 's/^/    /' <<<"$dirty")"; return
  fi
  if ! can_fast_forward; then
    echo "this server has commits that main does not (or the histories diverged); it cannot be fast-forwarded. Push or discard them first."; return
  fi
  if grep -Eq '"running":[[:space:]]*true' <<<"$(running_body || true)"; then
    echo "process_invoices is running right now. Restarting would kill it. Re-run this deploy when it has finished."; return
  fi
}
reason=$(guard_failure)
if [ -n "$reason" ]; then
  if [ "$mode" = heal ]; then
    say "NOT restarting the service now (it is older than the code here): $reason"
    exit 0
  fi
  fail "$reason"
fi
if [ -z "$(status_body || true)" ]; then
  say "note: the backend is not answering at $STATUS_URL - continuing (a deploy may be what fixes it)"
fi

# ---- 4. apply ----------------------------------------------------------------------------------
undo() {
  say "putting the previous code back (${old:0:10})"
  git reset --hard --quiet "$old"
}

if [ "$old" != "$TARGET_SHA" ]; then
  git merge --ff-only --quiet "$TARGET_SHA" || fail "fast-forward failed (nothing was changed)"
  say "code updated to ${TARGET_SHA:0:10}"
fi

if grep -qx 'requirements.txt' <<<"$changed"; then
  say "requirements.txt changed - installing"
  "$PIP_BIN" install -q -r requirements.txt || { undo; fail "pip install failed; previous code restored, service NOT restarted"; }
fi

while IFS= read -r f; do
  case "$f" in *.py) [ -f "$f" ] || continue
    "$PYTHON_BIN" -m py_compile "$f" 2>&1 || { undo; fail "syntax error in $f; previous code restored, service NOT restarted"; } ;;
  esac
done <<<"$relevant"
say "changed Python files compile"

say "restarting the service"
bash -c "$RESTART_CMD" || { undo; bash -c "$RESTART_CMD" || true; fail "restart command failed; previous code restored"; }

# ---- 5. did it come back? ---------------------------------------------------------------------
if wait_for_service; then
  say "OK - backend is running ${TARGET_SHA:0:10} and answering at $STATUS_URL"
  exit 0
fi

echo "[backend] the service did not answer within ${HEALTH_WAIT_SECS}s after the restart" >&2
undo
bash -c "$RESTART_CMD" || true
if wait_for_service; then
  fail "the new version did not come up; the previous code was restored and the service is answering again"
else
  fail "the new version did not come up AND the service is not answering after the rollback - check it now: sudo systemctl status $SERVICE_NAME"
fi
