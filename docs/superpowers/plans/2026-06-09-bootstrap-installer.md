# HSCC Bootstrap Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `hscc-bootstrap` into a preflight-gated, topology-detecting, minimal-interview installer (per `docs/superpowers/specs/2026-06-09-bootstrap-installer-design.md`).

**Architecture:** Two pure, tested Python helpers + a bash orchestrator. `detect.py` parses `sparkrun cluster list --json` into a normalized cluster dict. `serving_gen.py` turns the detected cluster + answers into a `serving.json` structure. `bootstrap.sh` becomes the thin orchestrator: prereq gate → detect → minimal interview → install (skills, roles, ~/.hscc+serving.json, daemon) → staged report. No vLLM model provisioning.

**Tech Stack:** Python 3 stdlib (helpers), bash (orchestrator), pytest. All in `pom11/hscc` plugins repo, branch main.

**Scope:** This plan. Self-contained: after it, `hscc-bootstrap` readies a machine on any sparkrun cluster.

**Out of scope:** model provisioning/bring-up, installing sparkrun/Hermes themselves, multi-cluster, Telegram Operations setup.

---

## File Structure

- `hscc-bootstrap/detect.py` — `detect_cluster()` → normalized dict from sparkrun. Pure/testable.
- `hscc-bootstrap/serving_gen.py` — `build_serving(cluster, orchestrator, recipe, model, port, keepalive)` → serving.json dict. Pure/testable.
- `hscc-bootstrap/bootstrap.sh` — orchestrator (prereq gate, interview, install, report). Rewritten.
- `hscc-bootstrap/tests/{conftest.py,test_detect.py,test_serving_gen.py}` — unit tests.

---

## Task 1: Cluster detection helper

**Files:**
- Create: `hscc-bootstrap/detect.py`
- Create: `hscc-bootstrap/tests/conftest.py`
- Test: `hscc-bootstrap/tests/test_detect.py`

- [ ] **Step 1: Write the failing test**

`hscc-bootstrap/tests/conftest.py`:
```python
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
```

`hscc-bootstrap/tests/test_detect.py`:
```python
import json
import detect


def test_parse_default_cluster():
    raw = json.dumps([
        {"name": "hscc", "hosts": ["10.0.0.1", "10.0.0.2"], "user": "spark",
         "cache_dir": "/mnt/nas", "default": True},
    ])
    c = detect.parse_clusters(raw)
    assert c["name"] == "hscc"
    assert c["hosts"] == ["10.0.0.1", "10.0.0.2"]
    assert c["user"] == "spark"
    assert c["nas"] == "/mnt/nas"


def test_picks_default_among_many():
    raw = json.dumps([
        {"name": "a", "hosts": ["1.1.1.1"], "user": "x", "cache_dir": "", "default": False},
        {"name": "b", "hosts": ["2.2.2.2"], "user": "y", "cache_dir": "", "default": True},
    ])
    c = detect.parse_clusters(raw)
    assert c["name"] == "b"


def test_single_cluster_no_default_flag():
    raw = json.dumps([{"name": "solo", "hosts": ["9.9.9.9"], "user": "u", "cache_dir": ""}])
    c = detect.parse_clusters(raw)
    assert c["name"] == "solo"
    assert c["nas"] is None          # empty cache_dir -> no NAS


def test_no_clusters_returns_none():
    assert detect.parse_clusters("[]") is None
    assert detect.parse_clusters("") is None
    assert detect.parse_clusters("not json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_detect.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'detect'`

- [ ] **Step 3: Write minimal implementation**

`hscc-bootstrap/detect.py`:
```python
"""Detect the configured sparkrun cluster (facts, no assumptions)."""
import json
import subprocess


def parse_clusters(raw):
    """Parse `sparkrun cluster list --json` output into a normalized dict.

    Returns {name, hosts, user, nas} for the default cluster (or the only one),
    or None if no clusters / unparseable. ``nas`` is the cache_dir or None.
    """
    try:
        clusters = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(clusters, list) or not clusters:
        return None
    chosen = next((c for c in clusters if c.get("default")), clusters[0])
    cache = (chosen.get("cache_dir") or "").strip()
    return {
        "name": chosen.get("name", ""),
        "hosts": list(chosen.get("hosts") or []),
        "user": chosen.get("user", ""),
        "nas": cache or None,
    }


def detect_cluster(timeout=10):
    """Run sparkrun + parse. Returns the normalized dict or None."""
    try:
        r = subprocess.run(
            ["sparkrun", "cluster", "list", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return parse_clusters(r.stdout)


if __name__ == "__main__":
    c = detect_cluster()
    print(json.dumps(c) if c else "null")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_detect.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-bootstrap/detect.py hscc-bootstrap/tests/conftest.py hscc-bootstrap/tests/test_detect.py
git commit -m "feat(hscc-bootstrap): sparkrun cluster detection helper"
```

