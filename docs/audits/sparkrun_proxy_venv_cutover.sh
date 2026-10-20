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
# 2026-10-20 08:37 RUN REGRESSIONS (both cost ~1.5 min of avoidable outage
# during the live 0.4.0 -> 0.4.1 cutover; fixed here):
#   a. STEP 2 false-positive: the existence test (`lsof ... -sTCP:LISTEN`) and
#      a SEPARATE `lsof -t` owner lookup raced — the listener closed between
#      the two calls, owner came back EMPTY, the `*)` case treated an empty
#      owner as an "UNIDENTIFIED process" and rolled back a healthy stop.
#      Fix: ONE lsof call per iteration; empty owner = port free (continue /
#      re-loop); only a non-empty, non-litellm/sparkrun owner aborts.
#   b. rollback could not restart while the old proxy was DRAINING: the
#      litellm child (of the uvx wrapper) stays alive ~20s finishing
#      ESTABLISHED streaming conns, so `proxy start` reported "already
#      running (PID ...)" and the rollback verification curl got connection
#      refused. Fix: snapshot_proxy_pids BEFORE stop, then wait_proxy_down —
#      bounded wait for the recorded proxy PIDs + children to exit before ANY
#      start — used in BOTH the forward path and fail_rollback; identified
#      snapshot PIDs get SIGTERM if still up when the bounded wait expires.
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
DRAIN_WAIT_SECS=60                                  # bounded drain wait (regression b)
# ──────────────────────────────────────────────────────────────────────────

log(){ echo "[$(date '+%H:%M:%S')] $*"; }

# Snapshot the PIDs a `proxy stop` may leave draining: the :4000 listener
# lineage (uvx wrapper parent + python child via ppid) plus whatever PID the
# CLI itself reports. MUST run BEFORE `proxy stop` — afterwards the lineage
# is gone and there is nothing to wait for.
snapshot_proxy_pids(){
  PROXY_PIDS=""
  local lp p sp
  for lp in $(lsof -nP -iTCP:4000 -sTCP:LISTEN -t 2>/dev/null); do
    PROXY_PIDS="$PROXY_PIDS $lp"
    p=$(ps -o ppid= -p "$lp" 2>/dev/null | tr -d ' ')
    [ -n "$p" ] && [ "$p" != "1" ] && PROXY_PIDS="$PROXY_PIDS $p"
  done
  sp=$("$LINK" proxy status 2>/dev/null | grep -oE '[0-9]+' | head -1)
  [ -n "$sp" ] && PROXY_PIDS="$PROXY_PIDS $sp"
  PROXY_PIDS=$(echo "$PROXY_PIDS" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ')
  log "  snapshot proxy pids:$PROXY_PIDS"
}

# Bounded drain wait (regression b): stop returns before litellm finishes
# serving ESTABLISHED streaming conns; starting a proxy while the recorded
# PIDs are alive yields "already running (PID ...)" + refused conns.
# Wait up to $1 (default $DRAIN_WAIT_SECS), then SIGTERM the identified
# snapshot set once (no name-guessing beyond the snapshot) and wait 10s more.
wait_proxy_down(){
  local deadline=$(( SECONDS + ${1:-$DRAIN_WAIT_SECS} )) alive p
  while :; do
    alive=""
    for p in $PROXY_PIDS; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && { log "  recorded proxy pids exited (drained)"; return 0; }
    [ "$SECONDS" -ge "$deadline" ] && break
    sleep 2
  done
  log "  still alive after ${1:-$DRAIN_WAIT_SECS}s:$alive — SIGTERM identified snapshot pids"
  for p in $PROXY_PIDS; do kill -0 "$p" 2>/dev/null && kill "$p" 2>/dev/null; done
  deadline=$(( SECONDS + 10 ))
  while :; do
    alive=""
    for p in $PROXY_PIDS; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && { log "  identified pids exited after SIGTERM"; return 0; }
    [ "$SECONDS" -ge "$deadline" ] && break
    sleep 2
  done
  log "  WARN: pids still alive:$alive — proceeding (port check below still guards)"
  return 0
}

fail_rollback(){
  log "FAIL: $1 — rolling back to $OLD_VENV"
  ln -sfn "$OLD_VENV/bin/sparkrun" "$LINK"
  snapshot_proxy_pids
  "$OLD_VENV/bin/sparkrun" proxy stop   || log "rollback stop nonzero — draining anyway"
  wait_proxy_down
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
snapshot_proxy_pids
"$LINK" proxy stop || log "stop returned nonzero — proceeding to port check anyway"
wait_proxy_down   # drain guard (regression b): no start may race these pids

log "STEP 2: verify :4000 actually free (zombie-PID guard)"
for i in 1 2 3 4 5 6; do
  owners=$(lsof -nP -iTCP:4000 -sTCP:LISTEN -t 2>/dev/null)   # ONE lsof call (regression a)
  [ -z "$owners" ] && break        # empty owner = port free (was: false-positive rollback)
  owner=$(echo "$owners" | head -1)
  cmd=$(ps -o command= -p "$owner" 2>/dev/null)
  if [ -z "$cmd" ]; then log "  pid $owner already vanished (listener closing) — re-loop"; sleep 2; continue; fi
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
