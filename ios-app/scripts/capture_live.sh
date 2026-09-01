#!/bin/bash
# capture_live.sh — capture REAL live API GET responses into a timestamped folder.
#
# model_decode_check decodes committed FIXTURES (written by hand). Those prove
# the models match what we THINK the server returns. This script captures what
# the server ACTUALLY returns, so live_decode_check can run the same compiled
# models against the real wire — closing the "fixtures match our assumptions,
# not reality" gap.
#
# READ-ONLY: fires GET requests only. Never mutates live state. The bearer token
# is read from ~/.hscc/api-token (never echoed, never committed).
#
# Output layout (under this project's scripts/):
#   scripts/live_captures/<timestamp>/
#     manifest.json         route -> { file, url, status }
#     <safe-route>.json     the raw response body (pretty-printed)
#
# The capture set is derived from the Swift CLIENT's real GET routes
# (HSCCClient.swift), not a hand-maintained copy, so a new route in the app is
# picked up here. Parameterized routes resolve their {param} from a prior
# capture in the SAME folder (e.g. a card id from /v1/cards), so they are
# exercised against the real registry.
#
# Usage: scripts/capture_live.sh [base_url] [token_file]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # the ios-app/ project dir

BASE="${1:-http://100.115.243.3:8788}"
TOKEN_FILE="${2:-$HOME/.hscc/api-token}"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "error: token file not found at $TOKEN_FILE" >&2
  exit 1
fi
TOKEN="$(cat "$TOKEN_FILE")"
HEADER_AUTH="Authorization: Bearer $TOKEN"
PY=/Users/desac/miniconda3/envs/p313/bin/python3

OUT="scripts/live_captures/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
echo "capturing live GET routes from $BASE -> $OUT"

# A helper that maps a route to a safe filesystem name (strip slashes/qmarks).
safe_name() { echo "$1" | tr '/?=.' '____' | sed 's/_*$//'; }

declare -a STATUSES=()   # "route<TAB>file<TAB>url<TAB>status" lines

# fetch <url> <outname> <route> — GET, capture body, record status.
# The response body is saved verbatim to <outname>.json (pretty-printed via
# python if it is valid JSON), or the raw bytes if not.
fetch() {
  local url="$1" name="$2" route="$3"
  local code raw
  raw="$(curl -s -o "$OUT/$name.raw" -w '%{http_code}' \
      -H "$HEADER_AUTH" -H 'Accept: application/json' "$url")"
  code="$raw"
  # pretty-print JSON if valid; keep raw otherwise
  if "$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$OUT/$name.raw" 2>/dev/null; then
    "$PY" -m json.tool "$OUT/$name.raw" > "$OUT/$name.json" 2>/dev/null \
      || cp "$OUT/$name.raw" "$OUT/$name.json"
  else
    cp "$OUT/$name.raw" "$OUT/$name.json"
  fi
  rm -f "$OUT/$name.raw"
  STATUSES+=("$route"$'\t'"$name"$'\t'"$url"$'\t'"$code")
  echo "  [$code] $route  ->  $name.json"
}

# --------------------------------------------------------------------------- #
# Pass 0: root list endpoints (no params) — also the source of param discovery
# --------------------------------------------------------------------------- #
fetch "$BASE/v1/ping"                                v1_ping                    "/v1/ping"
fetch "$BASE/v1/verify"                              v1_verify                  "/v1/verify"
fetch "$BASE/v1/health"                              v1_health                  "/v1/health"
fetch "$BASE/v1/cluster/status"                      v1_cluster_status          "/v1/cluster/status"
fetch "$BASE/v1/cluster/hosts"                       v1_cluster_hosts           "/v1/cluster/hosts"
fetch "$BASE/v1/cluster/monitor"                     v1_cluster_monitor         "/v1/cluster/monitor"
fetch "$BASE/v1/cluster/jobs"                        v1_cluster_jobs            "/v1/cluster/jobs"
fetch "$BASE/v1/cluster/info"                        v1_cluster_info            "/v1/cluster/info"
fetch "$BASE/v1/cards"                               v1_cards                   "/v1/cards"
fetch "$BASE/v1/standup"                             v1_standup                 "/v1/standup"
fetch "$BASE/v1/review/queue"                        v1_review_queue            "/v1/review/queue"
fetch "$BASE/v1/qa/queue"                            v1_qa_queue                "/v1/qa/queue"
fetch "$BASE/v1/fleet/throughput"                    v1_fleet_throughput        "/v1/fleet/throughput"
fetch "$BASE/v1/fleet/streams"                       v1_fleet_streams           "/v1/fleet/streams"
fetch "$BASE/v1/autoscale"                           v1_autoscale               "/v1/autoscale"
fetch "$BASE/v1/autodown/status"                     v1_autodown_status         "/v1/autodown/status"
fetch "$BASE/v1/projects"                            v1_projects                "/v1/projects"
fetch "$BASE/v1/daemon/status"                       v1_daemon_status           "/v1/daemon/status"
fetch "$BASE/v1/triggers"                            v1_triggers                "/v1/triggers"
fetch "$BASE/v1/escalate"                            v1_escalate                "/v1/escalate"
fetch "$BASE/v1/profiles"                            v1_profiles                "/v1/profiles"
fetch "$BASE/v1/kanban/blocked"                      v1_kanban_blocked          "/v1/kanban/blocked"
fetch "$BASE/v1/kanban/stale?older_than=0"           v1_kanban_stale            "/v1/kanban/stale?older_than=0"
fetch "$BASE/v1/activity/feed?limit=50"              v1_activity_feed           "/v1/activity/feed?limit=50"
fetch "$BASE/v1/fleet/stats?days=7"                  v1_fleet_stats             "/v1/fleet/stats?days=7"
fetch "$BASE/v1/template/list"                       v1_template_list           "/v1/template/list"
fetch "$BASE/v1/template/status"                     v1_template_status         "/v1/template/status"

