#!/usr/bin/env bash

set -e

echo "🛡️  Booting Heimdall Ecosystem..."

# --- Config ---
CTRL_PORT=8000
AGENT_PORT=8001
SVC_PORT=5000
SESSION_NAME=${HEIMDALL_TMUX_SESSION:-heimdall}
TMUX_HISTORY=${HEIMDALL_TMUX_HISTORY:-10000}
TMUX_STATUS=${HEIMDALL_TMUX_STATUS:-on}
PROD=${PROD:-false}
# --------------

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_VENV_PY="$ROOT_DIR/../.venv/bin/python"
LOCAL_VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  . "$ROOT_DIR/.env"
  set +a
fi
if [ -x "$LOCAL_VENV_PY" ]; then
  PYTHON_BIN="$LOCAL_VENV_PY"
elif [ -x "$PARENT_VENV_PY" ]; then
  PYTHON_BIN="$PARENT_VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "❌ No python executable found. Activate a venv or install python."
  exit 1
fi

if [ -x "${PYTHON_BIN%/python}/uvicorn" ]; then
  UVICORN_BIN="${PYTHON_BIN%/python}/uvicorn"
else
  UVICORN_BIN="uvicorn"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "❌ tmux is required. Install tmux and retry."
  exit 1
fi

echo "0. Cleaning up previous running instances (Ports $CTRL_PORT, $AGENT_PORT, $SVC_PORT)..."
fuser -k $CTRL_PORT/tcp 2>/dev/null || true
fuser -k $AGENT_PORT/tcp 2>/dev/null || true
fuser -k $SVC_PORT/tcp 2>/dev/null || true
fuser -k 8080/tcp 2>/dev/null || true  # Old default
pkill -f "uvicorn api:app" || true
pkill -f "uvicorn main:app" || true
pkill -f "python discord_bot/bot.py" || true
pkill -f "python api.py" || true
sleep 1

# Create a place to store logs
mkdir -p logs

export WEBHOOK_SECRET="super-secret-key"
export INFRA_API_KEY="heimdall"
export HEIMDALL_ENV="dev"
export HEIMDALL_ALLOW_DEFAULTS="1"
export INFRA_API_URL="${INFRA_API_URL:-http://localhost:$CTRL_PORT}"
export HEIMDALL_API_PORT=$CTRL_PORT
export HEIMDALL_AGENT_PORT=$AGENT_PORT

echo "1. Starting tmux session '$SESSION_NAME'..."
tmux has-session -t "$SESSION_NAME" 2>/dev/null && tmux kill-session -t "$SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" -n control

touch logs/api.log logs/node.log logs/bot.log

tmux set-option -t "$SESSION_NAME" history-limit "$TMUX_HISTORY"
tmux set-option -t "$SESSION_NAME" status "$TMUX_STATUS"
tmux set-option -t "$SESSION_NAME" mouse on
tmux set-option -t "$SESSION_NAME" monitor-activity on
tmux set-option -t "$SESSION_NAME" visual-activity on

tmux send-keys -t "$SESSION_NAME:0" "cd \"$ROOT_DIR\"; export WEBHOOK_SECRET=\"$WEBHOOK_SECRET\" INFRA_API_KEY=\"$INFRA_API_KEY\" HEIMDALL_ENV=\"$HEIMDALL_ENV\" HEIMDALL_ALLOW_DEFAULTS=\"$HEIMDALL_ALLOW_DEFAULTS\" INFRA_API_URL=\"$INFRA_API_URL\" HEIMDALL_API_PORT=$HEIMDALL_API_PORT HEIMDALL_AGENT_PORT=$HEIMDALL_AGENT_PORT; $UVICORN_BIN api:app --host 0.0.0.0 --port $CTRL_PORT 2>&1 | tee logs/api.log" C-m

