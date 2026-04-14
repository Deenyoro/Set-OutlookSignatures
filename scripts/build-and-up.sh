#!/bin/bash
# One-shot build + start. Run from repo root: ./scripts/build-and-up.sh
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f .env ]; then
    echo "Missing .env. Run: cp .env.example .env  (then edit it)" >&2
    exit 1
fi

CERT="sig-relay/certs/fullchain.pem"
KEY="sig-relay/certs/privkey.pem"
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    echo "WARNING: TLS cert/key missing in sig-relay/certs/" >&2
    echo "         Postfix will refuse to start TLS. Provision via certbot first." >&2
fi

echo "==> docker compose build"
docker compose build

echo "==> docker compose up -d"
docker compose up -d

echo "==> docker compose ps"
docker compose ps

echo
echo "Tail logs:  docker compose logs -f"
echo "Stop:       docker compose down"
