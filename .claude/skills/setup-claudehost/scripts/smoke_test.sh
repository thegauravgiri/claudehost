#!/bin/sh
# Mints one virtual key scoped to all 4 models and sends a real chat
# completion through it, so "the stack is verified working" means an
# actual Claude Code response came back, not just "containers are up."
# Usage: smoke_test.sh <LITELLM_MASTER_KEY>
set -eu

MASTER_KEY="$1"
PORT="${LITELLM_PORT:-4000}"
BASE="http://localhost:${PORT}"

echo "Minting a virtual key..."
KEY_RESPONSE=$(curl -sf -m 15 -X POST "${BASE}/key/generate" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"key_alias": "setup-smoke-test", "models": ["claude-haiku", "claude-sonnet", "claude-opus", "claude-opusplan"]}')

VIRTUAL_KEY=$(echo "$KEY_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
echo "Virtual key: ${VIRTUAL_KEY}"

echo "Sending a real chat completion through claude-haiku..."
COMPLETION=$(curl -sf -m 60 "${BASE}/v1/chat/completions" \
    -H "Authorization: Bearer ${VIRTUAL_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model": "claude-haiku", "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}]}')

echo "Response: ${COMPLETION}"
