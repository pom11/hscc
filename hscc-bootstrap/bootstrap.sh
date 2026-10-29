#!/bin/bash
# HSCC Bootstrap — preflight-gated, topology-detecting installer.
#   1. Prereq gate: sparkrun cluster configured + Hermes present (hard stop).
#   2. Detect cluster (nodes/user/NAS) from sparkrun.
#   3. Minimal interview (orchestrator node, recipe) — defaults + --yes.
#   4. Install: skills, role profiles, ~/.hscc + serving.json, daemon.
# No vLLM model provisioning. Idempotent.
# Works from ANY source root: a git checkout, the deployed plugins copy, or a
# brew libexec tree (the homebrew-hscc tap installs it at
# <prefix>/Cellar/hscc/<ver>/libexec/hscc — version-stable, no marker file).
# From a brew tree the CLI-shim stage is SKIPPED (brew owns `hscc` on PATH);
# `--dry-run` prints every planned write and performs none. Reachable as
# `hscc bootstrap [flags]` — flags pass through (t_bc5a3fee).
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HSCC_DIR="${HSCC_DIR:-$HOME/.hscc}"
PLUGINS="$HERMES_HOME/plugins"
RECIPES_DIR="${RECIPES_DIR:-$HOME/.sparkrun-local/recipes}"
BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BOOT_DIR/.." && pwd)"
PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

ASSUME_YES=false; FORCE=false; NO_BACKUP=false; DRY_RUN=false
SKIP_SKILLS=false; SKIP_ROLES=false; SKIP_DAEMON=false; SKIP_PATCHES=false; SKIP_CLI=false
SKIP_NODE_RECOVERY=false
for a in "$@"; do case "$a" in
  --yes|-y) ASSUME_YES=true ;;
  --dry-run) DRY_RUN=true ;;
  --force) FORCE=true ;;
  --no-backup) NO_BACKUP=true ;;
  --skip-skills) SKIP_SKILLS=true ;;
  --skip-roles) SKIP_ROLES=true ;;
  --skip-daemon) SKIP_DAEMON=true ;;
  --skip-patches) SKIP_PATCHES=true ;;
  --skip-cli) SKIP_CLI=true ;;
  --skip-node-recovery) SKIP_NODE_RECOVERY=true ;;
  --help|-h) echo "Usage: hscc-bootstrap [--yes] [--dry-run] [--force] [--no-backup] [--skip-skills|--skip-roles|--skip-daemon|--skip-patches|--skip-cli|--skip-node-recovery]"; echo "  --dry-run: print every planned write (skills, role profiles, ~/.hscc state + serving.json, plugins payload, node hardware watchdog, daemon, CLI shim) and perform NONE."; echo "  --skip-node-recovery: do not touch ANY fleet node (panic-to-reboot sysctls, hardware watchdog; the isolated-install tests use it; the fleet apply is an operator/orchestrator action)."; exit 0 ;;
  *) echo "Unknown option: $a" >&2; exit 1 ;;
esac; done

say()  { echo "  $1"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
die()  { echo "  ✗ $1" >&2; exit 1; }
hdr()  { echo; echo "━━━ $1 ━━━"; }
plan() { echo "  → plan: $1"; }

# ── brew libexec detection (t_bc5a3fee) ─────────────────────────────────────
# The brew tap (homebrew-hscc) extracts the release tarball under
# <prefix>/libexec/hscc-<ver>/ and puts `hscc` on PATH through brew itself.
# Primary signal: the marker file the tap writes at the extracted tree root
# (explicit, survives any prefix relocation). Fallback: the path shapes the
# tap uses (Cellar/…, or a libexec/hscc-* dir) for taps that ship without
# the marker. Effect: the CLI-shim stage below is SKIPPED — brew already
# owns `hscc` on PATH, and writing a shim into the Hermes venv would shadow
# it with a second, surprising entry point. Non-brew trees are unaffected.
HSCC_BREW_LIBEXEC=""
if [ -f "$REPO_ROOT/.hscc-brew-libexec" ]; then
  HSCC_BREW_LIBEXEC="$REPO_ROOT"
else
  case "$REPO_ROOT" in
    */Cellar/*|*/libexec/hscc-*) HSCC_BREW_LIBEXEC="$REPO_ROOT" ;;
  esac
