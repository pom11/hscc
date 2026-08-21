#!/bin/bash
# HSCC Bootstrap — preflight-gated, topology-detecting installer.
#   1. Prereq gate: sparkrun cluster configured + Hermes present (hard stop).
#   2. Detect cluster (nodes/user/NAS) from sparkrun.
#   3. Minimal interview (orchestrator node, recipe) — defaults + --yes.
#   4. Install: skills, role profiles, ~/.hscc + serving.json, daemon.
# No vLLM model provisioning. Idempotent.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HSCC_DIR="${HSCC_DIR:-$HOME/.hscc}"
PLUGINS="$HERMES_HOME/plugins"
RECIPES_DIR="${RECIPES_DIR:-$HOME/.sparkrun-local/recipes}"
BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BOOT_DIR/.." && pwd)"
PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

ASSUME_YES=false; FORCE=false; NO_BACKUP=false
SKIP_SKILLS=false; SKIP_ROLES=false; SKIP_DAEMON=false; SKIP_PATCHES=false
HSCC_TELEGRAM="${HSCC_TELEGRAM:-}"
for a in "$@"; do case "$a" in
  --yes|-y) ASSUME_YES=true ;;
  --force) FORCE=true ;;
  --no-backup) NO_BACKUP=true ;;
  --skip-skills) SKIP_SKILLS=true ;;
  --skip-roles) SKIP_ROLES=true ;;
  --skip-daemon) SKIP_DAEMON=true ;;
  --skip-patches) SKIP_PATCHES=true ;;
  --telegram=yes) HSCC_TELEGRAM=yes ;;
  --telegram=no) HSCC_TELEGRAM=no ;;
  --help|-h) echo "Usage: hscc-bootstrap [--yes] [--force] [--no-backup] [--skip-skills|--skip-roles|--skip-daemon|--skip-patches] [--telegram=yes|no]"; exit 0 ;;
  *) echo "Unknown option: $a" >&2; exit 1 ;;
esac; done

say()  { echo "  $1"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
die()  { echo "  ✗ $1" >&2; exit 1; }
hdr()  { echo; echo "━━━ $1 ━━━"; }

# ── Stage 1: prerequisites (preflight doctor, hard gate) ────────────────────
hdr "Prerequisites (doctor)"
# The doctor checks python/pyyaml/sparkrun/cluster/Hermes/disk/gateway and
# explains any failure in plain language. It exits non-zero on a FATAL miss, so
# a half-configured machine stops here instead of failing deep in a later stage.
"$PYBIN" "$BOOT_DIR/doctor.py" || die "preflight failed — fix the items above, then re-run."
CLUSTER_JSON="$("$PYBIN" "$BOOT_DIR/detect.py" 2>/dev/null || echo null)"
[ "$CLUSTER_JSON" != "null" ] || die "No sparkrun cluster configured. Run: sparkrun cluster add <name> <hosts...>"

# ── Stage 2: detect ────────────────────────────────────────────────────────
hdr "Detected cluster"
HOSTS="$(echo "$CLUSTER_JSON" | "$PYBIN" -c 'import sys,json;print(" ".join(json.load(sys.stdin)["hosts"]))')"
NAS="$(echo "$CLUSTER_JSON" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin).get("nas") or "")')"
CLUSTER_NAME="$(echo "$CLUSTER_JSON" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin)["name"])')"
read -ra HOST_ARR <<< "$HOSTS"
say "cluster: $CLUSTER_NAME"
say "nodes:   $HOSTS"
say "NAS:     ${NAS:-<none>}"

# ── Stage 3: interview (minimal; defaults under --yes) ─────────────────────
hdr "Configuration"
ORCH="${HOST_ARR[0]}"
# default orchestrator = a host already serving a model, else first host
for h in "${HOST_ARR[@]}"; do
  if curl -s --max-time 4 "http://$h:8000/v1/models" -o /dev/null -w '%{http_code}' 2>/dev/null | grep -q 200; then ORCH="$h"; break; fi
done
if ! $ASSUME_YES && [ "${#HOST_ARR[@]}" -gt 1 ]; then
  read -rp "  Orchestrator node [$ORCH]: " _in; [ -n "${_in:-}" ] && ORCH="$_in"
fi
ok "orchestrator: $ORCH"

# recipe: sole recipe auto, else pick.
# (read loop, not mapfile — macOS ships Bash 3.2 which lacks mapfile.)
RECIPES=()
while IFS= read -r _line; do
  [ -n "$_line" ] && RECIPES+=("$_line")
done < <("$PYBIN" -c "import sys;sys.path.insert(0,'$BOOT_DIR');import detect;print('\n'.join(detect.list_recipes('$RECIPES_DIR')))")
RECIPE=""
if [ "${#RECIPES[@]}" -eq 1 ]; then RECIPE="${RECIPES[0]}"
elif [ "${#RECIPES[@]}" -gt 1 ]; then
  if $ASSUME_YES; then RECIPE="${RECIPES[0]}"; else
    echo "  recipes:"; for i in "${!RECIPES[@]}"; do echo "    [$i] ${RECIPES[$i]}"; done
    read -rp "  Recipe index [0]: " _ri; RECIPE="${RECIPES[${_ri:-0}]}"
  fi
fi
if [ -n "$RECIPE" ]; then
  MODEL="$("$PYBIN" -c "import sys;sys.path.insert(0,'$BOOT_DIR');import detect;print(detect.recipe_model('$RECIPE') or '')")"
  ok "recipe: $RECIPE"
  ok "model:  ${MODEL:-<unknown>}"
else
  warn "no recipes found in $RECIPES_DIR — serving.json model will be blank"
  MODEL=""
fi

# Telegram: OPTIONAL out-of-band fallback. ASK rather than assume, default No.
# Resolved by telegram_choice.py — HSCC_TELEGRAM env override → --yes documented
# default (No) → interactive prompt. Declining must never break an install.
TG_ARGS=""; $ASSUME_YES && TG_ARGS="--yes"
TG_JSON=$(HSCC_TELEGRAM="${HSCC_TELEGRAM:-}" "$PYBIN" "$BOOT_DIR/telegram_choice.py" $TG_ARGS 2>/dev/null)
TELEGRAM=$(echo "$TG_JSON" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin)["telegram"])' 2>/dev/null || echo no)
if [ "$TELEGRAM" = "yes" ]; then
  ok "telegram: enabled (optional out-of-band fallback)"
