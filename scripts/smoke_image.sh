#!/usr/bin/env bash
# Smoke-test a built sre-agent image before it is pushed.
# Usage: scripts/smoke_image.sh <image>
# Runs the image with dummy config, then checks /health, the /mcp initialize and tools/list
# round trips, and the Claude Agent SDK's bundled CLI. Prints the container logs on any failure.
set -euo pipefail

IMAGE="${1:?usage: smoke_image.sh <image>}"
NAME="sre-agent-smoke-$$"
TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-90}"
MCP_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

cleanup() {
    docker rm -f "$NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "SMOKE FAILED: $1" >&2
    echo "--- docker logs ($NAME) ---" >&2
    docker logs "$NAME" >&2 || true
    echo "--- end docker logs ---" >&2
    exit 1
}

# Only the settings the app cannot start without. The MCP access mode is "off" so the smoke
# run needs no Cloudflare Access config (the setting is ignored until it exists).
docker run -d --name "$NAME" -p 127.0.0.1::8000 \
    -e PROMETHEUS_URL=http://127.0.0.1:9 \
    -e GRAFANA_URL=http://127.0.0.1:9 \
    -e GRAFANA_SERVICE_ACCOUNT_TOKEN=smoke \
    -e OPENAI_API_KEY=sk-smoke \
    -e MCP_ACCESS_MODE=off \
    "$IMAGE" > /dev/null

PORT="$(docker port "$NAME" 8000/tcp | head -n1 | sed 's/.*://')"
BASE="http://127.0.0.1:${PORT}"

# 1. /health answers 200 with a status field.
health=""
for ((i = 0; i < TIMEOUT_SECONDS; i++)); do
    if health="$(curl -fsS --max-time 5 "$BASE/health" 2> /dev/null)"; then
        break
    fi
    health=""
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
        fail "container exited before /health answered"
    fi
    sleep 1
done
[ -n "$health" ] || fail "/health did not return 200 within ${TIMEOUT_SECONDS}s"
echo "$health" | grep -q '"status"' || fail "/health body has no status field: $health"
echo "ok: /health -> $health"

# Responses may be plain JSON or a single SSE event ("data: {...}"), so strip the SSE prefix.
mcp_post() {
    curl -fsS --max-time 20 "${MCP_HEADERS[@]}" -X POST "$BASE/mcp" -d "$1" | sed 's/^data: //'
}

# 2. /mcp initialize returns serverInfo.
init="$(mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')" \
    || fail "POST /mcp initialize failed"
echo "$init" | grep -q '"serverInfo"' || fail "initialize response has no serverInfo: $init"
echo "ok: /mcp initialize"

# 3. tools/list has at least 10 tools, including prometheus_instant_query.
tools="$(mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')" || fail "POST /mcp tools/list failed"
count="$(echo "$tools" | grep -o '"name":"[a-z_]*"' | grep -c . || true)"
echo "$tools" | grep -q '"name":"prometheus_instant_query"' || fail "tools/list lacks prometheus_instant_query"
[ "$count" -ge 10 ] || fail "tools/list returned only $count tool names (need at least 10)"
echo "ok: /mcp tools/list ($count names)"

# 4. The Claude Agent SDK's bundled CLI runs.
docker exec "$NAME" sh -c 'exec "$(ls /app/.venv/lib/python*/site-packages/claude_agent_sdk/_bundled/claude)" --version' \
    || fail "bundled claude CLI --version failed"
echo "ok: bundled claude CLI"

echo "SMOKE PASSED: $IMAGE"