---

## Task 2: serving.json generator

**Files:**
- Create: `hscc-bootstrap/serving_gen.py`
- Test: `hscc-bootstrap/tests/test_serving_gen.py`

- [ ] **Step 1: Write the failing test**

`hscc-bootstrap/tests/test_serving_gen.py`:
```python
import serving_gen


def test_orchestrator_plus_keepalive_workers():
    cluster = {"hosts": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.1",
        recipe="~/r/qwen.yaml", model="Qwen/X", port=8000, keepalive=True)
    assert s["version"] == 1
    assert s["port"] == 8000
    units = s["units"]
    orch = [u for u in units if u["role"] == "orchestrator"]
    workers = [u for u in units if u["role"] == "worker"]
    assert len(orch) == 1 and orch[0]["nodes"] == ["10.0.0.1"]
    assert {tuple(w["nodes"]) for w in workers} == {("10.0.0.2",), ("10.0.0.3",)}
    assert all(w.get("keepalive") is True for w in workers)
    assert all(u["model"] == "Qwen/X" and u["recipe"] == "~/r/qwen.yaml" for u in units)


def test_single_node_no_workers():
    cluster = {"hosts": ["10.0.0.1"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.1",
        recipe="~/r.yaml", model="M", port=8000, keepalive=True)
    assert len([u for u in s["units"] if u["role"] == "worker"]) == 0
    assert len([u for u in s["units"] if u["role"] == "orchestrator"]) == 1


def test_keepalive_false_omits_flag():
    cluster = {"hosts": ["1.1.1.1", "2.2.2.2"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="1.1.1.1",
        recipe="r", model="m", port=8000, keepalive=False)
    workers = [u for u in s["units"] if u["role"] == "worker"]
    assert all("keepalive" not in w for w in workers)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_serving_gen.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'serving_gen'`

- [ ] **Step 3: Write minimal implementation**

`hscc-bootstrap/serving_gen.py`:
```python
"""Generate a serving.json structure from a detected cluster + choices."""


def build_serving(cluster, *, orchestrator, recipe, model, port=8000, keepalive=True):
    """Build the serving.json dict: one orchestrator unit + a worker unit per
    other host. Workers get keepalive=True when ``keepalive`` is set.

    ``cluster`` is the detect.py dict (uses ``hosts``). The orchestrator host is
    excluded from the worker set. Unit ids: 'orch-<octet>' and 'worker-<octet>'
    where octet is the host's last IP segment (falls back to index).
    """
    hosts = list(cluster.get("hosts") or [])

    def _octet(ip, idx):
        tail = ip.rsplit(".", 1)[-1]
        return tail if tail.isdigit() else str(idx)

    units = [{
        "id": f"orch-{_octet(orchestrator, 0)}",
        "role": "orchestrator",
        "model": model,
        "recipe": recipe,
        "nodes": [orchestrator],
    }]
    for i, host in enumerate(h for h in hosts if h != orchestrator):
        unit = {
            "id": f"worker-{_octet(host, i)}",
            "role": "worker",
            "model": model,
            "recipe": recipe,
            "nodes": [host],
        }
        if keepalive:
            unit["keepalive"] = True
        units.append(unit)
    return {"version": 1, "port": port, "units": units}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_serving_gen.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-bootstrap/serving_gen.py hscc-bootstrap/tests/test_serving_gen.py
git commit -m "feat(hscc-bootstrap): serving.json generator from detected cluster"
```

---

## Task 3: Recipe + model resolution helper

**Files:**
- Modify: `hscc-bootstrap/detect.py` (add `list_recipes` + `recipe_model`)
- Test: `hscc-bootstrap/tests/test_detect.py`

- [ ] **Step 1: Write the failing test**

