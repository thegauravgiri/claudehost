#!/bin/sh
# Waits for claudebox to finish its first-run `npm install` of Claude Code
# (observed anywhere from ~5s to ~2min depending on npm registry latency)
# and for litellm to report healthy. Run from the repo root.
set -eu

LITELLM_PORT="${LITELLM_PORT:-4000}"
TIMEOUT="${TIMEOUT:-180}"
elapsed=0

echo "Waiting for claudebox to finish installing Claude Code..."
while true; do
    if docker compose logs claudebox 2>&1 | grep -q "Uvicorn running"; then
        echo "claudebox is up."
        break
    fi
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo "Timed out after ${TIMEOUT}s waiting for claudebox. Check: docker compose logs claudebox" >&2
        exit 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
done

echo "Waiting for litellm to report healthy..."
elapsed=0
while true; do
    if curl -sf -m 5 "http://localhost:${LITELLM_PORT}/health/liveliness" >/dev/null 2>&1; then
        echo "litellm is up."
        break
    fi
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo "Timed out after ${TIMEOUT}s waiting for litellm. Check: docker compose logs litellm" >&2
        exit 1
    fi
    sleep 3
    elapsed=$((elapsed + 3))
done