else
  warn "telegram: declined — HSCC runs fully without it (cluster ops, kanban, dispatch, API unaffected)"
fi

# Suggest a cluster template matching the detected host count (suggestion only;
# the operator applies it explicitly).
SUGGEST=$("$PYBIN" -c "import sys;sys.path.insert(0,'$BOOT_DIR');import suggest_template as s;print(s.pick_template(${#HOST_ARR[@]}) or '')" 2>/dev/null)
[ -n "$SUGGEST" ] && say "template: $SUGGEST (apply: hscc template apply $SUGGEST --confirm)"

# ── Stage 4: install ───────────────────────────────────────────────────────
hdr "Install: plugin files"
# Copy the plugin tree from the work repo into the Hermes runtime dir
# (backup-then-overwrite; --no-backup overwrites in place). Skipped
# automatically when the repo IS the runtime dir (old in-place layout). All
# later stages run from $PLUGINS/... so this must come first.
if [ "$REPO_ROOT" -ef "$PLUGINS" ]; then
  warn "repo is the runtime dir ($PLUGINS) — skipping plugin copy (in-place layout)"
else
  COPY_FLAG=""; $NO_BACKUP && COPY_FLAG="--no-backup"
  # The copy is the FOUNDATION — every later stage runs from $PLUGINS/... A failed
  # copy cascades into a half-wired install, so surface the error and HARD-STOP
  # here rather than degrading to a yellow warning (H3). stderr is shown, not
  # swallowed.
  if COPY=$(REPO_ROOT="$REPO_ROOT" PLUGINS="$PLUGINS" "$PYBIN" "$BOOT_DIR/install_payload.py" $COPY_FLAG); then
    ok "plugin files copied → $PLUGINS$($NO_BACKUP && echo ' (no backup)')"
  else
    echo "$COPY" >&2
    die "plugin copy FAILED — aborting (later stages depend on $PLUGINS being populated)"
  fi
fi

hdr "Install: operator watchdog scripts"
# Copy <repo>/scripts/hscc_*.sh into ~/.hermes/scripts/. Non-fatal: watchdogs
# are operator convenience, not a foundation — if this step fails the rest of
# the install can still complete. User scripts in the runtime dir are
# preserved (only hscc_*.sh names are touched).
SCRIPTS_RUNTIME="${HERMES_SCRIPTS:-$HOME/.hermes/scripts}"
if SCOPY=$(REPO_ROOT="$REPO_ROOT" SCRIPTS="$SCRIPTS_RUNTIME" "$PYBIN" "$BOOT_DIR/install_scripts.py" $COPY_FLAG); then
  ok "watchdog scripts copied → $SCRIPTS_RUNTIME$($NO_BACKUP && echo ' (no backup)')"