fi

if $DRY_RUN; then
  echo "DRY RUN — every planned write is printed with a '→ plan:' marker; NONE is performed."
  [ -n "$HSCC_BREW_LIBEXEC" ] && say "brew libexec tree detected: $HSCC_BREW_LIBEXEC (CLI shim stage will be skipped)"
fi

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
# node_recovery.py takes a comma-separated host list (argv, never shell-eval'd);
# $HOSTS stays space-separated for everything else here.
HOSTS_CSV="$(printf '%s,' "${HOST_ARR[@]}")"; HOSTS_CSV="${HOSTS_CSV%,}"
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
if $DRY_RUN; then
  plan "copy plugin payload: $REPO_ROOT → $PLUGINS (install_payload.py; backup-then-overwrite$($NO_BACKUP && echo ', no backup'))"
elif [ "$REPO_ROOT" -ef "$PLUGINS" ]; then
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

hdr "Install: hscc CLI entry point (into the Hermes venv)"
# The `hscc` CLI reads the Hermes runtime from the SAME interpreter: the
# project chat/sessions/ask verbs import `hermes_cli`/`hermes_state` (which live
# ONLY in the Hermes venv), and `hscc check` needs the macOS Local Network (TCC)
# grant the Hermes venv carries. Installing the console script into ANY other
# interpreter (e.g. a bare conda p313) bakes a wrong shebang that silently
# degrades those verbs (HermesRuntimeUnavailable / errno 65). We install
# `hscc-cli` INTO the Hermes venv so pip/uv bakes the venv interpreter into the
# shebang BY CONSTRUCTION, then verify it and force-repair on drift. Install_cli
# is idempotent and REPORTS what it changed — no silent rewrites here. This is a
# foundation step: a wrong-shebang CLI is exactly the silent degradation this
# exists to prevent, so failure HARD-STOPS rather than degrading to a warning.
if $DRY_RUN; then
  if $SKIP_CLI; then
    plan "CLI shim: skipped (--skip-cli)"
  elif [ -n "$HSCC_BREW_LIBEXEC" ]; then
    plan "CLI shim: SKIPPED — brew libexec install already provides 'hscc' on PATH"
  else
    plan "CLI shim: install hscc-cli into the Hermes venv ($PYBIN) from $REPO_ROOT/hscc-cli (install_cli.py)"
  fi
elif [ -n "$HSCC_BREW_LIBEXEC" ]; then
  # t_bc5a3fee: a brew install owns `hscc` on PATH already. Writing a shim
  # into the Hermes venv would drop a SECOND entry point that shadows (or is
  # shadowed by) brew's, outside brew's control — surprising and unverifiable
  # by `brew`. Skip the stage; non-brew installs keep the behavior exactly.
  warn "CLI shim skipped — brew libexec install ($HSCC_BREW_LIBEXEC): 'hscc' already on PATH via brew"
elif $SKIP_CLI; then warn "skipped"; else
CLI_JSON=$("$PYBIN" "$BOOT_DIR/install_cli.py" "$PYBIN" "$REPO_ROOT/hscc-cli" 2>/dev/null)
if [ -z "$CLI_JSON" ]; then
  "$PYBIN" "$BOOT_DIR/install_cli.py" "$PYBIN" "$REPO_ROOT/hscc-cli" >&2
  die "hscc CLI install FAILED — see output above"