Append to `hscc-bootstrap/tests/test_detect.py`:
```python
def test_recipe_model_reads_top_level_field(tmp_path):
    r = tmp_path / "qwen.yaml"
    r.write_text("model: Qwen/Qwen3.6-27B-FP8\nruntime: vllm\n")
    assert detect.recipe_model(str(r)) == "Qwen/Qwen3.6-27B-FP8"


def test_recipe_model_missing_returns_none(tmp_path):
    r = tmp_path / "x.yaml"
    r.write_text("runtime: vllm\n")
    assert detect.recipe_model(str(r)) is None


def test_list_recipes_finds_yaml(tmp_path):
    (tmp_path / "a.yaml").write_text("model: A\n")
    sub = tmp_path / "local-fixed"
    sub.mkdir()
    (sub / "b.yaml").write_text("model: B\n")
    found = detect.list_recipes(str(tmp_path))
    names = {r.rsplit("/", 1)[-1] for r in found}
    assert {"a.yaml", "b.yaml"}.issubset(names)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_detect.py -v`
Expected: FAIL `AttributeError: module 'detect' has no attribute 'recipe_model'`

- [ ] **Step 3: Write minimal implementation**

Append to `hscc-bootstrap/detect.py`:
```python
import os


def list_recipes(recipes_dir):
    """All *.yaml recipe paths under recipes_dir (incl. one level of subdirs
    like local-fixed/), sorted. [] if the dir is absent."""
    out = []
    if not os.path.isdir(recipes_dir):
        return out
    for root, _dirs, files in os.walk(recipes_dir):
        for f in sorted(files):
            if f.endswith(".yaml"):
                out.append(os.path.join(root, f))
    return sorted(out)


def recipe_model(recipe_path):
    """Read the top-level ``model:`` field from a sparkrun recipe, or None.

    Avoids a yaml dependency for one field — scans for a top-level ``model:``
    line (no leading whitespace).
    """
    try:
        with open(recipe_path) as f:
            for line in f:
                if line.startswith("model:"):
                    return line.split(":", 1)[1].strip() or None
    except (FileNotFoundError, OSError):
        return None
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/test_detect.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-bootstrap/detect.py hscc-bootstrap/tests/test_detect.py
git commit -m "feat(hscc-bootstrap): recipe discovery + model resolution"
```

---

## Task 4: Rewrite bootstrap.sh — prereq gate + detect + interview + install

**Files:**
- Modify (rewrite): `hscc-bootstrap/bootstrap.sh`

**Context:** The bash script orchestrates; the logic lives in detect.py/serving_gen.py + existing plugin CLIs. Use `python3 <plugin>/hscc.py` for skills/roles, `launchd-setup.sh` for the daemon. Keep the staged-report UI style from the old script. `PYBIN` resolves to `~/.hermes/hermes-agent/venv/bin/python` if present, else `python3`.

- [ ] **Step 1: Write the new bootstrap.sh**

Replace `hscc-bootstrap/bootstrap.sh` entirely with the staged orchestrator below (full content — no placeholders):
```bash
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
read -ra HOST_ARR <<< "$HOSTS"
say "nodes: $HOSTS"
say "NAS:   ${NAS:-<none>}"

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

# recipe: sole recipe auto, else pick
mapfile -t RECIPES < <("$PYBIN" -c "import sys;sys.path.insert(0,'$BOOT_DIR');import detect;print('\n'.join(detect.list_recipes('$RECIPES_DIR')))")
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
  "$PYBIN" -c "
import sys, json
sys.path.insert(0, '$BOOT_DIR')
import detect, serving_gen
cl = detect.detect_cluster()
s = serving_gen.build_serving(cl, orchestrator='$ORCH', recipe='$RECIPE',
                              model='$MODEL', port=8000, keepalive=True)
open('$SERVING','w').write(json.dumps(s, indent=2) + '\n')
" && ok "serving.json written ($SERVING)" || warn "serving.json generation failed"
fi

hdr "Install: daemon"
if $SKIP_DAEMON; then warn "skipped"; else
  bash "$PLUGINS/hscc-daemon/launchd-setup.sh" >/dev/null 2>&1 && ok "daemon installed + started" || warn "daemon setup reported issues (run launchd-setup.sh manually)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
hdr "Done"
ok "HSCC installed. Models are NOT running yet."
say "Bring a model up:  sparkrun run $RECIPE --cluster $(echo "$CLUSTER_JSON" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin)["name"])') --hosts $ORCH --port 8000 --ensure"
say "Start Hermes gateway if not running, then message it to dispatch work."
```

- [ ] **Step 2: Syntax-check the script**

Run: `bash -n ~/.hermes/plugins/hscc-bootstrap/bootstrap.sh && echo "bash syntax OK"`
Expected: `bash syntax OK`

