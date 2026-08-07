"""
HSCC Cluster Template Engine

Loads cluster templates, validates them, and can:
- List available templates
- Preview what applying a template would change (dry-run)
- Apply a template (write configs, provision models, wire Hermes)

Flow:
  hscc template list          → list available templates
  hscc template preview <n>   → dry-run, show config changes
  hscc template apply <n>     → apply template (with confirmation)
"""

from __future__ import annotations

import json
import os
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────

PLUGIN_DIR = Path(__file__).parent
TEMPLATE_DIR = PLUGIN_DIR / "templates"
HSCC_DIR = Path(os.path.expanduser("~/.hscc"))
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
SERVING_JSON = HSCC_DIR / "serving.json"
MODELS_JSON = HSCC_DIR / "models.json"
CLUSTER_JSON = HSCC_DIR / "cluster.json"
CONFIG_YAML = HERMES_HOME / "config.yaml"
# Worker-side profile dir: the :4000-family model id must be mirrored into the
# top-level model.default of every role profile whose base_url points at the
# worker proxy. Overridable via env (tests point it at a tmp dir).
PROFILES_DIR = Path(os.environ.get("HSCC_PROFILES_DIR", str(HERMES_HOME / "profiles")))
# The worker/family inference proxy port. Workers send model ids to this port,
# so it is the "which family is the worker tier" discriminator.
WORKER_PROXY_PORT = int(os.environ.get("HSCC_WORKER_PROXY_PORT", "4000"))
PROXY_DIR = HSCC_DIR / "proxies"
APPLIED_STATE = HSCC_DIR / "applied_template.json"  # which template is live

# Provisioning timeout per unit (sparkrun run --no-follow --ensure).
# 15 min covers a mods image build + a ~30 GB multi-node sync.
# Override via HSCC_PROVISION_TIMEOUT env var (seconds).
PROVISION_TIMEOUT_S = int(os.environ.get("HSCC_PROVISION_TIMEOUT", "900"))

# Cap timestamped backups per file so re-applies don't accumulate forever
# (a prior version left 100+ serving.json.bak.* / models.json.bak.* in ~/.hscc).
MAX_BACKUPS = 5

# ── Helpers ────────────────────────────────────────────────────────────────