fi
# Emit a single actionable line: <action>|<shebang> (or <action>|<error>).
CLI_LINE=$(printf '%s' "$CLI_JSON" | "$PYBIN" -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('action','unknown') + '|' + (d.get('shebang') or d.get('error') or '?'))
")
CLI_ACTION="${CLI_LINE%%|*}"
CLI_DETAIL="${CLI_LINE#*|}"
case "$CLI_ACTION" in
  installed) ok "hscc CLI installed into Hermes venv ($CLI_DETAIL)" ;;
  verified)  ok "hscc CLI verified — shebang already on Hermes venv ($CLI_DETAIL)" ;;
  repaired)  ok "hscc CLI shebang repaired → Hermes venv ($CLI_DETAIL)" ;;
  *) echo "$CLI_JSON" >&2; die "hscc CLI install FAILED ($CLI_DETAIL) — a wrong-shebang CLI silently degrades project/chat/check" ;;
esac
fi

hdr "Install: operator watchdog scripts"
# Copy <repo>/scripts/hscc_*.sh into ~/.hermes/scripts/. Non-fatal: watchdogs
# are operator convenience, not a foundation — if this step fails the rest of
# the install can still complete. User scripts in the runtime dir are
# preserved (only hscc_*.sh names are touched).
SCRIPTS_RUNTIME="${HERMES_SCRIPTS:-$HOME/.hermes/scripts}"
if $DRY_RUN; then
  plan "watchdog scripts: copy $REPO_ROOT/scripts/hscc_*.sh → $SCRIPTS_RUNTIME (install_scripts.py)"
elif SCOPY=$(REPO_ROOT="$REPO_ROOT" SCRIPTS="$SCRIPTS_RUNTIME" "$PYBIN" "$BOOT_DIR/install_scripts.py" $COPY_FLAG); then
  ok "watchdog scripts copied → $SCRIPTS_RUNTIME$($NO_BACKUP && echo ' (no backup)')"
else
  echo "$SCOPY" >&2
  warn "watchdog script copy reported issues — see scripts/README.md for manual install"
fi

hdr "Install: git hooks (public-repo address guard)"
# Real operator LAN/tailnet addresses have reached tracked files TWICE on this
# PUBLIC repo — most recently a ledger tick with a verbatim `mount_nfs` command
# that reached origin/main on a docs-only commit. The pytest gate could see that
# leak but never ran for it, so the same detector now also runs at COMMIT time
# from the committed .githooks/pre-commit.
#
# A committed .githooks directory does nothing until core.hooksPath points at it
# (plain .git/hooks is not cloned, so it cannot be the mechanism). install_hooks.py
# sets it idempotently and writes to the COMMON git config, so every linked
# worktree — including the dispatcher's worker worktrees — inherits the hook from
# ONE install. Non-fatal: a machine whose repo is not a git checkout (or an older
# revision without the committed hooks) reports skipped and still installs.
if $DRY_RUN; then
  plan "git hooks: install_hooks.py sets core.hooksPath=.githooks in $REPO_ROOT (commit-time address guard)"
elif HK_JSON=$("$PYBIN" "$BOOT_DIR/install_hooks.py" "$REPO_ROOT" 2>/dev/null); then
  HK_ACTION=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['action'])" _ "$HK_JSON" 2>/dev/null || echo unknown)
  case "$HK_ACTION" in
    installed) ok "address-guard hook armed (core.hooksPath=.githooks)" ;;
    verified)  ok "address-guard hook already armed (core.hooksPath=.githooks)" ;;
    skipped)   warn "git hooks not armed: $(printf '%s' "$HK_JSON" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin).get("reason") or "?")' 2>/dev/null || echo '?')" ;;
    *)         warn "git hooks not armed ($HK_ACTION) — real addresses can reach a commit; run install_hooks.py manually" ;;
  esac
else
  warn "git hooks install failed — the commit-time address guard is INACTIVE; run: $PYBIN $BOOT_DIR/install_hooks.py"
fi

hdr "Install: skills"
if $SKIP_SKILLS; then warn "skipped"
elif $DRY_RUN; then plan "skills: $PLUGINS/hscc-skills/hscc.py install-skills (writes $HERMES_HOME/skills)"
else
  "$PYBIN" "$PLUGINS/hscc-skills/hscc.py" install-skills >/dev/null 2>&1 && ok "skills installed" || warn "skills install reported issues"
fi