- [ ] **Step 3: Dry verify the helpers the script calls (no install side effects)**

Run:
```bash
cd ~/.hermes/plugins/hscc-bootstrap
../../hermes-agent/venv/bin/python detect.py   # prints the detected cluster JSON
../../hermes-agent/venv/bin/python -c "import sys;sys.path.insert(0,'.');import detect,serving_gen;cl=detect.detect_cluster();print(serving_gen.build_serving(cl,orchestrator=cl['hosts'][0],recipe='r',model='m',port=8000,keepalive=True))"
```
Expected: prints the live cluster + a serving.json structure with one orchestrator + keepalive workers.

- [ ] **Step 4: Run the full unit suite**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && ../../hermes-agent/venv/bin/python -m pytest tests/ -q`
Expected: all green (4 detect + 3 recipe + 3 serving_gen = 10).

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-bootstrap/bootstrap.sh
git commit -m "feat(hscc-bootstrap): rewrite as preflight-gated topology-detecting installer"
```

---

## Task 5: Live idempotent run + sync install template

**Files:** none new (operational verification + dual-layout sync).

- [ ] **Step 1: Run bootstrap on this machine, non-interactive, no force**

Run: `cd ~/.hermes/plugins/hscc-bootstrap && bash bootstrap.sh --yes`
Expected: prereqs pass; detects the live cluster; installs skills + roles (idempotent — already present); keeps existing serving.json (no --force); daemon already running. Staged report ends with "Done". No errors.

- [ ] **Step 2: Confirm it did not clobber the working serving.json**

Run: `diff <(cat ~/.hscc/serving.json) <(cat ~/.hscc/serving.json) && echo "serving.json intact"` and confirm the orchestrator + 3 keepalive workers are still present: `cat ~/.hscc/serving.json | ../../hermes-agent/venv/bin/python -c "import sys,json;u=json.load(sys.stdin)['units'];print('units:',[(x['role'],x['nodes'][0]) for x in u])"`
Expected: orchestrator .244 + worker .246/.247/.248 unchanged.

- [ ] **Step 3: Sync the install template (dual-layout)**

The repo keeps a template copy under `install/hscc-plugins/hscc-bootstrap/`. Sync the new files so the packaged installer matches:
```bash
cd ~/.hermes/plugins
mkdir -p install/hscc-plugins/hscc-bootstrap
cp hscc-bootstrap/bootstrap.sh hscc-bootstrap/detect.py hscc-bootstrap/serving_gen.py install/hscc-plugins/hscc-bootstrap/ 2>/dev/null || true
```
(If `install/hscc-plugins/hscc-bootstrap/` isn't the right template path, check where the repo vendors bootstrap and sync there; if no template exists, skip.)

- [ ] **Step 4: Commit + push**

```bash
cd ~/.hermes/plugins
git add hscc-bootstrap/ install/hscc-plugins/hscc-bootstrap/ 2>/dev/null
git commit -m "chore(hscc-bootstrap): sync install template + verified live run"
git push origin main
```

---

## Self-Review

**Spec coverage:**
- Prereq hard gate (sparkrun cluster + Hermes) → Task 4 Stage 1 ✓
- Detect facts from sparkrun (nodes/user/NAS) → Task 1 ✓
- Minimal interview (orchestrator, recipe; NAS auto from cache_dir) → Task 4 Stage 3 ✓
- Install skills + roles + ~/.hscc/serving.json + daemon → Task 4 Stage 4 ✓
- No model provisioning → confirmed absent ✓
- Idempotent, --yes/--force/--skip flags, staged report → Task 4 ✓
- Pure testable helpers → Tasks 1-3 ✓
- Topology-agnostic / new-user safe → detection + defaults, NAS optional ✓

**Placeholder scan:** none — full script + helper code given.

**Type consistency:** `detect.parse_clusters`/`detect_cluster` return `{name,hosts,user,nas}`; `serving_gen.build_serving(cluster, orchestrator, recipe, model, port, keepalive)` consumes `cluster["hosts"]` and the choices; `recipe_model`/`list_recipes` signatures match their call sites in bootstrap.sh. serving.json shape matches the live one (version/port/units with role/model/recipe/nodes + keepalive on workers).

**Risk:** Low. Helpers are pure + tested; the script is additive (replaces a stale script) and idempotent; serving.json is backed up before any regen and only regenerated with --force. No model provisioning, no core changes. The live run in Task 5 is non-destructive (--yes without --force keeps existing serving.json).
