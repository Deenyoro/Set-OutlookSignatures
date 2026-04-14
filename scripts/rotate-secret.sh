#!/bin/bash
# Rotate the Entra app client secret in .env and restart containers.
# Usage: ./scripts/rotate-secret.sh <new-secret-value>

set -e

NEW_SECRET="${1:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [ -z "$NEW_SECRET" ]; then
    echo "Usage: $0 <new-client-secret-value>" >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Not found: $ENV_FILE" >&2
    exit 1
fi

cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%s)"

if grep -q '^CLIENT_SECRET=' "$ENV_FILE"; then
    sed -i "s|^CLIENT_SECRET=.*|CLIENT_SECRET=${NEW_SECRET}|" "$ENV_FILE"
else
    echo "CLIENT_SECRET=${NEW_SECRET}" >> "$ENV_FILE"
fi

echo "Updated $ENV_FILE. Restarting containers..."
( cd "$REPO_ROOT" && docker compose restart sig-deployer sig-relay )
echo "Done. Verify: docker compose logs --tail=50 sig-deployer sig-relay"
