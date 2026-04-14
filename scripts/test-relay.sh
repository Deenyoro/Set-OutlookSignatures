#!/bin/bash
# Send 3 test emails through the relay to exercise inject / passthrough / skip paths.
# Requires: swaks (sudo apt install swaks)

set -e

HOST="${RELAY_HOST:-localhost}"
PORT="${RELAY_SMTP_PORT:-25}"
TO="${TEST_TO:-test@example.com}"
FROM_MANAGED="${TEST_FROM_MANAGED:-john@treconstruction.net}"
FROM_EXTERNAL="${TEST_FROM_EXTERNAL:-outsider@gmail.com}"
DOMAIN="${TEST_DOMAIN:-treconstruction.net}"

if ! command -v swaks >/dev/null 2>&1; then
    echo "swaks not installed. apt install swaks" >&2
    exit 1
fi

echo "==> Test 1: managed sender, NO signature marker (relay should INJECT)"
swaks --to "$TO" --from "$FROM_MANAGED" --server "$HOST:$PORT" \
      --header "Subject: KawaSig test 1 - no sig" \
      --add-header "Content-Type: text/html" \
      --body '<html><body><p>Hello, this email has no signature.</p></body></html>'

echo
echo "==> Test 2: managed sender WITH signature marker (relay should PASS THROUGH)"
swaks --to "$TO" --from "$FROM_MANAGED" --server "$HOST:$PORT" \
      --header "Subject: KawaSig test 2 - already signed" \
      --add-header "Content-Type: text/html" \
      --body "<html><body><p>Hello.</p><!--KAWASIG:${DOMAIN}-->SIG<!--/KAWASIG:${DOMAIN}--></body></html>"

echo
echo "==> Test 3: external sender (relay should SKIP)"
swaks --to "$TO" --from "$FROM_EXTERNAL" --server "$HOST:$PORT" \
      --header "Subject: KawaSig test 3 - external" \
      --add-header "Content-Type: text/html" \
      --body '<html><body><p>External email.</p></body></html>'

echo
echo "Done. Check container logs:"
echo "  docker compose logs --tail=50 sig-relay"
