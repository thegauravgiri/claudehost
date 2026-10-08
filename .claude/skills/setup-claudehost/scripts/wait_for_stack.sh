#!/bin/sh
# Waits for the runner to report ready and for litellm to report healthy.
# Claude Code is installed at image build time (not on first boot, unlike
# the old claudebox-based setup), so this is normally quick - a few seconds.
# Run from the repo root.
set -eu

LITELLM_PORT="${LITELLM_PORT:-4000}"
TIMEOUT="${TIMEOUT:-180}"
elapsed=0

echo "Waiting for the runner to come up..."
while true; do
    if docker compose logs runner 2>&1 | grep -q "Uvicorn running"; then
        echo "runner is up."
        break
    fi
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo "Timed out after ${TIMEOUT}s waiting for runner. Check: docker compose logs runner" >&2
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