else
  echo "$SCOPY" >&2
  warn "watchdog script copy reported issues — see scripts/README.md for manual install"
fi

hdr "Install: skills"
if $SKIP_SKILLS; then warn "skipped"; else
  "$PYBIN" "$PLUGINS/hscc-skills/hscc.py" install-skills >/dev/null 2>&1 && ok "skills installed" || warn "skills install reported issues"
fi

hdr "Install: role profiles"
if $SKIP_ROLES; then warn "skipped"; else
  "$PYBIN" "$PLUGINS/hscc-roles/hscc.py" generate >/dev/null 2>&1 && ok "role profiles generated" || warn "role generate reported issues"
fi

# Per-project orchestrators come from a SEPARATE bulk command (`orch-all`), not
# from `generate` (which only builds the role profiles from roles/*.yaml).
# Without this, a fresh bootstrap run produces ZERO per-project orchestrators —
# the whole per-project architecture is absent until someone runs `orch` by
# hand, once per project. `orch-all` provisions every registry project plus the
# `general` catch-all in one shot and is idempotent (re-running never clobbers
# an existing profile's memory/sessions). Inside the same --skip-roles guard so
# both role provision stages are skipped together. Non-fatal: a missing/unreadable
# registry still ensures `general`, and the step warns rather than dies.
hdr "Install: per-project orchestrators"
if $SKIP_ROLES; then warn "skipped"; else
  "$PYBIN" "$PLUGINS/hscc-roles/hscc.py" orch-all >/dev/null 2>&1 && ok "per-project orchestrators provisioned" || warn "orchestrator provisioning reported issues"
fi

hdr "Install: ~/.hscc state + serving.json"
mkdir -p "$HSCC_DIR"
[ -f "$HSCC_DIR/autonomy" ] || { echo off > "$HSCC_DIR/autonomy"; ok "autonomy flag seeded (off)"; }
SERVING="$HSCC_DIR/serving.json"
if [ -f "$SERVING" ] && ! $FORCE; then
  warn "serving.json exists — keeping it (use --force to regenerate)"
else
  [ -f "$SERVING" ] && cp "$SERVING" "$SERVING.bak-$(date +%Y%m%d-%H%M%S)"
  ORCH="$ORCH" RECIPE="$RECIPE" MODEL="$MODEL" SERVING="$SERVING" BOOT_DIR="$BOOT_DIR" "$PYBIN" -c "
import sys, os, json
sys.path.insert(0, os.environ['BOOT_DIR'])
import detect, serving_gen
cl = detect.detect_cluster()
s = serving_gen.build_serving(cl, orchestrator=os.environ['ORCH'],
                              recipe=os.environ['RECIPE'], model=os.environ['MODEL'],
                              port=8000, keepalive=True)
open(os.environ['SERVING'], 'w').write(json.dumps(s, indent=2) + '\n')
" && ok "serving.json written ($SERVING)" || warn "serving.json generation failed"
fi

hdr "Install: hermes/sparkrun patches"
# Reapply the curated upstream patches (kanban review + kanban_task_claimed
# resume hook live in hermes core, so the review/resume features need them on a
# fresh official install). Non-fatal: a machine already on a patched/newer
# hermes will report patches that don't re-apply — that's expected, not an error.
if $SKIP_PATCHES; then warn "skipped"; else
  HERMES_DIR="$HERMES_HOME/hermes-agent"
  CHK=$("$PYBIN" "$BOOT_DIR/apply_patches.py" --check 2>/dev/null)
  if echo "$CHK" | grep -q '"ok": true'; then
    "$PYBIN" "$BOOT_DIR/apply_patches.py" --target "$HERMES_DIR" --set hermes >/dev/null 2>&1 \
      && ok "hermes patches applied" || warn "hermes patch apply reported issues"
    [ -d "$HOME/sparkrun" ] && { "$PYBIN" "$BOOT_DIR/apply_patches.py" --target "$HOME/sparkrun" --set sparkrun >/dev/null 2>&1 \
      && ok "sparkrun patches applied" || warn "sparkrun patch apply reported issues"; }
  else
    warn "patches not cleanly applicable (already patched, or upstream drifted — run apply_patches.py --check to inspect)"
  fi
fi

