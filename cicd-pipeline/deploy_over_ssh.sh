#!/usr/bin/env bash
# Send the tested dashboard build to the server over SSH and publish it there.
#
# Runs on GITHUB'S machine (the "deploy" job of .github/workflows/ci.yml).
# It packs the build together with cicd-pipeline/deploy_frontend.sh, streams the pack over one SSH
# connection, and runs deploy_frontend.sh on the server: that script checks the build, publishes
# it, verifies the site and rolls back by itself if the check fails. This script's exit code is
# the server-side script's exit code, so a failed or rolled-back deploy turns the job red.
#
# Usage:  cicd-pipeline/deploy_over_ssh.sh <folder containing the built dashboard>
# Needs:  DEPLOY_HOST         server address
#         DEPLOY_USER         login name on the server
#         SSH_KEY_FILE        private key file
#         KNOWN_HOSTS_FILE    file holding the server's public host key
#         DEPLOY_DIR          folder FastAPI serves (frontend/dist)
# Optional: SSH_PORT (default 22)
#           EXPECTED_API_URL  leave empty for same-origin /api
#           HEALTH_URL        default http://localhost:9005/
#           BACKUP_DIR

set -euo pipefail

SRC="${1:?usage: $0 <built dashboard folder>}"
: "${DEPLOY_HOST:?DEPLOY_HOST is empty - add the DEPLOY_HOST variable in GitHub}"
: "${DEPLOY_USER:?DEPLOY_USER is empty - add the DEPLOY_USER variable in GitHub}"
: "${SSH_KEY_FILE:?SSH_KEY_FILE is not set}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is not set}"
: "${DEPLOY_DIR:?DEPLOY_DIR is not set}"
EXPECTED_API_URL="${EXPECTED_API_URL:-}"
HEALTH_URL="${HEALTH_URL:-http://localhost:9005/}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SRC/index.html" ] || { echo "[ssh-deploy] no index.html in $SRC" >&2; exit 1; }
[ -f "$here/deploy_frontend.sh" ] || { echo "[ssh-deploy] deploy_frontend.sh not found next to this script" >&2; exit 1; }

ssh_opts=(-i "$SSH_KEY_FILE" -p "${SSH_PORT:-22}"
          -o BatchMode=yes -o IdentitiesOnly=yes
          -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS_FILE"
          -o ConnectTimeout=20 -o ServerAliveInterval=15)

stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
cp -a "$SRC" "$stage/dist"
cp "$here/deploy_frontend.sh" "$stage/deploy_frontend.sh"

printf -v q_dir    '%q' "$DEPLOY_DIR"
printf -v q_url    '%q' "$EXPECTED_API_URL"
printf -v q_health '%q' "$HEALTH_URL"
remote="set -eu; d=\$(mktemp -d); trap 'rm -rf \"\$d\"' EXIT; tar -xzf - -C \"\$d\"; "
envs="DEPLOY_DIR=$q_dir EXPECTED_API_URL=$q_url HEALTH_URL=$q_health"
if [ -n "${BACKUP_DIR:-}" ]; then printf -v q_bk '%q' "$BACKUP_DIR"; envs+=" BACKUP_DIR=$q_bk"; fi
remote+="$envs bash \"\$d/deploy_frontend.sh\" \"\$d/dist\""

echo "[ssh-deploy] sending the build to $DEPLOY_USER@$DEPLOY_HOST"
tar -czf - -C "$stage" . | ssh "${ssh_opts[@]}" "$DEPLOY_USER@$DEPLOY_HOST" "$remote"
