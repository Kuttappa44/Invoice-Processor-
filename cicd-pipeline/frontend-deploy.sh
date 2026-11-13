#!/usr/bin/env bash
# Rebuild the dashboard into dist/. FastAPI serves this folder, so a successful
# build is the live UI — there is no nginx copy step.
#
# Run this after ANY change to src/ or index.html, otherwise the live site
# keeps serving the previously built bundle. The next merge to main overwrites
# this with the CI artifact.

set -euo pipefail
cd "$(dirname "$0")/../frontend"

echo "Building frontend..."
npm run build

echo
echo -n "Verifying live site... "
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:9005/ || echo "000")
if [ "$code" = "200" ]; then
    echo "OK (HTTP 200)"
    echo "Deployed. Users may need one refresh to pick up new assets."
else
    echo "FAILED (HTTP $code)"
    echo "The build succeeded but FastAPI is not serving. Check: sudo systemctl status invoice-processor"
    exit 1
fi
