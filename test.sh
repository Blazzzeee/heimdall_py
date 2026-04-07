#!/usr/bin/env bash
set -euo pipefail

set -a; . ./.env; set +a

AGENT_URL="${HEIMDALL_AGENT_URL:-http://localhost:8001}"
export FLAKE='path:/home/blazzee/Development/heimdall_py/examples/api_service'
export TS=$(date +%s)

export BODY=$(python - <<'PY'
import json, os
print(json.dumps({"flake": os.environ["FLAKE"]}))
PY
)

SIG=$(python - <<'PY'
import hmac, hashlib, os
body = os.environ["BODY"]
ts = os.environ["TS"]
key = os.environ["INFRA_API_KEY"].encode()
print(hmac.new(key, (body+ts).encode(), hashlib.sha256).hexdigest())
PY
)

curl -sS -X POST "$AGENT_URL/inspect" \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $TS" \
  -H "X-Signature: $SIG" \
  -d "$BODY"
