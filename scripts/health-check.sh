#!/bin/bash
# KawaSig health check — run via cron: */5 * * * * /opt/docker/365sig/Set-OutlookSignatures/scripts/health-check.sh

RELAY_HOST="${RELAY_HOST:-localhost}"
RELAY_PORT="${RELAY_SMTP_PORT:-25}"
LOG="${HEALTH_LOG:-/var/log/kawasig-health.log}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"

mkdir -p "$(dirname "$LOG")"

stamp() { date -u +%FT%TZ; }

check_service() {
    local name=$1 container=$2
    if docker inspect --format='{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
        echo "$(stamp) OK $name running" >> "$LOG"
        return 0
    fi
    echo "$(stamp) FAIL $name DOWN" >> "$LOG"
    return 1
}

ANY_FAIL=0

check_service "relay" "kawasig-relay" || ANY_FAIL=1
check_service "deployer" "kawasig-deployer" || ANY_FAIL=1
check_service "redis" "kawasig-redis" || ANY_FAIL=1

if nc -z -w5 "$RELAY_HOST" "$RELAY_PORT" 2>/dev/null; then
    echo "$(stamp) OK SMTP $RELAY_HOST:$RELAY_PORT responding" >> "$LOG"
else
    echo "$(stamp) FAIL SMTP $RELAY_HOST:$RELAY_PORT NOT responding" >> "$LOG"
    ANY_FAIL=1
fi

if [ "$ANY_FAIL" -ne 0 ] && [ -n "$ALERT_WEBHOOK_URL" ]; then
    curl -s -X POST "$ALERT_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{\"text\":\"KawaSig health check FAIL on $(hostname) at $(stamp)\"}" \
        > /dev/null || true
fi

exit "$ANY_FAIL"
