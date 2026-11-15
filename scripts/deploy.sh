#!/usr/bin/env bash
# Deploy the studio to the project server.
#
#   ./scripts/deploy.sh            # pull main, rebuild, restart
#   ./scripts/deploy.sh --no-pull  # rebuild the current checkout only
#
# The server keeps the app on 127.0.0.1:8000 and the static report on
# 127.0.0.1:8001; nginx on 8443 is the only public entry point.
set -euo pipefail

HOST="${DEPLOY_HOST:-root@46.34.163.254}"
DIR="${DEPLOY_DIR:-/opt/order-forecasting-engine}"
PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

ssh "$HOST" bash -s -- "$DIR" "$PULL" <<'REMOTE'
set -euo pipefail
DIR="$1"
PULL="$2"
cd "$DIR"

if [[ "$PULL" == "1" ]]; then
  git fetch origin main
  git reset --hard origin/main
fi
echo "deploying $(git log --oneline -1)"

mkdir -p artifacts reports docs
docker compose build
docker compose up -d

# Regenerate the static report from the freshly built image.
docker run --rm -v "$DIR/docs:/app/docs" bnpl-forecast-engine:latest \
  python scripts/build_static_demo.py --out /app/docs

docker compose ps
curl -fsS -o /dev/null http://127.0.0.1:8000/healthz && echo "app healthy"
REMOTE

echo "done: https://46.34.163.254:8443/"
