#!/usr/bin/env bash

set -e

echo "🚀 Starting Heimdall Agent Only..."

# --- Config ---
AGENT_PORT=${HEIMDALL_AGENT_PORT:-8001}
SESSION_NAME=${HEIMDALL_TMUX_SESSION:-heimdall-agent}
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMUX_SOCK="${HEIMDALL_TMUX_SOCK:-$ROOT_DIR/.tmux/heimdall.sock}"
mkdir -p "$(dirname "$TMUX_SOCK")"
if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  . "$ROOT_DIR/.env"
  set +a
fi
# --------------

# --- Python resolution ---
PARENT_VENV_PY="$ROOT_DIR/../.venv/bin/python"
LOCAL_VENV_PY="$ROOT_DIR/.venv/bin/python"

if [ -x "$LOCAL_VENV_PY" ]; then
  PYTHON_BIN="$LOCAL_VENV_PY"
elif [ -x "$PARENT_VENV_PY" ]; then
  PYTHON_BIN="$PARENT_VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "❌ Python not found"
  exit 1
fi

UVICORN_CMD=("$PYTHON_BIN" -m uvicorn)

# --- Check tmux ---
if ! command -v tmux >/dev/null 2>&1; then
  echo "❌ tmux is required"
  exit 1
fi

# --- Cleanup old agent ---
echo "🧹 Cleaning port $AGENT_PORT..."
fuser -k $AGENT_PORT/tcp 2>/dev/null || true
pkill -f "uvicorn main:app" || true
sleep 1

# --- Logs ---
mkdir -p logs
touch logs/agent.log

# --- Env ---
if [ -z "${INFRA_API_URL:-}" ]; then
  echo "❌ INFRA_API_URL is required. Set it in .env or export it before running."
  exit 1
fi
if [ -z "${INFRA_API_KEY:-}" ]; then
  echo "❌ INFRA_API_KEY is required. Set it in .env or export it before running."
  exit 1
fi
export INFRA_API_URL="${INFRA_API_URL%/}"
export HEIMDALL_AGENT_PORT=$AGENT_PORT

# --- Start tmux session ---
tmux -S "$TMUX_SOCK" has-session -t "$SESSION_NAME" 2>/dev/null && tmux -S "$TMUX_SOCK" kill-session -t "$SESSION_NAME"
tmux -S "$TMUX_SOCK" new-session -d -s "$SESSION_NAME" -n agent

echo "⚡ Starting agent on port $AGENT_PORT..."

tmux -S "$TMUX_SOCK" send-keys -t "$SESSION_NAME:0" \
  "cd \"$ROOT_DIR/fastapi_agent\"; \
   export INFRA_API_URL=\"$INFRA_API_URL\" INFRA_API_KEY=\"$INFRA_API_KEY\" HEIMDALL_AGENT_PORT=$HEIMDALL_AGENT_PORT; \
   ${UVICORN_CMD[*]} main:app --host 0.0.0.0 --port $AGENT_PORT 2>&1 | tee ../logs/agent.log" C-m

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Agent running at: http://localhost:$AGENT_PORT"
echo "📺 Attach: tmux -S $TMUX_SOCK attach -t $SESSION_NAME"
echo "🛑 Stop:   tmux kill-session -t $SESSION_NAME"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