hdr "Install: role profiles"
if $SKIP_ROLES; then warn "skipped"
elif $DRY_RUN; then plan "role profiles: $PLUGINS/hscc-roles/hscc.py generate (writes $HERMES_HOME/profiles)"
else
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
  if $DRY_RUN; then plan "per-project orchestrators: $PLUGINS/hscc-roles/hscc.py orch-all (writes $HERMES_HOME/profiles)"
  else
    "$PYBIN" "$PLUGINS/hscc-roles/hscc.py" orch-all >/dev/null 2>&1 && ok "per-project orchestrators provisioned" || warn "orchestrator provisioning reported issues"
  fi
fi

# Guarded review-skill fan-out (t_bc611809): the dispatcher force-injects
# sdlc-review for review-lane spawns for ANY assignee, so every reviewer-capable
# profile must resolve it exactly once. install_review_skill.py is collision-
# guarded (skips wherever the name already resolves elsewhere), idempotent,
# never deletes and never overwrites a resolving copy, and --dry-run plans
# without writing. Warn-not-die: a residual collision is operator data to
# adjudicate (the doctor's two skill findings report it with every path).
#
# STAGE ORDER IS LOAD-BEARING (B1, round-2 review): this stage MUST run AFTER
# both profile-provisioning stages above. `generate` and `orch-all` CREATE the
# role profiles and every `*-orch` — with EMPTY skills trees — so a fan-out
# that ran before them saw zero reviewer-capable profiles, installed only the
# root home, and shipped freshly-created orchestrators that die on the very
# first review-lane spawn ('Unknown skill(s): sdlc-review'). The stage is
# idempotent, so running it here costs a no-op for homes that already resolve.
hdr "Install: review skills (guarded)"
if $SKIP_SKILLS; then warn "skipped"
elif $DRY_RUN; then plan "review skills: $BOOT_DIR/install_review_skill.py --skill all (guarded fan-out into $HERMES_HOME profiles)"
else
  "$PYBIN" "$BOOT_DIR/install_review_skill.py" --skill all >/dev/null 2>&1 \
    && ok "review skills guarded-installed" \
    || warn "review skills: unresolved profile(s) remain — run: $PYBIN $BOOT_DIR/install_review_skill.py --json (per-profile verdicts) and doctor.py --skills"
fi

hdr "Install: ~/.hscc state + serving.json"
if $DRY_RUN; then
  [ -d "$HSCC_DIR" ] || plan "mkdir $HSCC_DIR"
  [ -f "$HSCC_DIR/autonomy" ] || plan "seed $HSCC_DIR/autonomy (off)"
  [ -f "$HSCC_DIR/autodown.json" ] || plan "seed $HSCC_DIR/autodown.json (disabled — preserve_autodown.py)"
  if [ -f "$HSCC_DIR/serving.json" ] && ! $FORCE; then
    plan "serving.json exists — would keep it (use --force to regenerate)"
  else
    plan "write $HSCC_DIR/serving.json (orch=$ORCH recipe=${RECIPE:-<none>} model=${MODEL:-<unknown>})"
  fi
else
mkdir -p "$HSCC_DIR"
[ -f "$HSCC_DIR/autonomy" ] || { echo off > "$HSCC_DIR/autonomy"; ok "autonomy flag seeded (off)"; }
hdr "Install: autodown.json (operator state — preserve, never clobber)"
# ~/.hscc/autodown.json is OPERATOR state, not installable payload: it decides
# whether the daemon may tear down the whole fleet. Bootstrap must never flip
# existing `enabled`/`idle_minutes`, and must seed a fresh install DISABLED
# (opt-in per C5). preserve_autodown.py handles the preserve-vs-seed logic; we
# only surface a clear ok line for each case. Non-fatal: warn, not die.
if AD_JSON=$("$PYBIN" "$BOOT_DIR/preserve_autodown.py" 2>/dev/null); then
  AD_ACTION=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['action'])" _ "$AD_JSON" 2>/dev/null || echo unknown)
  if [ "$AD_ACTION" = "seeded" ]; then
    ok "autodown: seeded disabled (opt-in)"
  else
    AD_EN=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['enabled'])" _ "$AD_JSON" 2>/dev/null || echo '?')
    AD_MIN=$("$PYBIN" -c "import sys,json;print(json.loads(sys.argv[2])['idle_minutes'])" _ "$AD_JSON" 2>/dev/null || echo '?')
    ok "autodown: existing config preserved (enabled=$AD_EN, idle_minutes=$AD_MIN)"
  fi