hdr "Install: enable HSCC plugins + toolsets"
WIRED=$("$PYBIN" "$BOOT_DIR/enable_plugins.py" 2>/dev/null) && ok "config wired ($WIRED)" || warn "plugin/toolset enable reported issues"

hdr "Install: agent instructions (SOUL + ops personality)"
INSTR=$("$PYBIN" "$BOOT_DIR/install_soul.py" 2>/dev/null) && ok "$INSTR" || warn "SOUL/personality update reported issues"

hdr "Install: worker Telegram credential hygiene"
# Telegram is optional. Stripping seeded creds from worker role profiles is
# part of wiring Telegram CORRECTLY (only the `default` profile should hold the
# bot token). When Telegram is DECLINED we SKIP — never strip or destroy any
# operator's existing config. Clear ok/warn, never a silent skip or a hard die.
if [ "$TELEGRAM" != "yes" ]; then
  warn "telegram declined — skipping worker credential hygiene (existing Telegram config untouched)"
else
  # Comment out TELEGRAM_* vars in non-default role profiles so the multiplex
  # gateway does not log ~24 ERROR lines on each restart (0.19+). Non-fatal:
  # a failure here should warn, not die.
  if STRIP_JSON=$("$PYBIN" "$BOOT_DIR/strip_worker_telegram.py" 2>/dev/null); then
    STRIP_N=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['scanned'])" _ "$STRIP_JSON" 2>/dev/null || echo 0)
    ok "worker telegram creds stripped ($STRIP_N profiles)"
  else
    warn "strip_worker_telegram failed (profiles may still hold seeded Telegram creds)"
  fi
fi

hdr "Install: ensure kanban review feature in Hermes runtime"
# Re-apply the kanban_submit_review / auto_review feature (upstream PR #43425)
# if a hermes update dropped it. Idempotent + safe (refuses a dirty repo, aborts
# on conflict). No-op once the PR merges upstream. Non-fatal: warn, not die.
if REVIEW_JSON=$("$PYBIN" "$BOOT_DIR/ensure_review_feature.py" 2>/dev/null); then
  REVIEW_STATUS=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['status'])" _ "$REVIEW_JSON" 2>/dev/null || echo unknown)
  case "$REVIEW_STATUS" in
    already_present|applied|skipped_missing) ok "kanban review feature: $REVIEW_STATUS" ;;
    *) warn "kanban review feature not ensured ($REVIEW_STATUS) — auto_review may be inactive; see PR #43425" ;;
  esac
else
  warn "ensure_review_feature failed — auto_review may be inactive"
fi

hdr "Install: default trigger rules"
# Seed ~/.hscc/triggers.json with default alert rules (idempotent: only adds
# rules whose id is not already present, preserves operator edits). Non-fatal.
if TRIG_JSON=$("$PYBIN" "$BOOT_DIR/install_triggers.py" 2>/dev/null); then
  TRIG_TOTAL=$("$PYBIN" -c "import sys,json;d=json.loads(sys.argv[2]);print(d.get('total',0))" _ "$TRIG_JSON" 2>/dev/null || echo 0)
  TRIG_ADDED=$("$PYBIN" -c "import sys,json;d=json.loads(sys.argv[2]);print(len(d.get('added',[])))" _ "$TRIG_JSON" 2>/dev/null || echo 0)
  ok "trigger rules installed (${TRIG_TOTAL} total, added ${TRIG_ADDED})"
else
  warn "trigger rules install failed (~/.hscc/triggers.json may be missing default rules)"
fi

hdr "Install: daemon"
if $SKIP_DAEMON; then warn "skipped"; else
  if [ "$(uname -s)" = "Darwin" ]; then
    DAEMON_SETUP="$PLUGINS/hscc_daemon/launchd-setup.sh"
  else
    DAEMON_SETUP="$PLUGINS/hscc_daemon/systemd-setup.sh"
  fi
  bash "$DAEMON_SETUP" >/dev/null 2>&1 && ok "daemon installed + started" || warn "daemon setup reported issues (run $DAEMON_SETUP manually)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
hdr "Done"
ok "HSCC installed. Models are NOT running yet."
say "Bring a model up:  sparkrun run $RECIPE --cluster $CLUSTER_NAME --hosts $ORCH --port 8000 --ensure"
say "Start the Hermes gateway if not running, then message it to dispatch work."
say "Worker load-balancer: once worker models are up, the daemon starts the"
say "  sparkrun LiteLLM proxy on :4000 (role workers + subagents balance across"
say "  worker GPUs through it). Role profiles already point at it."