def _prune_backups(path: Path, keep: int = MAX_BACKUPS) -> None:
    """Keep only the newest ``keep`` ``<path>.bak.<epoch>`` siblings; delete older."""
    path = Path(path)
    backups = sorted(
        path.parent.glob(path.name + ".bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


ROLLBACK_DIR = HSCC_DIR / "rollback"
MAX_ROLLBACKS = 5
# Files captured in a pre-apply snapshot for atomic rollback (G4/5e).
_SNAPSHOT_FILES = ("serving.json", "models.json", "applied_template.json")


def _snapshot_state() -> Optional[Path]:
    """Copy the current serving/models/applied-template + config.yaml into a
    timestamped rollback bundle. Returns the bundle path (or None if nothing to
    snapshot). Pruned to MAX_ROLLBACKS most-recent bundles."""
    sources = [(HSCC_DIR / f) for f in _SNAPSHOT_FILES] + [CONFIG_YAML]
    if not any(s.exists() for s in sources):
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = ROLLBACK_DIR / ts
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        for src in sources:
            if src.exists():
                shutil.copy2(str(src), str(bundle / src.name))
    except OSError:
        return None
    # prune old bundles
    try:
        bundles = sorted([p for p in ROLLBACK_DIR.iterdir() if p.is_dir()],
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for old in bundles[MAX_ROLLBACKS:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return bundle


def _restore_snapshot(bundle: Optional[Path]) -> bool:
    """Restore files from a snapshot bundle back to their live locations.
    Returns True if a restore happened."""
    if not bundle or not Path(bundle).is_dir():
        return False
    restored = False
    for f in _SNAPSHOT_FILES:
        src = bundle / f
        if src.exists():
            try:
                shutil.copy2(str(src), str(HSCC_DIR / f))
                restored = True
            except OSError:
                pass
    cfg = bundle / CONFIG_YAML.name
    if cfg.exists():
        try:
            shutil.copy2(str(cfg), str(CONFIG_YAML))
            restored = True
        except OSError:
            pass
    return restored


def read_json(path: Path) -> Optional[dict]:
    """Read and parse a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict, backup: bool = True) -> Path:
    """Write JSON atomically with optional backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_path = Path(str(path) + f".bak.{int(datetime.now().timestamp())}")
        shutil.copy2(str(path), str(backup_path))
        _prune_backups(path)
    # Atomic write: tmp + rename
    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(str(tmp_path), str(path))
    return path


def atomic_yaml_update(path: Path, update_fn, backup: bool = True):
    """Read a YAML file, apply update_fn, write back atomically.

    update_fn receives the parsed dict and returns the updated dict.
    Returns (path, changed): ``changed`` is False when the new content is
    byte-identical to the old, so callers can skip side effects (e.g. a gateway
    restart) on a no-op apply.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    import yaml
    old_data = {}
    old_text = ""
    if path.exists():
        try:
            with open(path) as f:
                old_text = f.read()
            old_data = yaml.safe_load(old_text) or {}
        except Exception:
            pass

    new_data = update_fn(old_data.copy()) if isinstance(old_data, dict) else {}
    new_text = yaml.dump(new_data, default_flow_style=False, sort_keys=False)
    changed = new_text != old_text

    if not changed:
        return path, False

    if backup and path.exists():
        backup_path = Path(str(path) + f".bak.{int(datetime.now().timestamp())}")
        shutil.copy2(str(path), str(backup_path))
        _prune_backups(path)

    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        f.write(new_text)
    os.replace(str(tmp_path), str(path))
    return path, True


# ── Proxy management via native sparkrun proxy ────────────────────────────

# sparkrun 0.3+ ships a native LiteLLM-based inference proxy that auto-discovers
# running endpoints and self-supervises (no launchd plist or watchdog needed).
# We drive it via the CLI instead of hand-rolling plists.


def install_proxy(family) -> dict:
    """Start the native sparkrun proxy for a family. Returns action summary.

    Delegates to ``sparkrun proxy start`` which auto-discovers running endpoints,
    generates LiteLLM config, and launches the proxy as a daemon. Re-applying is
    idempotent — if the proxy is already running on the requested port, sparkrun
    reports success without disruption.

    Returns a dict keyed: port, loaded, error, via.
    """
    import subprocess

    proxy_dir = PROXY_DIR / family.name
    proxy_dir.mkdir(parents=True, exist_ok=True)

    loaded = False
    load_error = ""
    try:
        r = subprocess.run(
            ["sparkrun", "proxy", "start",
             "--port", str(family.proxy_port),
             "--cluster", "hscc"],
            capture_output=True, text=True, timeout=30)
        loaded = r.returncode == 0
        if not loaded:
            load_error = (r.stderr or r.stdout or "").strip() or "sparkrun proxy start failed"
    except Exception as e:
        load_error = str(e)

    return {
        "port": family.proxy_port,
        "loaded": loaded,
        "error": load_error or None,
        "via": "sparkrun-proxy",
    }


def remove_proxy(family) -> dict:
    """Stop the native sparkrun proxy. Returns action summary.

    Calls ``sparkrun proxy stop`` (SIGTERM via stored PID). Removes any leftover
    family config dir under ~/.hscc/proxies/."""
    import subprocess

    try:
        subprocess.run(
            ["sparkrun", "proxy", "stop"],
            capture_output=True, text=True, timeout=15)
    except Exception:
        pass

    # Remove family config dir (if any stale artifacts remain).
    family_dir = PROXY_DIR / family.name
    if family_dir.is_dir():
        try:
            shutil.rmtree(family_dir)
        except OSError:
            pass

    return {"status": "removed"}


def _prune_orphan_proxies(active_names) -> list:
    """Remove proxy dirs for families no longer in the plan (incl. their backups).

    Orphan family dirs (e.g. a 'vision' family dropped from the template) would
    otherwise keep their config.json + accumulated .bak.* forever. Never touches
    the 'logs' dir or an active family. Returns the names pruned."""
    if not PROXY_DIR.is_dir():
        return []
    keep = set(active_names) | {"logs"}
    pruned = []
    for d in PROXY_DIR.iterdir():
        if not d.is_dir() or d.name in keep:
            continue
        try:
            shutil.rmtree(d)
            pruned.append(d.name)
        except OSError:
            pass
    return pruned


# ── Backward-compatible aliases ──────────────────────────────────────────

def install_proxy_plist(family) -> dict:
    """Alias for install_proxy — kept for backward compat."""
    return install_proxy(family)


def remove_proxy_plist(family) -> dict:
    """Alias for remove_proxy — kept for backward compat."""
    return remove_proxy(family)


# ── Model provisioning ─────────────────────────────────────────────────────

def _serving_unit_id(u: Any, is_orch: bool) -> str:
    """The serving.json unit id for a resolved unit — MUST match
    template_intent.to_serving_json's id formula, since drift comparison
    correlates recorded serve commands to units by this id."""
    if is_orch:
        return "orch"
    short = u.model.split("/")[-1]
    suffix = u.nodes[0].rsplit(".", 1)[-1]
    return f"family-{u.family}-{short}-{suffix}-{u.port}"


def _render_serve_cmd(cluster: str, hosts_arg: str, port: int, recipe: str,
                      alias: str, tp: int, model: str = "") -> List[str]:
    """Canonical serve argv for a wanted unit — the exact argv that gets run.

    This is the SINGLE source of truth shared by both (a) recording at
    provision time and (b) drift comparison on the next apply, so the two
    always agree on what "the rendered serve command" is. Keeping it a pure
    function of (cluster, hosts, port, recipe, alias, tp) means a recorded
    value only ever changes when one of those inputs actually changes.
    """
    concrete = _extract_model_name(recipe) or model
    cmd = ["sparkrun", "run", os.path.expanduser(recipe),
           "--cluster", cluster, "--hosts", hosts_arg,
           "--port", str(port), "--no-follow", "--ensure"]
    cmd.extend(["--served-model-name", f"{concrete} {alias}"])
    if tp > 1:
        cmd.extend(["--tp", str(tp)])
    return cmd


def _diff_serve_cmds(old_cmd: List[str], new_cmd: List[str]) -> str:
    """Human-readable description of WHAT changed between two rendered serve
    commands (flag: old -> new), or '' when they are identical."""
    def flag_map(cmd: List[str]) -> dict:
        out: dict = {}
        i = 0
        while i < len(cmd):
            tok = cmd[i]
            if tok.startswith("--"):
                if i + 1 < len(cmd) and not cmd[i + 1].startswith("--"):
                    out[tok] = cmd[i + 1]
                    i += 2
                else:
                    out[tok] = True
                    i += 1
            else:
                out.setdefault("positional", []).append(tok)
                i += 1
        return out
    om, nm = flag_map(old_cmd), flag_map(new_cmd)
    if om == nm:
        return ""
    changes = [f"{k}: {om.get(k)!r} -> {nm.get(k)!r}"
               for k in sorted(set(om) | set(nm)) if om.get(k) != nm.get(k)]
    return "; ".join(changes) if changes else "command differs"


def _load_serving() -> dict:
    """Read serving.json defensively (missing/corrupt → empty unit list)."""
    try:
        data = read_json(SERVING_JSON)
        if isinstance(data, dict) and isinstance(data.get("units"), list):
            return data
    except Exception:
        pass
    return {"version": 2, "units": []}


def _provision_models(plan: Any, cluster: str = "hscc",
                      do_launch: bool = True,
                      recreate: bool = False) -> dict:
    """Bring the cluster to the resolved plan's model layout via sparkrun.

    For each unit (orchestrator + each worker unit) launch the model with
    `sparkrun run <recipe> --cluster <c> --hosts <node> --port <port> --ensure`
    (--ensure = no-op if already serving). Stop sparkrun containers on nodes the
    plan does not use. do_launch=False = dry plan (preview/tests).

    recreate=True forces every wanted unit to be stopped before its --ensure
    run, so the FULL rendered serve command (including --served-model-name and
    any other flag change) reaches vLLM on an already-running fleet. Without it
    a container already serving the same recipe is left untouched by --ensure,
    so a changed flag — the v1.6.0 alias being the motivating case — never
    activates. Every recreated unit is reported loudly in ``recreated``.
    """
    import subprocess

    result = {"stopped": [], "provisioned": [], "failed": [],
              "recreated": [], "warnings": [],
              "status": "ok", "note": ""}

    # (unit, nodes, port, recipe, tp, alias) the plan wants serving.
    #   nodes is a List[str] — the full span (len == tp).
    #
    # Logical-alias advertisement (HSCC v1.5.1). Each endpoint advertises BOTH
    # its concrete model id and a STABLE alias (orchestrator-model / worker-model)
    # via vLLM's multi-name `--served-model-name` (nargs='+', space-separated),
    # so consumers can pin the alias and a template/tier switch re-aims it
    # without rewiring every copied id.
    #
    # The alias is decided by ROLE at construction time (identity against
    # plan.orchestrator), NOT by magic list index — so a worker-only or reordered
    # plan can never alias a worker unit as "orchestrator-model".
    #
    # NOTE ON TOKENIZATION (verified 2026-08 against sparkrun v0.3.1, both
    # sparkrun paths): HSCC emits ONE argv token for the value, `"concrete alias"`.
    # sparkrun consumes it via `--served-model-name` (a single-valued CLI option),
    # then renders the command as a STRING on EVERY path — the explicit-command
    # template path (`_augment_served_model_name`: `"%s %s %s" % (cmd, flag,
    # value)`) AND the no-template structured path (`build_flags_from_map` →
    # `_build_base_command`, which does `" ".join(parts)`). That string is
    # base64-encoded, written to /tmp/sparkrun_serve.sh, and executed with
    # `bash --noprofile --norc`. bash then splits the space into SEPARATE argv
    # tokens, so `--served-model-name <concrete> <alias>` registers BOTH names.
    # Therefore this single-token-with-space encoding is correct on BOTH paths —
    # not just the template path. A comma-joined value instead registers ONE
    # model literally named "<concrete>,<alias>" → both concrete and alias 404.
    want = []
    for u in [plan.orchestrator] + [unit for fam in plan.families for unit in fam.units]:
        alias = "orchestrator-model" if u is plan.orchestrator else "worker-model"
        want.append((u, u.nodes, u.port, u.recipe, u.tp, alias,
                     _serving_unit_id(u, u is plan.orchestrator)))
    # Every node in every span is "in use" — don't stop a node that is part of a
    # spanning unit even if it isn't the primary node.
    plan_nodes = {n for _, nodes, _, _, _, _, _ in want for n in nodes}

    if not do_launch:
        span_label = lambda nodes: ",".join(nodes)
        result["note"] = "dry-run: would provision " + ", ".join(
            f"{r.split('/')[-1]}@{span_label(nodes)}:{p}" for _, nodes, p, r, _, _, _ in want)
        result["provisioned"] = [f"{span_label(nodes)}:{p}:{r}" for _, nodes, p, r, _, _, _ in want]
        return result

    # Stop sparkrun containers on nodes the plan does not use.
    stop_failures: list[str] = []
    for node in _running_nodes_via_sparkrun():
        if node and node not in plan_nodes:
            try:
                subprocess.run(["sparkrun", "stop", "--all", "--hosts", node],
                               capture_output=True, timeout=60)
                result["stopped"].append(node)
            except Exception as e:
                stop_failures.append(f"{node}: {e}")
    if stop_failures:
        result["stop_failures"] = stop_failures
        result.setdefault("note", "")

    # Launch each wanted (nodes, port, recipe, tp). --ensure: skip if already up.
    # Before launching, free each span node of any STALE container serving a
    # DIFFERENT model — a reused node would otherwise keep its old container on
    # the serve port, so the new `sparkrun run` crashes with Errno 98
    # (Address already in use). A node already serving the wanted model is left
    # running (--ensure idempotency). Nodes with no attributable job are skipped.
    #
    # DRIFT (t_5b0f4d2d): --ensure only compares the recipe, not the rendered
    # serve command. A unit already running the SAME recipe therefore keeps its
    # old flags (--served-model-name included) forever — the v1.6.0 alias never
    # activates on an already-running fleet. Two guards:
    #   * recreate=True → stop the unit's span first, so the --ensure run applies
    #     the FULL current command. Reported loudly in ``recreated``.
    #   * recreate=False → do NOT silently claim success. If a unit is found
    #     already serving the same recipe (so --ensure WILL skip it) we emit a
    #     warning that any flag change is NOT being applied and tell the operator
    #     to use --force-recreate. Apply then flips to warn, never silent success.
    running_recipes = _running_recipes_via_sparkrun()
    # Load recorded serve commands (from the PREVIOUS provision) so we can tell
    # a genuinely-unmodified unit apart from a genuinely-drifted one.
    serving = _load_serving()
    serving_records = {u.get("id"): u for u in serving.get("units", [])}
    drift_real: List[str] = []        # recorded cmd differs from current render
    drift_unchecked: List[str] = []   # no recorded cmd (pre-upgrade unit)
    recorded: List[str] = []          # unit ids whose serve_cmd we (re)stamped
    for unit, nodes, port, recipe, tp, alias, unit_id in want:
        want_model = _extract_model_name(recipe)
        want_span = list(nodes)
        hosts_arg = ",".join(nodes)
        # Was this unit already running the SAME recipe (so --ensure will skip it)?
        already_running_same = any(
            running_recipes.get(n) and _extract_model_name(running_recipes[n]) == want_model
            for n in want_span)
        if recreate and already_running_same:
            # Force-recreate: stop the whole span so --ensure relaunches with the
            # current rendered command. Same-recipe containers are normally left
            # alone by --ensure, so without this stop a flag change never applies.
            for node in want_span:
                try:
                    subprocess.run(["sparkrun", "stop", "--all", "--hosts", node],
                                   capture_output=True, timeout=60)
                    result["stopped"].append(node)
                except Exception as e:
                    stop_failures.append(f"{node}: {e}")
            result["recreated"].append(f"{hosts_arg}:{port}:{recipe.split('/')[-1]}")
        elif not recreate and already_running_same:
            # Not recreating and the unit is already up on the same recipe →
            # --ensure WILL skip it, so only a genuinely-changed serve command is
            # left unapplied. Compare the freshly rendered command against the
            # one recorded at the last provision:
            #   * identical      → unit truly unchanged → NO drift warning (this
            #     is the important half: silence on the unchanged fleet is what
            #     makes the warning meaningful).
            #   * differs        → REAL drift: name the unit AND what changed.
            #   * no record      → pre-upgrade unit: fall back to conservative
            #     "drift not checked", never a false "command drift" claim.
            rendered = _render_serve_cmd(cluster, hosts_arg, port, recipe,
                                         alias, tp, getattr(unit, "model", ""))
            rec = serving_records.get(unit_id)
            rec_cmd = (rec or {}).get("serve_cmd")
            if rec_cmd is None:
                drift_unchecked.append(f"{hosts_arg}:{port}:{recipe.split('/')[-1]}")
            elif rec_cmd != rendered:
                diff = _diff_serve_cmds(rec_cmd, rendered)
                drift_real.append(f"{hosts_arg}:{port}:{recipe.split('/')[-1]}" +
                                  (f" ({diff})" if diff else ""))
        for node in nodes:
            run_recipe = running_recipes.get(node)
            if run_recipe and _extract_model_name(run_recipe) != want_model:
                try:
                    subprocess.run(["sparkrun", "stop", "--all", "--hosts", node],
                                   capture_output=True, timeout=60)
                    result["stopped"].append(node)
                except Exception as e:
                    stop_failures.append(f"{node}: {e}")
        # True when this unit's span is being (re)launched with the CURRENT
        # rendered command — a fresh launch or a recreate-forced relaunch. False
        # when it's an already-running-same-recipe --ensure no-op. We only record
        # serve_cmd for actually-(re)launched units; a skipped unit keeps its
        # prior record (which is what we just compared against), so we never
        # overwrite "what's running" with a command that did NOT take effect.
        actually_relaunched = recreate or not already_running_same
        try:
            # Concrete id = recipe's model field (fall back to the resolved
            # unit's model). Always advertise concrete + alias so the endpoint
            # stays queryable by its real name and by the stable logical alias.
            # The value is SPACE-separated (`concrete alias`), NOT comma-joined:
            # see the tokenization note above — sparkrun bash-executes the
            # rendered command on every path, so the space reaches vLLM's
            # nargs='+' `--served-model-name` as SEPARATE argv tokens and both
            # names register.
            cmd = _render_serve_cmd(cluster, hosts_arg, port, recipe, alias, tp,
                                    getattr(unit, "model", ""))
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=PROVISION_TIMEOUT_S)
            if r.returncode == 0:
                result["provisioned"].append(
                    f"{hosts_arg}:{port}:{recipe.split('/')[-1]}")
                # Record the rendered serve command ONLY for units we actually
                # (re)launched, so the record stays truthful about what the
                # running container was started with. An --ensure no-op on an
                # already-running unit must NOT overwrite its prior record.
                if actually_relaunched:
                    rec = serving_records.setdefault(unit_id, {"id": unit_id})
                    if rec.get("serve_cmd") != cmd:
                        rec["serve_cmd"] = cmd
                        recorded.append(unit_id)
            else:
                result["failed"].append(
                    {"node": hosts_arg, "port": port, "recipe": recipe,
                     "error": (r.stderr or "").strip()[:200]})
        except Exception as e:
            result["failed"].append({"node": hosts_arg, "port": port, "recipe": recipe,
                                     "error": str(e)})

    # Persist any serve_cmd stamps we recorded back to serving.json.
    if recorded:
        write_json(SERVING_JSON, serving, backup=True)

    # Loud drift reporting. A unit whose rendered serve command DIFFERS from what
    # was recorded at its last provision is real drift — name it and what
    # changed. A pre-upgrade unit with no recorded command gets the conservative
    # "drift not checked" wording, never a false "command drift" claim. A unit
    # whose command is unchanged is silence (not in either list).
    if drift_real:
        result["warnings"].append(
            "serve command drift NOT applied: " + ", ".join(drift_real) +
            " — rendered serve command differs from what the running unit was "
            "started with. Re-run apply with --force-recreate to re-apply "
            "changed flags (e.g. a new served-model-name alias).")
    if drift_unchecked:
        result["warnings"].append(
            "drift not checked for: " + ", ".join(drift_unchecked) +
            " — no previously recorded serve command (pre-upgrade unit); "
            "re-run apply with --force-recreate to re-apply changed flags.")

    if result["failed"]:
        result["status"] = "warn"
        result["note"] = f"{len(result['failed'])} model(s) failed to launch"
    elif drift_real:
        result["status"] = "warn"
        result["note"] = (f"{len(result['provisioned'])} model(s) ensured up; "
                          f"{len(drift_real)} unit(s) skipped with command drift "
                          f"(re-run with --force-recreate to apply).")
    elif drift_unchecked:
        result["status"] = "warn"
        result["note"] = (f"{len(result['provisioned'])} model(s) ensured up; "
                          f"{len(drift_unchecked)} unit(s) with unrecorded serve "
                          f"command (drift not checked; re-run with "
                          f"--force-recreate to apply).")
    elif result["recreated"]:
        result["note"] = (f"{len(result['provisioned'])} model(s) ensured up; "
                          f"{len(result['recreated'])} unit(s) recreated to apply "
                          f"the current serve command.")
    else:
        result["note"] = f"{len(result['provisioned'])} model(s) ensured up"
    return result


def _running_nodes_via_sparkrun() -> List[str]:
    """Best-effort: node IPs that currently have a sparkrun container."""
    import subprocess
    nodes: List[str] = []
    try:
        r = subprocess.run(["sparkrun", "status"], capture_output=True,
                           text=True, timeout=15)
        for line in (r.stdout or "").split("\n"):
            # status rows include the host IP; collect anything IP-shaped
            for tok in line.split():
                if tok.count(".") == 3 and tok.replace(".", "").isdigit():
                    nodes.append(tok)
    except Exception:
        pass
    return nodes


def _running_recipes_via_sparkrun() -> Dict[str, str]:
    """Best-effort: {node_ip: served recipe} for running sparkrun containers.

    Parses `sparkrun status` into (node, recipe) pairs using the same format as
    ops._running_by_node: hosts listed under a `Job:` block are running that
    job's recipe; hosts under the `Idle hosts (...)` section are NOT running.
    Nodes not attributed to any job (e.g. bare IPs with no surrounding Job
    block) are skipped. Returns {} on failure or when nothing maps.
    """
    import subprocess
    mapping: Dict[str, str] = {}
    try:
        r = subprocess.run(["sparkrun", "status"], capture_output=True,
                           text=True, timeout=15)
    except Exception:
        return mapping
    in_idle = False
    recipe = "unknown"
    for line in (r.stdout or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Idle hosts"):
            in_idle = True
            continue
        if stripped.startswith("Job:"):
            in_idle = False
            parts = stripped.split()
            recipe = parts[1] if len(parts) > 1 else "unknown"
            continue
        if in_idle:
            continue
        if recipe == "unknown":
            # no known job context → can't attribute a served model
            continue
        ip = next((tok for tok in line.split()
                   if tok.count(".") == 3 and tok.replace(".", "").isdigit()),
                  None)
        if ip is not None:
            mapping[ip] = recipe
    return mapping


# ── Template loading ───────────────────────────────────────────────────────

def _ti():
    try:
        from . import template_intent as m
    except ImportError:
        import template_intent as m
    return m


def _discover(probe: bool = False):
    try:
        from . import discovery as m
    except ImportError:
        import discovery as m
    try:
        return m.discover(probe=probe)
    except Exception:
        # Probe is live-cluster + ssh; on failure fall back to probe=False
        # (free stays None, overflow check skipped, as before).
        return m.discover()


def _find_template_file(template_name: str):
    """Locate a template yaml by name, searching TEMPLATE_DIR + one level of
    subdirs (e.g. templates/4node/coding.yaml). Match by filename stem OR by the
    template's own ``name:`` field, so both 'coding' and '4node-coding' resolve."""
    direct = TEMPLATE_DIR / f"{template_name}.yaml"
    if direct.exists():
        return direct
    import yaml
    for f in sorted(TEMPLATE_DIR.rglob("*.yaml")):
        if f.stem == template_name:
            return f
        try:
            data = yaml.safe_load(open(f)) or {}
            if data.get("name") == template_name:
                return f
        except Exception:
            continue
    return None


def _load_intent(template_name: str):
    """Load a v2 intent template (yaml → ClusterTemplate). Raises on bad shape."""
    import yaml
    path = _find_template_file(template_name)
    if path is None:
        raise FileNotFoundError(f"Template not found: {template_name}")
    with open(path) as f:
        data = yaml.safe_load(f)
    return _ti().ClusterTemplate.from_dict(data)


def _existing_tp_peer_nodes():
    """IPs currently reserved as tp peers of a live serving span (serving.json).

    Reuses cmdlib.serving_unit_scoreboard() — the same helper /cluster and
    enumerate_cluster_nodes use — rather than re-implementing tp-peer detection.
    cmdlib lives in the sibling hscc-commands plugin; we import it bare like
    the daemon does. Best-effort: any failure -> empty set (the pre-fix
    behaviour), never a hard failure of resolve/apply.
    """
    try:
        _ensure_cmdlib_on_path()
        from cmdlib import serving_unit_scoreboard
        score = serving_unit_scoreboard()
    except Exception:
        return set()
    if not isinstance(score, dict):
        return set()
    return {ip for ip, s in score.items() if s and s.get("tp_peer")}


def _ensure_cmdlib_on_path():
    """Put the sibling hscc-commands plugin dir on sys.path for the cmdlib import."""
    cmd_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "hscc-commands"))
    if cmd_dir not in sys.path:
        sys.path.insert(0, cmd_dir)


def _resolve(template_name: str, *, topology=None, probe: bool = False):
    """Load + resolve a template against the live cluster → ResolvedPlan.

    Nodes that are currently tp peers of a live serving span are excluded from
    the assignable pool (resolve's exclude_nodes), so a span member is never
    given a solo unit — the plan/provision guard for the double-provision bug.
    """
    tpl = _load_intent(template_name)
    topo = topology if topology is not None else _discover(probe=probe)
    return _ti().resolve(tpl, topo,
                         exclude_nodes=_existing_tp_peer_nodes())


def list_templates():
    """List all available cluster templates (v2 intent files)."""
    import yaml
    templates = []
    for f in sorted(TEMPLATE_DIR.rglob("*.yaml")):
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
            if data.get("name"):
                rel = f.relative_to(TEMPLATE_DIR)
                templates.append({
                    "name": data["name"],
                    "version": data.get("version", 2),
                    "description": data.get("description", ""),
                    "families": [fam.get("name") for fam in (data.get("families") or [])],
                    "group": rel.parent.name if rel.parent.name != "." else "",
                })
        except Exception:
            continue
    return {"count": len(templates), "templates": templates}


def preview_template(template_name: str) -> dict:
    """Preview what applying a template would change (dry-run). No writes.

    Resolves the intent template against the LIVE cluster so the preview shows
    the concrete nodes/ports that would be used.
    """
    resolved = _resolve(template_name, probe=True)
    tpl = _load_intent(template_name)

    new_serving = _ti().to_serving_json(resolved)
    new_models = _build_models_json(resolved)
    out = {
        "template": resolved.template,
        "description": tpl.description,
        "changes": [
            {"file": "serving.json", "action": "write",
             "summary": f"{len(new_serving['units'])} units "
                        f"(1 orchestrator + {len(resolved.families)} families)",
             "diff_summary": _diff_serving_summary(read_json(SERVING_JSON), new_serving)},
            {"file": "models.json", "action": "write",
             "summary": f"{len(new_models['models'])} models registered"},
            {"file": "config.yaml", "action": "update",
             "summary": "Update provider/model settings",
             "details": _describe_config_changes(resolved, read_json(MODELS_JSON))},
        ],
    }
    proxy_fams = [f for f in resolved.families if f.proxy_port is not None]
    if proxy_fams:
        out["changes"].append({
            "file": "proxies/", "action": "create",
            "summary": f"{len(proxy_fams)} proxy configs",
            "details": [f"  {f.name}: port {f.proxy_port}, "
                        f"nodes {sorted({u.node for u in f.units})}"
                        for f in proxy_fams],
        })
    out["changes"].append({
        "file": "models (provision)", "action": "provision",
        "summary": f"1 orchestrator + "
                   f"{sum(len(f.units) for f in resolved.families)} worker units",
    })
    return out


class TemplateValidationError(Exception):
    """A template is not deployable on this machine. Carries the failures."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_resolved_plan(plan: Any) -> List[str]:
    """Pre-apply preflight on a RESOLVED plan (concrete nodes/ports).

    Auto-assigned ports make node:port collisions structurally impossible, so the
    real checks are: every recipe exists, and no residual (node,port) dup. Pure +
    read-only.
    """
    errors: List[str] = []

    def _recipe_missing(recipe: str) -> bool:
        return not Path(os.path.expanduser(recipe)).is_file()

    if _recipe_missing(plan.orchestrator.recipe):
        errors.append(f"orchestrator recipe not found: {plan.orchestrator.recipe}")
    seen = set()
    for fam in plan.families:
        for u in fam.units:
            if _recipe_missing(u.recipe):
                errors.append(f"family '{fam.name}' recipe not found: {u.recipe}")
            key = (u.node, u.port)
            if key in seen:
                errors.append(f"(node,port) collision: {u.node}:{u.port}")
            seen.add(key)
    return errors


def apply_template(template_name: str, confirm: bool = False,
                   recreate: bool = False) -> dict:
    """Apply a cluster template (v2 intent). Resolves it against the live cluster,
    then writes configs, provisions models, sets up proxies — transactionally.

    recreate=True is forwarded to _provision_models: every unit whose serve
    command differs from what the running container was started with is forced
    to be recreated (stopped first, then re-run), so a changed flag — the
    v1.6.0 alias being the motivating case — actually reaches vLLM on an
    already-running fleet. Each recreated unit is reported loudly. Units that
    are already serving the current command are still left alone.
    """
    # Load + resolve against the live topology. Bad shape or unresolvable
    # (no workers, overcommit, …) is a hard, pre-write failure.
    try:
        tpl = _load_intent(template_name)
        plan = _resolve(template_name, probe=True)
    except _ti().TemplateIntentError as e:
        return {"status": "blocked", "success": False,
                "note": "Template is NOT deployable.", "errors": [str(e)]}

    problems = validate_resolved_plan(plan)

    if not confirm:
        return {
            "status": "blocked" if problems else "preview",
            "note": ("Template is NOT deployable — fix the errors below."
                     if problems else "Re-call with confirm=true to execute"),
            "errors": problems,
            "changes": preview_template(template_name),
        }

    if problems:
        raise TemplateValidationError(problems)

    result = {"template": tpl.name, "steps": [], "success": True}

    # Snapshot the live state BEFORE any write, so a half-completed apply can be
    # rolled back atomically (G4/5e — we corrupted a live cluster once this way).
    snapshot = _snapshot_state()
    result["rollback_bundle"] = str(snapshot) if snapshot else None

    try:
        # Ensure cluster-template package dir is on path for imports
        _pkg_dir = str(Path(__file__).parent)
        if _pkg_dir not in sys.path:
            sys.path.insert(0, _pkg_dir)
        # Step 1: Write serving.json (from the resolved plan)
        serving = _ti().to_serving_json(plan)
        write_json(SERVING_JSON, serving, backup=True)
        result["steps"].append({"step": "serving.json", "status": "ok", "units": len(serving["units"])})

        # Step 2: Write models.json
        models = _build_models_json(plan)
        write_json(MODELS_JSON, models, backup=True)
        result["steps"].append({"step": "models.json", "status": "ok", "models": len(models["models"])})

        # Step 3: Update Hermes config.yaml
        _, config_changed = atomic_yaml_update(
            CONFIG_YAML, lambda d: _update_hermes_config(d, plan))
        result["steps"].append({"step": "config.yaml", "status": "ok",
                                "changed": config_changed})

        # Step 4: Start native sparkrun proxy (per resolved family)
        # The native proxy auto-discovers running endpoints and generates its own
        # LiteLLM config — no manual config.json write needed.
        proxy_actions = []
        for fam in plan.families:
            if fam.proxy_port is None:
                continue
            proxy_result = install_proxy(fam)
            proxy_actions.append(proxy_result)
        # Remove proxy dirs (+ their accumulated backups) for families no longer
        # in the plan, so orphan families don't leak config.json.bak.* forever.
        active = [f.name for f in plan.families if f.proxy_port is not None]
        pruned = _prune_orphan_proxies(active)
        result["steps"].append({
            "step": "proxies/",
            "status": "ok",
            "proxies": len(proxy_actions),
            "pruned_orphans": pruned,
            "details": proxy_actions,
        })

        # Step 5: Rewire worker-facing model ids (config.yaml delegation.* +
        # fallback_providers; worker role profiles' model.default) to the family
        # model. A worker-tier switch otherwise leaves stale model ids behind,
        # so every worker hits the strict proxy with an invalid model name.
        wm = _update_worker_model_ids(plan)
        result["steps"].append({
            "step": "worker-model-ids",
            "status": "ok",
            "model_id": wm["model_id"],
            "config_changed": wm["config_changed"],
            "profiles_changed": wm["profiles_changed"],
        })

        # Step 6: Provision models via sparkrun
        provision_result = _provision_models(plan, recreate=recreate)
        result["steps"].append({
            "step": "provision",
            "status": provision_result.get("status", "ok"),
            "stopped": provision_result.get("stopped", []),
            "provisioned": provision_result.get("provisioned", []),
            "recreated": provision_result.get("recreated", []),
            "warnings": provision_result.get("warnings", []),
            "note": provision_result.get("note", ""),
        })
        
        # Step 7: Restart gateway ONLY if config.yaml actually changed —
        # a no-op apply shouldn't cause a ~30s gateway outage.
        if config_changed:
            from gateway_restart import restart_gateway
            gw_result = restart_gateway()
            result["steps"].append({
                "step": "gateway-restart",
                "status": "ok" if gw_result["success"] else "warn",
                "note": gw_result.get("note", ""),
            })
        else:
            result["steps"].append({
                "step": "gateway-restart",
                "status": "skipped",
                "note": "config.yaml unchanged — gateway restart not needed",
            })

        # After provision + gateway-restart, check that no step ended in a
        # non-OK state — warn/error means the cluster was only partially
        # brought up, so apply is not a success.
        for step in result["steps"]:
            status = step.get("status", "")
            if status in ("warn", "error", "fail"):
                result["success"] = False
                break

    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        result["steps"].append({"step": "error", "status": "fail", "message": str(e)})
        # Atomic rollback: restore the pre-apply snapshot so the cluster is left
        # in its prior state, not a half-applied one (G4/5e).
        rolled_back = _restore_snapshot(snapshot)
        result["rolled_back"] = rolled_back
        result["steps"].append({
            "step": "rollback",
            "status": "ok" if rolled_back else "warn",
            "note": ("restored serving/models/config from snapshot"
                     if rolled_back else "no snapshot to restore"),
        })

    # Record which template is now live (so `status` can answer "what's applied?")
    if result["success"]:
        try:
            write_json(APPLIED_STATE, {
                "template": tpl.name,
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "orchestrator_node": plan.orchestrator.node,
                "families": [f.name for f in plan.families],
                "units": len(serving["units"]),
            }, backup=False)
        except Exception:
            pass

    return result


def applied_status() -> dict:
    """Report which template is currently applied (from APPLIED_STATE).

    Returns {"applied": <state>} or {"applied": None} if nothing recorded.
    """
    state = read_json(APPLIED_STATE)
    return {"applied": state or None,
            "note": "" if state else "No template applied yet (or applied before status tracking)."}


def validate_template(template_name: str) -> dict:
    """Standalone preflight: is this template deployable against the live cluster?
    No writes. Resolves intent → plan, then validates the plan."""
    try:
        plan = _resolve(template_name, probe=True)
    except FileNotFoundError as e:
        return {"template": template_name, "ok": False, "errors": [str(e)]}
    except _ti().TemplateIntentError as e:
        return {"template": template_name, "ok": False, "errors": [str(e)]}
    except Exception as e:
        return {"template": template_name, "ok": False,
                "errors": [f"template invalid: {e}"]}
    problems = validate_resolved_plan(plan)
    return {"template": template_name, "ok": not problems, "errors": problems}


# ── Config generation helpers ──────────────────────────────────────────────

def _build_models_json(plan: Any) -> dict:
    """Build models.json from a resolved plan (template_intent.ResolvedPlan)."""
    o = plan.orchestrator
    models = [{
        "name": o.model, "type": "llm", "status": "serving", "location": "vLLM",
        "tp": o.tp, "pp": o.pp, "family": "orchestrator",
    }]
    for fam in plan.families:
        for u in fam.units:
            models.append({
                "name": u.model, "type": "llm", "status": "serving",
                "location": "vLLM", "tp": u.tp, "pp": u.pp, "family": fam.name,
            })
    return {
        "primary_model": o.model,
        "provider": "custom",
        "base_url": f"http://{o.node}:{o.port}/v1",
        "models": models,
    }


def _build_proxy_config(resolved_family) -> dict:
    """Build a LiteLLM proxy config for a resolved worker family.

    Each backend is a concrete node:port from the resolved plan (so the proxy
    load-balances across the real worker endpoints)."""
    backends = []
    for u in resolved_family.units:
        backends.append({
            "model_name": u.model,
            "litellm_params": {
                "model": f"openai/{u.model}",
                "api_base": f"http://{u.node}:{u.port}/v1",
                "tp": u.tp, "pp": u.pp,
            },
        })
    return {
        "model": [],
        "litellm_settings": {"drop_params": True},
        "general_settings": {},
        "serving_model_configs": backends,
        "proxy_params": {
            "host": "0.0.0.0",
            "port": resolved_family.proxy_port,
            "model_type": "openai",
            "extra_args": {},
        },
    }


def _update_hermes_config(config: dict, plan: Any) -> dict:
    """Update Hermes config.yaml providers from a resolved plan.

    Idempotent: providers keyed by name and rebuilt, so re-running apply never
    duplicates (a prior version appended on every call, corrupting config).

    Also updates the top-level ``model`` block so the orchestrator's served
    model id and base_url are correct — vLLM is strict about model identity."""
    o = plan.orchestrator

    # ── Top-level model block ────────────────────────────────────────
    model_cfg = config.setdefault("model", {})
    model_cfg["default"] = o.model
    model_cfg["base_url"] = f"http://{o.node}:{o.port}/v1"
    # Preserve existing provider; default to "custom" if absent
    if "provider" not in model_cfg:
        model_cfg["provider"] = "custom"

    # ── Providers list ───────────────────────────────────────────────
    existing = config.get("providers")
    by_name: dict = {}
    if isinstance(existing, list):
        for p in existing:
            if isinstance(p, dict) and p.get("name"):
                by_name[p["name"]] = p

    by_name["custom"] = {
        "name": "custom",
        "model": {"default": f"{o.node}:{o.port}"},
        "base_url": f"http://{o.node}:{o.port}/v1",
    }
    for fam in plan.families:
        if fam.proxy_port is None:
            continue
        name = f"family-{fam.name}"
        by_name[name] = {
            "name": name,
            "model": {"default": f"localhost:{fam.proxy_port}"},
            "base_url": f"http://localhost:{fam.proxy_port}/v1",
        }
    config["providers"] = list(by_name.values())
    return config


def _owned_worker_family(plan: Any) -> Optional[Any]:
    """The family that owns the worker proxy (:4000), or None.

    A family owns the worker proxy when its ``proxy_port`` matches
    WORKER_PROXY_PORT and it has at least one unit. None when no family owns
    the worker proxy (e.g. a dual-orchestrator plan with no worker tier) —
    callers must then leave worker-facing fields untouched.
    """
    for fam in plan.families:
        if fam.proxy_port == WORKER_PROXY_PORT and fam.units:
            return fam
    return None


def _worker_model_id(plan: Any) -> Optional[str]:
    """Resolve the worker/family model id — the model the worker proxy family
    serves.

    Returns ``units[0].model`` for the family that owns the worker proxy
    (see ``_owned_worker_family``). All units in that family share the served
    model. Returns None when no family owns the worker proxy — callers must
    then leave worker ids untouched.
    """
    fam = _owned_worker_family(plan)
    return fam.units[0].model if fam is not None else None


def _worker_proxy_url(plan: Any) -> Optional[str]:
    """The worker proxy base_url from the plan, or None when no worker tier.

    Derived from the family that owns the worker proxy — same identity test as
    ``_worker_model_id`` — so the re-aimed base_url is always consistent with
    the rewired model id. Returns None when no family owns the worker proxy.
    """
    fam = _owned_worker_family(plan)
    if fam is None:
        return None
    return f"http://localhost:{fam.proxy_port}/v1"


def _models_probe_url(base_url: str) -> str:
    """Normalize an endpoint base_url to an OpenAI-style ``/models`` probe URL.

    Mirrors ``hscc-bootstrap.doctor._models_url`` exactly: the version path is
    PRESERVED (an OpenAI-compatible server serves the model list at
    ``{base_url}/models``, e.g. ``http://host:port/v1/models``, NOT at a
    version-stripped root), so we append ``/models`` and never strip ``/v1``.

    ``http://host:port/v1``  -> ``http://host:port/v1/models``
    ``http://host:port/v1/`` -> ``http://host:port/v1/models``
    ``http://host:port``     -> ``http://host:port/models``
    Returns "" for missing/blank input.
    """
    url = (base_url or "").strip()
    if not url:
        return ""
    url = url.rstrip("/")
    if "/v1" in url:
        head = url.split("/v1", 1)[0].rstrip("/")
        return head + "/v1/models"
    return url + "/models"


def _http_get_default(url: str, api_key: Optional[str] = None) -> str:
    """Fetch ``url`` and return its body as text.

    Mirrors ``hscc-bootstrap.doctor._http_get_default``: sends ``Authorization:
    Bearer <key>`` when a key is provided (the configs carry api_key next to
    base_url; an endpoint that requires a key returns 401 without it), and
    raises ``urllib.error`` on failure so callers can tell a probe/config error
    apart from an unreachable endpoint.
    """
    import urllib.request
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _probe_served_models(base_url, *, _http_get=None):
    """Probe an endpoint's ``/v1/models`` and report what it serves.

    Mirrors ``hscc-bootstrap.doctor._probe_served_models`` so the probe CONTRACT
    (correct ``/v1/models`` path + Bearer auth) is shared across the fleet. It is
    intentionally NOT a literal cross-plugin import: each HSCC plugin deploys
    standalone into ``~/.hermes/plugins`` (hyphenated dirs are not importable
    package names), so hscc-cluster cannot ``import doctor`` from hscc-bootstrap.
    Keeping the probe byte-compatible with doctor's lets both stay in lockstep.

    Returns ``(status, served_ids)`` where ``status`` is one of:
      - ``"ok"``          - endpoint reachable and ``/models`` parsed; ``served_ids``
                            is the list of served model ids (possibly empty).
      - ``"unreachable"`` - network/timeout; nothing could be verified.
      - ``"error"``       - endpoint reachable but ``/models`` returned a non-2xx
                            HTTP status or an unparseable body (probe/config problem).

    ``_http_get(url, api_key=None) -> str`` is injectable for tests, so the
    probe-before-write safety gate below can be exercised without the network.
    """
    getter = _http_get or _http_get_default
    url = _models_probe_url(base_url)
    if not url:
        return ("error", [])
    import json
    from urllib.error import HTTPError
    try:
        raw = getter(url, api_key=None)
    except HTTPError as exc:
        return ("error", [f"{url} returned HTTP {exc.code} on /models probe"])
    except Exception as exc:
        return ("unreachable", [f"{url} unreachable ({exc})"])
    try:
        data = json.loads(raw)
        served = [m.get("id") for m in (data.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        return ("error", [f"{url} body not parseable as /models JSON ({exc})"])
    return ("ok", served)


def _resolve_worker_model_id(model_id: Optional[str], served) -> Optional[str]:
    """Probe-before-write: resolve the model id to write against the worker proxy.

    ``model_id`` is the template-declared worker unit model (``units[0].model``).
    It MAY be the stable alias ``worker-model`` or the concrete id depending on
    how the template was authored — and the worker proxy (:4000, sparkrun
    LiteLLM balancer) serves ONLY the concrete id, NOT the alias. Writing an
    alias the endpoint does not advertise makes every delegated call 404.

    So we only ever write an id the endpoint actually serves:
      - if the declared ``model_id`` is served → use it (today's fleet: the
        concrete id, since ``units[0].model`` = recipe ``model:`` field);
      - else if the proxy serves exactly one concrete id → use that (the
        "write CONCRETE when the endpoint does not advertise the alias" path);
      - else → ``None``: refuse to write anything we cannot confirm the
        endpoint resolves (never write an id that 404s).

    ``served`` comes from ``_probe_served_models``. Requires a probe result.
    """
    if not served:
        return None
    if model_id in served:
        return model_id
    if len(served) == 1:
        return served[0]
    return None


def _is_worker_proxy_url(base_url: Optional[str], port: int) -> bool:
    """True when ``base_url`` points at the family proxy on ``port``.

    Matched on loopback host + port (e.g. http://localhost:4000/v1) so the
    orchestrator (:8000) or any remote node is never mistaken for the worker
    tier.
    """
    if not base_url:
        return False
    try:
        host, rest = base_url.split("://", 1)[1].split(":", 1)[:2]
        port_str = rest.split("/", 1)[0].strip()
        if not port_str.isdigit() or int(port_str) != port:
            return False
    except Exception:
        return False
    return host.strip() in ("localhost", "127.0.0.1")


def _set_worker_model_in_config(config: dict, model_id: str, proxy_url: str) -> dict:
    """Set the worker-facing routing fields of config.yaml.

    Rewires ``delegation`` and every ``fallback_providers[].model`` to
    ``model_id`` AND their ``base_url`` to ``proxy_url`` (the worker proxy on
    :4000). Keeping all three in lockstep is what routes delegated subagents to
    the worker pool instead of the orchestrator GPU: rewiring only the model id
    while the base_url still points at the orchestrator (:8000) leaves
    subagents running on the orchestrator. Idempotent: re-setting already
    correct values is a byte no-op, so repeated applies never churn the file.

    Does NOT touch the orchestrator's top-level model block (handled by
    ``_update_hermes_config``).
    """
    delegation = config.get("delegation")
    if not isinstance(delegation, dict):
        delegation = {}
        config["delegation"] = delegation
    delegation["model"] = model_id
    delegation["base_url"] = proxy_url

    fps = config.get("fallback_providers")
    if isinstance(fps, list):
        for fp in fps:
            if isinstance(fp, dict):
                fp["model"] = model_id
                if isinstance(proxy_url, str) and proxy_url:
                    fp["base_url"] = proxy_url
    return config


def _set_worker_model_in_profile(config: dict, model_id: str, port: int) -> dict:
    """Set a role profile's top-level ``model.default`` to ``model_id`` — but
    ONLY when that profile's ``model.base_url`` points at the worker proxy on
    ``port``. Profiles routing to the orchestrator (:8000) or elsewhere are left
    untouched. Idempotent: no-op when already correct or not worker-facing.
    """
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return config
    if not _is_worker_proxy_url(model_cfg.get("base_url"), port):
        return config
    if model_cfg.get("default") != model_id:
        model_cfg["default"] = model_id
    return config


def _update_worker_model_ids(plan: Any, *, profiles_dir: Optional[Path] = None,
                             config_yaml: Optional[Path] = None,
                             _http_get=None) -> dict:
    """Rewire worker-facing routing to the resolved worker/family model.

    Called from ``apply_template`` after config.yaml is written. Two concerns:
      1. config.yaml — ``delegation.model`` + every ``fallback_providers[].model``
         AND their ``base_url``. The base_url is re-aimed at the worker proxy
         (:4000) so that delegating subagents and the first fallback hit the
         WORKER pool, not the orchestrator GPU — a worker-tier apply otherwise
         fixes the model id but leaves subagents still running on the
         orchestrator (the :8000 routing regression).
      2. worker role profiles — top-level ``model.default`` for every profile
         whose ``model.base_url`` points at the worker proxy.

    PROBE-BEFORE-WRITE: before writing an id to the worker proxy, this probes
    the proxy's ``/v1/models`` and only writes an id the endpoint actually
    serves (``_resolve_worker_model_id``). The worker proxy serves ONLY the
    concrete id — NOT the alias ``worker-model`` — so if the template declared
    an alias, the CONCRETE id the proxy serves is written instead. If the probe
    fails or no resolvable id is found, nothing is written (the id would 404
    every delegated call). ``_http_get`` is injectable for tests; default is a
    real network probe.

    All writes use atomic_yaml_update (backup + tmp + os.replace) and are
    idempotent: re-running apply with an already-correct state changes nothing.
    No worker family in the plan → returns immediately, leaving worker-facing
    fields untouched. Never touches the orchestrator model.default.
    """
    model_id = _worker_model_id(plan)
    result = {"model_id": model_id, "config_changed": False, "profiles_changed": 0}
    if model_id is None:
        return result

    # A worker family exists (model_id is not None) ⇒ it owns the proxy, so the
    # base_url is always resolved here.
    proxy_url = _worker_proxy_url(plan) or f"http://localhost:{WORKER_PROXY_PORT}/v1"

    # Probe the worker proxy /v1/models BEFORE writing. Resolve to the declared
    # model if it is served, else to the CONCRETE id the proxy serves, else
    # None (refuse to write an id the endpoint cannot resolve). Unreachable /
    # probe-error also resolves to None — never write blind.
    status, served = _probe_served_models(proxy_url, _http_get=_http_get)
    resolved = _resolve_worker_model_id(model_id, served if status == "ok" else [])
    result["probe_status"] = status
    result["probe_served"] = list(served) if isinstance(served, list) else []
    if resolved is None:
        result["refused"] = (
            f"worker proxy {proxy_url} did not confirm it serves '{model_id}' "
            f"(probe {status}); leaving delegation/fallback untouched")
        return result
    model_id = resolved

    conf = config_yaml or CONFIG_YAML
    _, result["config_changed"] = atomic_yaml_update(
        conf, lambda d: _set_worker_model_in_config(d, model_id, proxy_url))

    pd = profiles_dir or PROFILES_DIR
    for pfile in sorted(pd.glob("*/config.yaml")):
        if not pfile.is_file():
            continue
        _, changed = atomic_yaml_update(
            pfile,
            lambda d, port=WORKER_PROXY_PORT: _set_worker_model_in_profile(d, model_id, port))
        if changed:
            result["profiles_changed"] += 1
    return result


def _describe_config_changes(plan, current_models: Optional[dict]) -> list:
    """Describe what config changes will be made (from a resolved plan)."""
    o = plan.orchestrator
    changes = [f"  orchestrator: {o.node}:{o.port} (model: {o.model})"]
    for fam in plan.families:
        port = fam.proxy_port if fam.proxy_port is not None else "—"
        nodes = sorted({u.node for u in fam.units})
        changes.append(
            f"  family-{fam.name}: localhost:{port} "
            f"({len(fam.units)} units, {len(nodes)} nodes)")
    return changes


def _diff_serving_summary(current: Optional[dict], new: dict) -> str:
    """Human-readable summary of serving.json changes."""
    old_units = len(current.get("units", [])) if current else 0
    new_units = len(new.get("units", []))
    return f"{old_units} units → {new_units} units"


# ── Utility helpers ────────────────────────────────────────────────────────

def _extract_model_name(recipe_path: str) -> str:
    """Resolve the served model name for a recipe (recipe ``model:`` field, else
    filename stem). Single source of truth: template_intent._model_name."""
    try:
        from . import template_intent as _ti
    except ImportError:
        import template_intent as _ti
    return _ti._model_name(recipe_path)


# ── CLI entry point ────────────────────────────────────────────────────────

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="HSCC Cluster Template Manager")
    subparsers = parser.add_subparsers(dest="command")
    
    # list
    list_parser = subparsers.add_parser("list", help="List available templates")
    
    # preview
    preview_parser = subparsers.add_parser("preview", help="Preview template application")
    preview_parser.add_argument("template", help="Template name (without .yaml)")
    
    # apply
    apply_parser = subparsers.add_parser("apply", help="Apply a cluster template")
    apply_parser.add_argument("template", help="Template name (without .yaml)")
    apply_parser.add_argument("--confirm", action="store_true", help="Execute without confirmation")
    
    args = parser.parse_args()
    
    if args.command == "list":
        result = list_templates()
        print(json.dumps(result, indent=2))
    
    elif args.command == "preview":
        result = preview_template(args.template)
        print(json.dumps(result, indent=2))
    
    elif args.command == "apply":
        result = apply_template(args.template, confirm=args.confirm)
        print(json.dumps(result, indent=2))
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