else
  warn "autodown install failed — ~/.hscc/autodown.json left untouched"
fi
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
fi

hdr "Install: node hardware watchdog (self-recovery)"
# Every cluster node gets systemd's runtime watchdog armed (a drop-in with
# RuntimeWatchdogSec=30s + daemon-reexec) so a hard hang resets the SoC instead
# of wedging until someone power-cycles it — Sparks have no BMC. Grounded in the
# stage that already touches nodes: this runs the deployed
# $PLUGINS/hscc-cluster/node_recovery.py, whose ONE transport is sparkrun's own
# remote execution (this script never ssh's, and passes hosts as argv).
#
# Deliberately NON-FATAL: a node that refuses sudo, has no watchdog driver, or
# is down must never abort an install. But it must never read as installed
# either — a protected fleet is the whole point, so an unprotected node prints
# WARN here and the doctor's watchdog check keeps printing RED until fixed.
# Opt out: node_recovery.watchdog=false in ~/.hscc/cluster.json, or the flag.
if $SKIP_NODE_RECOVERY || [ -n "${HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY:-}" ]; then
  warn "skipped (--skip-node-recovery${HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY:+ / HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY})"
elif $DRY_RUN; then
  plan "node hardware watchdog: node_recovery.py --hosts $HOSTS_CSV (systemd drop-in + daemon-reexec on each node; writes NOTHING in dry-run)"
elif [ ! -f "$PLUGINS/hscc-cluster/node_recovery.py" ]; then
  warn "node_recovery.py missing from $PLUGINS — cluster nodes are NOT protected (see hscc-bootstrap/README.md)"
elif NR_OUT=$("$PYBIN" "$PLUGINS/hscc-cluster/node_recovery.py" --hosts "$HOSTS_CSV" 2>&1); then
  printf '%s\n' "$NR_OUT" | sed 's/^/  /'
  ok "hardware watchdog armed on every cluster node"
else
  printf '%s\n' "$NR_OUT" | sed 's/^/  /'
  warn "hardware watchdog NOT armed on every node — an unprotected node stays wedged until someone power-cycles it; see hscc doctor"
fi

hdr "Install: hermes/sparkrun patches"
# Reapply the curated upstream patches (kanban review + kanban_task_claimed
# resume hook live in hermes core, so the review/resume features need them on a
# fresh official install). Non-fatal: a machine already on a patched/newer
# hermes will report patches that don't re-apply — that's expected, not an error.
if $SKIP_PATCHES; then warn "skipped"
elif $DRY_RUN; then plan "patches: apply_patches.py --target $HERMES_HOME/hermes-agent --set hermes (+ sparkrun set when ~/sparkrun exists)"
else
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
# Seed the general plugin/toolset wiring in $HERMES_HOME/config.yaml
# (plugins.enabled, toolsets, kanban, delegation, compaction, fallback
# provider). enable_plugins.py reads HERMES_HOME itself.
#
# It is a deliberate NO-OP when config.yaml does not exist yet — Hermes owns
# that file and creates it on first run — so it reports "NOT WIRED" instead of
# exiting non-zero. Treat that as a warning, not success: reporting
# "✓ config wired" for a fresh machine where nothing was wired is exactly the
# silent gap this step exists to close.
if $DRY_RUN; then
  plan "config wiring: enable_plugins.py updates $HERMES_HOME/config.yaml (plugins.enabled, toolsets, kanban, delegation, compaction)"
elif WIRED=$("$PYBIN" "$BOOT_DIR/enable_plugins.py" 2>/dev/null); then
  case "$WIRED" in
    *"NOT WIRED"*) warn "$WIRED" ;;
    *)             ok "config wired ($WIRED)" ;;
  esac
