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
PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

ASSUME_YES=false; FORCE=false
SKIP_SKILLS=false; SKIP_ROLES=false; SKIP_DAEMON=false
for a in "$@"; do case "$a" in
  --yes|-y) ASSUME_YES=true ;;
  --force) FORCE=true ;;
  --skip-skills) SKIP_SKILLS=true ;;
  --skip-roles) SKIP_ROLES=true ;;
  --skip-daemon) SKIP_DAEMON=true ;;
  --help|-h) echo "Usage: hscc-bootstrap [--yes] [--force] [--skip-skills|--skip-roles|--skip-daemon]"; exit 0 ;;
  *) echo "Unknown option: $a" >&2; exit 1 ;;
esac; done

say()  { echo "  $1"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
die()  { echo "  ✗ $1" >&2; exit 1; }
hdr()  { echo; echo "━━━ $1 ━━━"; }

# ── Stage 1: prerequisites (hard gate) ─────────────────────────────────────
hdr "Prerequisites"
command -v sparkrun >/dev/null 2>&1 || die "sparkrun not found in PATH. Install sparkrun first."
CLUSTER_JSON="$("$PYBIN" "$BOOT_DIR/detect.py" 2>/dev/null || echo null)"
[ "$CLUSTER_JSON" != "null" ] || die "No sparkrun cluster configured. Run: sparkrun cluster add <name> <hosts...>"
[ -d "$HERMES_HOME/hermes-agent" ] || die "Hermes not found at $HERMES_HOME/hermes-agent. Install Hermes first."
ok "sparkrun cluster configured"
ok "Hermes present at $HERMES_HOME/hermes-agent"
if pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then ok "Hermes gateway running"; else warn "Hermes gateway not running (start it after bootstrap)"; fi

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

# ── Stage 4: install ───────────────────────────────────────────────────────
hdr "Install: skills"
if $SKIP_SKILLS; then warn "skipped"; else
  "$PYBIN" "$PLUGINS/hscc-skills/hscc.py" install-skills >/dev/null 2>&1 && ok "skills installed" || warn "skills install reported issues"
fi

hdr "Install: role profiles"
if $SKIP_ROLES; then warn "skipped"; else
  "$PYBIN" "$PLUGINS/hscc-roles/hscc.py" generate >/dev/null 2>&1 && ok "role profiles generated" || warn "role generate reported issues"
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

hdr "Install: daemon"
if $SKIP_DAEMON; then warn "skipped"; else
  bash "$PLUGINS/hscc-daemon/launchd-setup.sh" >/dev/null 2>&1 && ok "daemon installed + started" || warn "daemon setup reported issues (run launchd-setup.sh manually)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
hdr "Done"
ok "HSCC installed. Models are NOT running yet."
say "Bring a model up:  sparkrun run $RECIPE --cluster $CLUSTER_NAME --hosts $ORCH --port 8000 --ensure"
say "Start the Hermes gateway if not running, then message it to dispatch work."
