#!/usr/bin/env bash
# Fast dev loop: seed fixtures (main+empty) into the app container without
# running the full gate. Mirrors nav_graph_check.sh step 2 exactly.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$HERE")"
DEVICE="${1:-1D04754E-C19B-46DE-BA6D-4472F69D7438}"
BUNDLE="com.hscc.ios"
FIXTURES_SRC="$APP_ROOT/scripts/model_decode_check/fixtures"
CONTAINER=$(xcrun simctl get_app_container "$DEVICE" "$BUNDLE" data)
DOC="$CONTAINER/Documents"
MAIN_LIST=(v1_ping v1_health v1_verify v1_cluster_status cluster_hosts \
           v1_cluster_monitor fleet_stats fleet_throughput fleet_streams \
           v1_autoscale cards card_detail_t_049d6986 kanban_blocked kanban_stale \
           v1_activity_feed autodown_status cron_list daemon_status \
           v1_daemon_history v1_standup v1_review_queue v1_qa_queue v1_projects \
           project_hscc commands v1_profiles profiles_list v1_sessions \
           v1_triggers v1_escalate logs_daemon memory_list template_list \
           template_status review_detail)
rm -rf "$DOC/HSCCFixtures" "$DOC/HSCCFixturesEmpty"
mkdir -p "$DOC/HSCCFixtures" "$DOC/HSCCFixturesEmpty"
for f in "${MAIN_LIST[@]}"; do
  [ -f "$FIXTURES_SRC/$f.json" ] && cp "$FIXTURES_SRC/$f.json" "$DOC/HSCCFixtures/" || true
done
python3 "$HERE/make_empty_fixtures.py" "$FIXTURES_SRC" "$DOC/HSCCFixturesEmpty" "${MAIN_LIST[@]}"
echo "seeded: $(ls "$DOC/HSCCFixtures" | wc -l | tr -d ' ') main, $(ls "$DOC/HSCCFixturesEmpty" | wc -l | tr -d ' ') empty"