else
  warn "plugin/toolset enable reported issues"
fi

hdr "Install: agent instructions (SOUL + ops personality)"
if $DRY_RUN; then
  plan "SOUL/personality: install_soul.py updates $HERMES_HOME/SOUL.md + personality (sentinel blocks)"
else
INSTR=$("$PYBIN" "$BOOT_DIR/install_soul.py" 2>/dev/null) && ok "$INSTR" || warn "SOUL/personality update reported issues"
fi

hdr "Install: ensure kanban review feature in Hermes runtime"
# Re-apply the kanban_submit_review / auto_review feature (upstream PR #43425)
# if a hermes update dropped it. Idempotent + safe (refuses a dirty repo, aborts
# on conflict). No-op once the PR merges upstream. Non-fatal: warn, not die.
if $DRY_RUN; then
  plan "kanban review feature: ensure_review_feature.py re-applies PR #43425 into $HERMES_HOME/hermes-agent if missing"
elif REVIEW_JSON=$("$PYBIN" "$BOOT_DIR/ensure_review_feature.py" 2>/dev/null); then
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
if $DRY_RUN; then
  plan "trigger rules: install_triggers.py seeds defaults into $HSCC_DIR/triggers.json"
elif TRIG_JSON=$("$PYBIN" "$BOOT_DIR/install_triggers.py" 2>/dev/null); then
  TRIG_TOTAL=$("$PYBIN" -c "import sys,json;d=json.loads(sys.argv[2]);print(d.get('total',0))" _ "$TRIG_JSON" 2>/dev/null || echo 0)
  TRIG_ADDED=$("$PYBIN" -c "import sys,json;d=json.loads(sys.argv[2]);print(len(d.get('added',[])))" _ "$TRIG_JSON" 2>/dev/null || echo 0)
  ok "trigger rules installed (${TRIG_TOTAL} total, added ${TRIG_ADDED})"
else
  warn "trigger rules install failed (~/.hscc/triggers.json may be missing default rules)"
fi

hdr "Install: node-recovery sysctls (panic-to-reboot)"
# A node that hard-froze on 2026-10-27 had to be power-cycled by hand: Sparks
# have no BMC, and with kernel.panic=0 a kernel-level wedge never self-resets.
# This stage installs /etc/sysctl.d/90-hscc-autoreboot.conf on every cluster
# node over the sparkrun ssh path (root via sudo -n), verifies every value by
# READ-BACK, and never clobbers an operator-edited file (drift is reported,
# the doctor surfaces it red). Opt out with
# ~/.hscc/cluster.json -> {"node_recovery": {"autoreboot": false}}.
# Non-fatal: an unreachable/refusing node warns loudly with per-node detail;
# the rest of the install (and the other nodes) still complete.
if $SKIP_NODE_RECOVERY; then warn "skipped (--skip-node-recovery)"
elif $DRY_RUN; then
  plan "node-recovery sysctls: install_autoreboot.py --hosts $HOSTS (drop-in + sysctl --system + read-back; --dry-run writes nothing on any node)"