echo "⏳ Waiting for Control Plane to be ready..."
for i in {1..10}; do
  if curl -s "http://localhost:$CTRL_PORT/health" > /dev/null; then
    echo "🟢 Control Plane is UP."
    break
  fi
  sleep 1
done

echo "2. Bootstrapping 'local-agent' node in database..."
$PYTHON_BIN -c '
import os
from db import SessionLocal, Node
db = SessionLocal()
agent_port = os.environ.get("HEIMDALL_AGENT_PORT", "8001")
node = db.query(Node).filter_by(name="local-agent").first()
if not node:
    node = Node(name="local-agent", uuid="local-agent", host=f"http://localhost:{agent_port}", env="dev")
    db.add(node)
    db.commit()
db.close()
'

if [[ "${PROD,,}" == "true" ]]; then
  echo "3. Skipping dummy service registration (PROD=true)."
else
  echo "3. Registering 'service-1' and 'worker-1' via API curl..."
  curl -sS -X POST http://127.0.0.1:$CTRL_PORT/services \
    -H "X-API-Key: heimdall" \
    -H "Content-Type: application/json" \
    -d '{
      "service": "service-1",
      "node_name": "local-agent",
      "flake": "path:'"$PWD"'/examples/api_service"
    }' > /dev/null

  curl -sS -X POST http://127.0.0.1:$CTRL_PORT/services \
    -H "X-API-Key: heimdall" \
    -H "Content-Type: application/json" \
    -d '{
      "service": "worker-1",
      "node_name": "local-agent",
      "flake": "path:'"$PWD"'/examples/worker_service"
    }' > /dev/null
fi

echo ""

echo "4. Starting fastapi_agent (Node Agent) on port $AGENT_PORT..."
export WEBHOOK_URL="http://localhost:$CTRL_PORT/webhook"
tmux new-window -t "$SESSION_NAME" -n agent
tmux send-keys -t "$SESSION_NAME:1" "cd \"$ROOT_DIR/fastapi_agent\"; export WEBHOOK_URL=\"$WEBHOOK_URL\" WEBHOOK_SECRET=\"$WEBHOOK_SECRET\" HEIMDALL_AGENT_PORT=$HEIMDALL_AGENT_PORT; $UVICORN_BIN main:app --host 0.0.0.0 --port $AGENT_PORT 2>&1 | tee ../logs/node.log" C-m

echo "5. Starting Discord Bot..."
export SSL_CERT_FILE=$($PYTHON_BIN -m certifi)
tmux new-window -t "$SESSION_NAME" -n bot
tmux send-keys -t "$SESSION_NAME:2" "cd \"$ROOT_DIR\"; export SSL_CERT_FILE=\"$SSL_CERT_FILE\" INFRA_API_URL=\"$INFRA_API_URL\" INFRA_API_KEY=\"$INFRA_API_KEY\"; $PYTHON_BIN discord_bot/bot.py 2>&1 | tee logs/bot.log" C-m

tmux new-window -t "$SESSION_NAME" -n logs
tmux send-keys -t "$SESSION_NAME:3" "cd \"$ROOT_DIR\"; tail -n 200 -f logs/api.log" C-m
tmux split-window -t "$SESSION_NAME:3" -h
tmux send-keys -t "$SESSION_NAME:3.1" "cd \"$ROOT_DIR\"; tail -n 200 -f logs/node.log" C-m
tmux split-window -t "$SESSION_NAME:3" -v
tmux send-keys -t "$SESSION_NAME:3.2" "cd \"$ROOT_DIR\"; tail -n 200 -f logs/bot.log" C-m

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Heimdall Infrastructure Ecosystem is UP!"
echo "   - Control Plane: http://localhost:$CTRL_PORT"
echo "   - Node Agent:    http://localhost:$AGENT_PORT"
echo "   - Discord Bot:   Active"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Attach to logs: tmux attach -t $SESSION_NAME"
echo "Stop everything: tmux kill-session -t $SESSION_NAME"
echo ""
