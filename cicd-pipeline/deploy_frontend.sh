#!/usr/bin/env bash
# Publish a CI-built dashboard to frontend/dist (FastAPI serves this folder), verify it,
# roll back if it is broken.
#
# Runs ON THE SERVER: cicd-pipeline/deploy_over_ssh.sh copies this file and the CI-built dashboard
# here over SSH and runs it. It deploys the exact build CI just tested - nothing is built here,
# and git is never touched.
#
# Usage:   cicd-pipeline/deploy_frontend.sh <folder containing the built dashboard>
# Needs:   DEPLOY_DIR         folder FastAPI serves (frontend/dist)
# Optional EXPECTED_API_URL   baked VITE_API_BASE_URL; leave empty for same-origin /api
#          HEALTH_URL         where to check the site afterwards (default http://localhost:9005/)
#          BACKUP_DIR         previous version (default ~/.cache/invoice-processor-deploy/dashboard-prev)

set -euo pipefail

SRC="${1:?usage: $0 <built dashboard folder>}"
DEPLOY_DIR="${DEPLOY_DIR:?DEPLOY_DIR is not set}"
EXPECTED_API_URL="${EXPECTED_API_URL:-}"
HEALTH_URL="${HEALTH_URL:-http://localhost:9005/}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.cache/invoice-processor-deploy/dashboard-prev}"

say()  { echo "[deploy] $*"; }
fail() { echo "[deploy] FAILED: $*" >&2; exit 1; }

# ---- 1. Is this build safe to publish? ----------------------------------------------------------
[ -d "$SRC" ]            || fail "build folder not found: $SRC"
[ -f "$SRC/index.html" ] || fail "no index.html in $SRC"
compgen -G "$SRC/assets/*.js" >/dev/null || fail "no JavaScript bundle in $SRC/assets"

if [ -n "$EXPECTED_API_URL" ]; then
  grep -qF -- "$EXPECTED_API_URL" "$SRC"/assets/*.js \
    || fail "the build does not contain the backend address \"$EXPECTED_API_URL\" (was VITE_API_BASE_URL set, and the same, when it was built?)"
fi

api_health="${HEALTH_URL%/}/api/health"
api_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$api_health") || api_code=000
[ "$api_code" = 200 ] \
  || fail "the backend did not answer at $api_health (HTTP $api_code). Is invoice-processor running on port 9005?"
say "build looks right (index.html + bundle + backend answers at $api_health)"

# ---- 2. Keep the live version so we can go back ------------------------------------------------
mkdir -p "$DEPLOY_DIR" "$BACKUP_DIR"
rsync -a --delete "$DEPLOY_DIR"/ "$BACKUP_DIR"/
say "previous version saved to $BACKUP_DIR"

rollback() {
  say "rolling back to the previous version"
  rsync -a --delete "$BACKUP_DIR"/ "$DEPLOY_DIR"/
}

# ---- 3. Publish --------------------------------------------------------------------------------
rsync -a --delete-after --delay-updates "$SRC"/ "$DEPLOY_DIR"/
say "published to $DEPLOY_DIR"

# ---- 4. Verify the site really serves it -------------------------------------------------------
verify() {
  local page code asset
  page=$(curl -fsS --max-time 10 "$HEALTH_URL") || { echo "[deploy] cannot fetch $HEALTH_URL" >&2; return 1; }
  grep -q "<div id=\"root\"" <<<"$page" || { echo "[deploy] $HEALTH_URL did not return the dashboard page" >&2; return 1; }
  for asset in $(grep -oE '(src|href)="[^"]*assets/[^"]+"' <<<"$page" | sed -E 's/^(src|href)="(\.\/)?//; s/"$//'); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${HEALTH_URL%/}/$asset") || code=000
    [ "$code" = 200 ] || { echo "[deploy] $asset returned HTTP $code" >&2; return 1; }
  done
}

if verify; then
  say "OK - site is serving the new dashboard"
else
  rollback
  verify && fail "new version did not verify; the previous version was restored and is serving" \
         || fail "new version did not verify AND the rollback did not verify either - check the FastAPI service now"
fi