else
  # 2>&1 keeps a transport traceback visible in the report instead of lost;
  # the summariser below picks the LAST line that parses as JSON, so noise
  # cannot turn a good result into a false "not verified".
  if AR_JSON=$("$PYBIN" "$BOOT_DIR/install_autoreboot.py" --hosts "${HOST_ARR[@]}" 2>&1); then
    AR_STATUS=$("$PYBIN" -c '
import json, sys
raw = sys.argv[1]
d = None
for line in reversed(raw.splitlines()):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except ValueError:
        continue
    break
if not isinstance(d, dict):
    print("WARN|unparseable output — run: hscc-bootstrap/install_autoreboot.py --hosts ... manually")
    sys.exit(0)
if d.get("skipped_reason"):
    print("OK|node-recovery sysctls skipped — " + str(d["skipped_reason"]))
    sys.exit(0)
rs = d.get("results") or []
armed = [r for r in rs if r["action"] in ("installed", "unchanged", "live-reapplied")]
drift = [r for r in rs if r["action"] == "drift"]
failed = [r for r in rs if r["action"] == "failed"]
summary = ("node-recovery sysctls armed+read-back-verified on %d/%d node(s) "
           "(panic=10, panic_on_oops=1, softlockup_panic=1, hung_task_panic=1, "
           "hung_task_timeout_secs=600)" % (len(armed), len(rs)))
# Honest signals: a stage where ANY node failed or nothing got armed leads with
# WARN — an OK line here would present a broken fleet as protected.
if failed or not armed:
    print("WARN|" + summary)
else:
    print("OK|" + summary)
for r in drift:
    print("WARN|%s: %s" % (r.get("host"), r.get("note") or "drop-in drifted"))
for r in failed:
    print("WARN|%s: %s" % (r.get("host"), r.get("error") or "failed"))
if not rs:
    print("WARN|no nodes were checked — is the sparkrun cluster configured?")
elif failed:
    print("WARN|some nodes FAILED — those nodes can still wedge without rebooting")
' "$AR_JSON" 2>/dev/null)
    [ -z "$AR_STATUS" ] && AR_STATUS="WARN|summary unavailable — run: $PYBIN $BOOT_DIR/install_autoreboot.py --hosts $HOSTS"
    while IFS= read -r AR_LINE; do
      case "$AR_LINE" in
        OK\|*)   ok "${AR_LINE#OK|}" ;;
        WARN\|*) warn "${AR_LINE#WARN|}" ;;
        *)       warn "$AR_LINE" ;;
      esac
    done <<< "$AR_STATUS"
  else
    warn "node-recovery sysctls FAILED to run — nodes may still never reboot on a kernel wedge. Run: $PYBIN $BOOT_DIR/install_autoreboot.py --hosts $HOSTS"
  fi
fi

hdr "Install: tmux (iOS chat reachability)"
# The iOS app types into the project's REPL *pane* (hscc-api/repl_inject),
# so tmux is not optional for the phone → terminal path: a REPL started
# outside tmux is invisible to the app (messages would go to a hidden
# serve session). bootstrap checks the tool exists and shows the current
# REPL split — which chats the phone can reach RIGHT NOW (IN_TMUX) and
# which it cannot (OUTSIDE_TMUX). Report-only: installing tmux stays the
# operator's call (brew), same posture as the launchd opt-in below.
if ! command -v tmux >/dev/null 2>&1; then
  warn "tmux is NOT installed — the iOS app cannot deliver into any REPL (brew install tmux)"
else
  ok "tmux present ($(command -v tmux)) — chat opened OUTSIDE tmux would be invisible to the iOS app"
  # One matcher for both doors: the helper loads the installed
  # flightdeck/core/tmux_chat.py (same code `hscc project chat` uses to
  # decide where the chat opens), so bootstrap never re-implements ps/tmux
  # discovery and cannot drift from it.
  TMUX_REPL_JSON=$("$PYBIN" "$BOOT_DIR/tmux_repl_report.py" "$REPO_ROOT" 2>/dev/null) \
    || TMUX_REPL_JSON=""
  if [ -z "$TMUX_REPL_JSON" ]; then
    warn "REPL/tmux visibility report unavailable (run: $PYBIN $BOOT_DIR/tmux_repl_report.py $REPO_ROOT)"
  elif printf '%s' "$TMUX_REPL_JSON" | grep -q '^TMUX_UNVERIFIED '; then
    # The helper itself could not consult tmux/ps — say so, never render an
    # unverifiable world as "no REPLs running".
    warn "REPL/tmux visibility UNVERIFIED: $(printf '%s\n' "$TMUX_REPL_JSON" | grep '^TMUX_UNVERIFIED ' | head -1)"
  else
    IN_N=$(printf '%s\n' "$TMUX_REPL_JSON" | grep -c '^IN_TMUX ' || true)
    OUT_N=$(printf '%s\n' "$TMUX_REPL_JSON" | grep -c '^OUTSIDE_TMUX ' || true)
    [ "$IN_N" -gt 0 ]  && ok  "REPLs in tmux (iOS can reach them): $(printf '%s\n' "$TMUX_REPL_JSON" | grep '^IN_TMUX ' | awk '{print $2}' | paste -sd ' ' -)"
    [ "$OUT_N" -gt 0 ] && warn "REPLs OUTSIDE tmux (iOS messages would MISS them — reopen with 'hscc project chat <project>'): $(printf '%s\n' "$TMUX_REPL_JSON" | grep '^OUTSIDE_TMUX ' | awk '{print $2}' | paste -sd ' ' -)"
    [ "$IN_N" -eq 0 ] && [ "$OUT_N" -eq 0 ] && ok "no REPL chats running right now — 'hscc project chat <project>' opens one inside tmux"
  fi