# --------------------------------------------------------------------------- #
# Pass 1: parameterized routes — resolve {param} from the pass-0 captures
# --------------------------------------------------------------------------- #
json_get() { # json_get <file> <python-expr>  (expr has access to `d`)
  "$PY" -c '
import json,sys
d=json.load(open(sys.argv[1]))
try:
    v=eval(sys.argv[2]); print(v if v is not None else "")
except Exception: print("")
' "$1" "$2"
}

# A card id + project name + session id discoverable from live captures.
CARD_ID="$(json_get "$OUT/v1_cards.json" 'd.get("cards")[0].get("id") if d.get("cards") else ""')"
PROJECT="$(json_get "$OUT/v1_projects.json" 'd.get("projects")[0].get("name") if d.get("projects") else ""')"
REVIEW_ID="$(json_get "$OUT/v1_review_queue.json" 'd.get("reviews")[0].get("card_id") if d.get("reviews") else ""')"
TEMPLATE="$(json_get "$OUT/v1_template_list.json" 'd.get("templates")[0].get("name") if d.get("templates") else ""')"

if [ -n "$CARD_ID" ]; then
  fetch "$BASE/v1/cards/$CARD_ID"  v1_cards_detail  "/v1/cards/{id}->$CARD_ID"
fi
if [ -n "$REVIEW_ID" ]; then
  fetch "$BASE/v1/review/$REVIEW_ID"  v1_review_detail  "/v1/review/{id}->$REVIEW_ID"
fi
if [ -n "$PROJECT" ]; then
  fetch "$BASE/v1/projects/$PROJECT"        v1_projects_detail     "/v1/projects/{name}->$PROJECT"
  fetch "$BASE/v1/projects/$PROJECT/session/events?limit=20" v1_project_session_events "/v1/projects/{name}/session/events"
  fetch "$BASE/v1/profile/editor/$PROJECT-orch" v1_profile_editor   "/v1/profile/editor/{profile}->${PROJECT}-orch"
  fetch "$BASE/v1/sessions?profile=$PROJECT-orch" v1_sessions       "/v1/sessions?profile={profile}"
fi
if [ -n "$TEMPLATE" ]; then
  fetch "$BASE/v1/template/preview/$TEMPLATE"  v1_template_preview  "/v1/template/preview/{name}->$TEMPLATE"
fi

# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
{
  echo "{"
  echo "  \"captured_at\": \"$(date -u +%Y%m%dT%H%M%SZ)\","
  echo "  \"base\": \"$BASE\","
  echo "  \"routes\": ["
  first=1
  for row in "${STATUSES[@]}"; do
    IFS=$'\t' read -r route name url code <<< "$row"
    if [ $first -eq 0 ]; then echo "    ,"; fi
    first=0
    printf '    { "route": %s, "file": %s, "url": %s, "status": %s }' \
      "$("$PY" -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$route")" \
      "$("$PY" -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$name.json")" \
      "$("$PY" -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$url")" \
      "$code"
  done
  echo ""
  echo "  ]"
  echo "}"
} > "$OUT/manifest.json"

echo ""
echo "captured $((${#STATUSES[@]})) routes -> $OUT"
echo "manifest: $OUT/manifest.json"
