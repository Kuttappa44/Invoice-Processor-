#!/usr/bin/env bash
# Update the backend on the server to the commit CI just tested - over SSH.
#
# Runs on GITHUB'S machine (the "deploy" job of .github/workflows/ci.yml). It sends
# cicd-pipeline/deploy_backend.sh plus GitHub's short-lived, read-only token to the server over one
# SSH connection and runs it there. The server has no GitHub login of its own, so the token is
# how it can fetch this one commit; it is only valid while this job runs, and can only read.
# This script's exit code is the server-side script's exit code.
#
# Usage:  cicd-pipeline/deploy_backend_over_ssh.sh
# Needs:  DEPLOY_HOST  DEPLOY_USER  SSH_KEY_FILE  KNOWN_HOSTS_FILE
#         REPO_DIR     the project folder on the server
#         TARGET_SHA   the commit to deploy (github.sha)
#         GIT_TOKEN    the job's GitHub token (github.token)
# Optional: SSH_PORT (default 22)   plus anything deploy_backend.sh reads
#           (STATUS_URL, RUNNING_URL, RESTART_CMD, ...) which is passed through when set.

set -euo pipefail

: "${DEPLOY_HOST:?DEPLOY_HOST is empty}"
: "${DEPLOY_USER:?DEPLOY_USER is empty}"
: "${SSH_KEY_FILE:?SSH_KEY_FILE is not set}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is not set}"
: "${REPO_DIR:?REPO_DIR is not set}"
: "${TARGET_SHA:?TARGET_SHA is not set}"
: "${GIT_TOKEN:?GIT_TOKEN is not set}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$here/deploy_backend.sh" ] || { echo "[ssh-deploy] deploy_backend.sh not found next to this script" >&2; exit 1; }

ssh_opts=(-i "$SSH_KEY_FILE" -p "${SSH_PORT:-22}"
          -o BatchMode=yes -o IdentitiesOnly=yes
          -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS_FILE"
          -o ConnectTimeout=20 -o ServerAliveInterval=15)

# The token travels INSIDE the packed stream, into a file only the login user can read, so it never
# appears in a command line on the server. The remote temp folder is removed when the script ends.
stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
( umask 077; printf '%s' "$GIT_TOKEN" > "$stage/token" )
cp "$here/deploy_backend.sh" "$stage/deploy_backend.sh"

envs=""
add_env() { local q; printf -v q '%q' "$2"; envs+="$1=$q "; }
add_env REPO_DIR "$REPO_DIR"
add_env TARGET_SHA "$TARGET_SHA"
for v in SERVICE_NAME RESTART_CMD STATUS_URL RUNNING_URL PYTHON_BIN PIP_BIN HEALTH_WAIT_SECS SERVICE_STARTED_CMD; do
  if [ -n "${!v:-}" ]; then add_env "$v" "${!v}"; fi
done

remote="set -eu; d=\$(mktemp -d); trap 'rm -rf \"\$d\"' EXIT; tar -xzf - -C \"\$d\"; "
remote+="${envs}TOKEN_FILE=\"\$d/token\" bash \"\$d/deploy_backend.sh\""

echo "[ssh-deploy] asking $DEPLOY_USER@$DEPLOY_HOST to update the backend to ${TARGET_SHA:0:10}"
tar -czf - -C "$stage" . | ssh "${ssh_opts[@]}" "$DEPLOY_USER@$DEPLOY_HOST" "$remote"