fi

hdr "Install: API boot-resilience (opt-in)"
# Nothing is written or loaded here. The API normally runs from a PID-file
# start that does NOT survive a reboot (iPhone goes offline). `hscc api
# install-service` writes a launchd unit when the operator wants boot+crash
# resilience; bootstrap only reports the current state and the command.
if [ "$(uname -s)" = "Darwin" ]; then
  if [ -f "$HOME/Library/LaunchAgents/com.hscc.api.plist" ]; then
    ok "launchd unit present (opt-in already taken) — check: launchctl print gui/\$UID/com.hscc.api"
  else
    ok "launchd unit not installed (API won't survive reboot) — opt in: hscc api install-service"
  fi
fi

hdr "Install: daemon"
if $SKIP_DAEMON; then warn "skipped"; else
  if $DRY_RUN; then
    # (t_bc5a3fee) Wording stays free of the helper's literal invocation path:
    # the restart-wiring pins (test_restart_daemon.py) count occurrences of it.
    if [ "$(uname -s)" = "Darwin" ]; then
      plan "daemon: bash $PLUGINS/hscc_daemon/launchd-setup.sh (writes ~/Library/LaunchAgents) + daemon restart"
    else
      plan "daemon: bash $PLUGINS/hscc_daemon/systemd-setup.sh (writes the systemd user unit) + daemon restart"
    fi
  else
    if [ "$(uname -s)" = "Darwin" ]; then
      DAEMON_SETUP="$PLUGINS/hscc_daemon/launchd-setup.sh"
    else
      DAEMON_SETUP="$PLUGINS/hscc_daemon/systemd-setup.sh"
    fi
    bash "$DAEMON_SETUP" >/dev/null 2>&1 && ok "daemon installed + started" || warn "daemon setup reported issues (run $DAEMON_SETUP manually)"

    # Restart the daemon so freshly-installed code is ALWAYS loaded. The setup
    # script above leaves an ALREADY-RUNNING daemon alone, so without this the new
    # plugin code never loads (observed live: an 8-day-old process running while
    # the fresh autodown loop sat unloaded — cost real debugging time twice).
    # Non-fatal: a failed restart warns but never aborts the install. Safety: the
    # new daemon re-runs autodown.resume_from_restart on startup, so a restart can
    # never strand a down/waking fleet, and we never touch autodown/watchdog state.
    if RESTART_OUT=$("$BOOT_DIR/restart_daemon.sh" 2>&1); then
      ok "$RESTART_OUT"
    else
      warn "$RESTART_OUT"
    fi
  fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
hdr "Done"
if $DRY_RUN; then
  ok "DRY RUN complete — NOTHING was installed (every planned write is above, marked '→ plan:')."
  say "Re-run without --dry-run to perform these writes."
else
ok "HSCC installed. Models are NOT running yet."
say "Bring a model up:  sparkrun run $RECIPE --cluster $CLUSTER_NAME --hosts $ORCH --port 8000 --ensure"
say "Start the Hermes gateway if not running, then message it to dispatch work."
say "Worker load-balancer: once worker models are up, the daemon starts the"
say "  sparkrun LiteLLM proxy on :4000 (role workers + subagents balance across"
say "  worker GPUs through it). Role profiles already point at it."
fi
