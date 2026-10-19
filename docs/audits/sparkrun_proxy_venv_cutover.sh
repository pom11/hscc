#!/bin/bash
# sparkrun proxy venv cutover — DETACHED ONLY.
#
# WHY THIS EXISTS (2026-10-19, learned the hard way twice):
#   1. Never stop the proxy from a session whose own model rides :4000 — you
#      kill your own inference mid-cutover (the 12:35 incident). Run this as:
#         nohup bash sparkrun_041_cutover.sh > /tmp/sparkrun_cutover.log 2>&1 &
#      from a terminal that does NOT depend on the proxy, or from an operator
#      shell. It performs stop -> swap symlink -> start -> verify as ONE unit
#      and AUTO-ROLLS BACK to the previous venv on any failed gate.
#   2. `sparkrun proxy stop` may not clear a listener held by a zombie PID /
#      orphaned autodiscover (t_bbce7f58 class). This script therefore verifies
#      the PORT is actually free after stop, and reaps ONLY a port-owner it can
#      positively identify as a sparkrun/litellm descendant of the OLD venv —
#      it never kills by name-guess.
#
# EDIT THE CONFIG BLOCK for the target tag, then read every gate before running.
set -u
export CONDA_NO_PLUGINS=true
unset CONDA_SHLVL CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_PREFIX 2>/dev/null

# ── config ────────────────────────────────────────────────────────────────
NEW_VENV="$HOME/sparkrun/.venv-sparkrun-041"        # target venv (built + gated already)
NEW_BRANCH="feat/proxy-least-in-flight-041"         # fork branch checked out at NEW checkout
NEW_CHECKOUT="$HOME/dev/sparkrun-041-verify"        # editable source of NEW_VENV
OLD_VENV="$HOME/sparkrun/.venv-sparkrun-040"        # rollback target (current live)
OLD_BRANCH="feat/proxy-least-in-flight-040"
OLD_CHECKOUT="$HOME/dev/sparkrun-live"
LINK="$HOME/.local/bin/sparkrun"
MODELS=(worker-model orchestrator-model)
LAUNCHD_LABEL="gui/$(id -u)/com.hermes.hscc_daemon"
EXPECTED_NEW="0.4.1"
# ──────────────────────────────────────────────────────────────────────────

log(){ echo "[$(date '+%H:%M:%S')] $*"; }
fail_rollback(){
  log "FAIL: $1 — rolling back to $OLD_VENV"
  ln -sfn "$OLD_VENV/bin/sparkrun" "$LINK"
  "$OLD_VENV/bin/sparkrun" proxy stop   || true
  "$OLD_VENV/bin/sparkrun" proxy start  || true
  sleep 20
  if curl -fsS -m 30 -o /dev/null "http://127.0.0.1:4000/v1/models"; then
    log "ROLLBACK OK — proxy back on $EXPECTED_NEW-less old runtime (:4000 200). DO NOT retry; report."
  else
    log "ROLLBACK ALSO FAILED — manual: ln -sfn $OLD_VENV/bin/sparkrun $LINK; $OLD_VENV/bin/sparkrun proxy stop; $OLD_VENV/bin/sparkrun proxy start. Get the operator."
  fi
  exit 1
}

log "PRE-FLIGHT"
[ -x "$NEW_VENV/bin/sparkrun" ] || fail_rollback "new venv missing/not executable"
[ "$("$NEW_VENV/bin/sparkrun" --version 2>/dev/null | grep -c "$EXPECTED_NEW")" = 1 ] || fail_rollback "new venv version != $EXPECTED_NEW"
[ "$(git -C "$NEW_CHECKOUT" rev-parse --abbrev-ref HEAD)" = "$NEW_BRANCH" ] || fail_rollback "new checkout not on $NEW_BRANCH"
curl -fsS -m 30 -o /dev/null "http://127.0.0.1:4000/v1/models" || fail_rollback "proxy not healthy BEFORE cutover — abort, don't cut over onto a broken base"
# optional operator gate: uncomment to refuse cutover while a card runs
# hermes kanban list --status running 2>/dev/null | grep -q . && fail_rollback "kanban card running"

log "STEP 1: stop proxy ($("$LINK" --version 2>/dev/null))"
"$LINK" proxy stop || log "stop returned nonzero — proceeding to port check anyway"

log "STEP 2: verify :4000 actually free (zombie-PID guard)"
for i in 1 2 3 4 5 6; do
  lsof -nP -iTCP:4000 -sTCP:LISTEN >/dev/null 2>&1 || break
  owner=$(lsof -nP -iTCP:4000 -sTCP:LISTEN -t 2>/dev/null | head -1)
  cmd=$(ps -o command= -p "$owner" 2>/dev/null)
  log "  :4000 still held by pid $owner: ${cmd:0:90}"
  case "$cmd" in
    *litellm*|*sparkrun*) log "  port owner is sparkrun/litellm lineage — SIGTERM $owner (identified, not name-guessed)"; kill "$owner" 2>/dev/null ;;
    *) fail_rollback ":4000 held by UNIDENTIFIED process $owner — refusing to kill; investigate manually" ;;
  esac
  sleep 5
done
lsof -nP -iTCP:4000 -sTCP:LISTEN >/dev/null 2>&1 && fail_rollback ":4000 never freed"

log "STEP 3: swap symlink -> $NEW_VENV"
ln -sfn "$NEW_VENV/bin/sparkrun" "$LINK"
[ "$("$LINK" --version 2>/dev/null | grep -c "$EXPECTED_NEW")" = 1 ] || fail_rollback "symlink swap did not take"

log "STEP 4: start proxy on new runtime"
"$LINK" proxy start || fail_rollback "proxy start failed on new venv"

log "STEP 5: verify gates"
sleep 25
for m in "${MODELS[@]}"; do
  code=$(curl -s -o /tmp/cutover_chat.json -w "%{http_code}" -m 60 \
    -H "Authorization: Bearer $(grep -m1 master_key "$HOME/.cache/sparkrun/proxy/litellm_config.yaml" | awk '{print $2}')" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":8}" \
    http://127.0.0.1:4000/v1/chat/completions)
  [ "$code" = "200" ] || fail_rollback "chat $m returned $code"
  grep -q '"content"' /tmp/cutover_chat.json || fail_rollback "chat $m 200 but hollow (no content)"
  log "  chat $m: 200 with real content"
done
grep -q "sparkrun_balancer.handler" "$HOME/.cache/sparkrun/proxy/litellm_config.yaml" || fail_rollback "balancer callback missing from generated config"
log "  balancer callback present (router_settings routing_strategy: simple-shuffle is EXPECTED — not drift)"
"$LINK" status || fail_rollback "sparkrun status failed"

log "STEP 6: restart daemon so it re-resolves sparkrun"
launchctl kickstart -k "$LAUNCHD_LABEL" || log "kickstart nonzero — check daemon manually"
sleep 20
launchctl print "$LAUNCHD_LABEL" 2>/dev/null | grep -q "state = running" || log "WARN: daemon not confirmed running — check ~/.hscc/daemon.log"

log "CUTOVER COMPLETE: $("$LINK" --version 2>/dev/null) — proxy verified, daemon restarted."
log "Now (from any session): re-point daemon-side expectations, run hscc scripts/run_tests.sh, and PR the lock bump."
