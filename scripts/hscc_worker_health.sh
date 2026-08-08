#!/bin/bash
# hscc_worker_health.sh — periodic check of all 4 vLLM endpoints.
# Reports only anomalies (down endpoints, model mismatch, slow response).
# Silent when all green. Watchdog pattern via --no-agent.

set -u
EXPECTED=(
  "10.0.0.244:nvidia/Qwen3.6-35B-A3B-NVFP4"
  "10.0.0.246:Qwen/Qwen3.6-27B-FP8"
  "10.0.0.247:Qwen/Qwen3.6-27B-FP8"
  "10.0.0.248:Qwen/Qwen3.6-27B-FP8"
)
TIMEOUT=5
PROBLEMS=()

for entry in "${EXPECTED[@]}"; do
  HOST="${entry%%:*}"
  EXPECT_MODEL="${entry#*:}"
  URL="http://$HOST:8000/v1/models"

  RESP=$(/usr/bin/curl -s -m "$TIMEOUT" "$URL" 2>/dev/null) || {
    PROBLEMS+=("$HOST: unreachable")
    continue
  }

  GOT_MODEL=$(/usr/bin/python3 -c "
import json,sys
# Endpoints advertise the concrete id alongside a role alias
# (orchestrator-model / worker-model); which lands at index 0 is not
# guaranteed. This check compares against the CONCRETE expected id, so pick the
# first served id that is NOT a role alias (fall back to the first if all are).
ALIASES={'orchestrator-model','worker-model'}
try:
    d=json.loads(sys.argv[1])
    names=[m.get('id') for m in (d.get('data') or []) if isinstance(m,dict) and m.get('id')]
    got=next((n for n in names if n not in ALIASES), names[0] if names else '')
    print(got)
except Exception:
    print('')
" "$RESP" 2>/dev/null)

  if [ -z "$GOT_MODEL" ]; then
    PROBLEMS+=("$HOST: no model in response")
  elif [ "$GOT_MODEL" != "$EXPECT_MODEL" ]; then
    PROBLEMS+=("$HOST: model mismatch — expected $EXPECT_MODEL, got $GOT_MODEL")
  fi
done

if [ ${#PROBLEMS[@]} -eq 0 ]; then
  exit 0
fi

echo "[$(date -Iseconds)] HSCC worker health: ${#PROBLEMS[@]} issue(s):"
for p in "${PROBLEMS[@]}"; do
  echo "  - $p"
done
