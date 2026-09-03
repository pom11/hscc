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


def _unit_alias(u: Any, plan: Any) -> str:
    """The stable logical alias for a unit, decided by ROLE IDENTITY against
    ``plan.orchestrator`` — NEVER by index, role string, or name matching (a
    worker whose role string is literally 'orchestrator' still gets
    worker-model).

    The orchestrator advertises ``orchestrator-model``; every family/worker
    unit advertises ``worker-model``. Shared by ``_provision_models`` (which
    builds the serve commands that advertise the alias) and
    ``_routing_target_endpoint`` (which offers the alias as the write
    candidate) so the two can never disagree about a unit's alias.
    """
    return "orchestrator-model" if u is plan.orchestrator else "worker-model"


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
        alias = _unit_alias(u, plan)
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


def _existing_serving_units():
    """{node_ip: {"kind": "orchestrator"|"worker", "family": <name|None>,
    "model": <str>}} for every node currently named in serving.json.

    Built from serving.json units directly (not the scoreboard) so each unit's
    FAMILY lineage is available — resolve()'s unit-aware guard needs to know
    which family a reserved worker node belongs to, so it can let that node
    back into its OWN family while refusing to hand it to a DIFFERENT one.
    Reads the same recorded plan as _existing_tp_peer_nodes did, but carries
    ownership instead of bluntly ejecting every tp-peer from the pool (which
    emptied the pool on re-apply of the same template). Best-effort: any
    failure -> empty dict (no guard), never a hard failure of resolve/apply.
    """
    try:
        serving = _load_serving()
    except Exception:
        return {}
    reserved: Dict[str, dict] = {}
    for u in serving.get("units", []):
        nodes = u.get("nodes") or []
        if not nodes:
            continue
        kind = "orchestrator" if u.get("role") == "orchestrator" else "worker"
        fam = u.get("family")
        model = u.get("model")
        for ip in nodes:
            # First wins: a node is primary in one unit; a later unit claiming
            # the same node must not overwrite the dominant lineage in a way
            # that could let that node be double-assigned to two families.
            if ip not in reserved:
                reserved[ip] = {"kind": kind, "family": fam, "model": model}
    return reserved


def _resolve(template_name: str, *, topology=None, probe: bool = False):
    """Load + resolve a template against the live cluster → ResolvedPlan.

    Feeds resolve()'s unit-aware `reserved` (which nodes currently serve which
    unit, from serving.json) so a node is only ever assigned to the SAME unit
    it already serves — re-applying a template keeps its own nodes (the
    --force-recreate path) while a span member is still never handed to a
    DIFFERENT unit (the double-provision guard).
    """
    tpl = _load_intent(template_name)
    topo = topology if topology is not None else _discover(probe=probe)
    return _ti().resolve(tpl, topo, reserved=_existing_serving_units())


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


def preview_template(template_name: str, *, _http_get=None) -> dict:
    """Preview what applying a template would change (dry-run). No writes.

    Resolves the intent template against the LIVE cluster so the preview shows
    the concrete nodes/ports that would be used. ``_http_get`` is injectable
    for tests so the probe-aware routing disclosure can run without the
    network.
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
                        f"nodes {sorted({n for u in f.units for n in u.nodes})}"
                        for f in proxy_fams],
        })
    # Node count reflects the FULL tp span of every unit, not the family's unit
    # count (a single tp=2 unit spans 2 nodes — report 2, not 1).
    worker_node_count = sum(len(u.nodes) for f in resolved.families for u in f.units)
    out["changes"].append({
        "file": "models (provision)", "action": "provision",
        "summary": f"1 orchestrator + {worker_node_count} worker nodes",
    })
    # Routing disclosure: show exactly what apply would write — and which
    # consumers it will NOT touch — reusing the SAME resolution helpers apply
    # uses so the two cannot drift (see _preview_routing). Read-only: probing
    # /v1/models is a GET; nothing is written, provisioned, or restarted.
    routing_disc = _preview_routing(tpl, resolved, _http_get=_http_get)
    if routing_disc["routing"]:
        out["routing"] = routing_disc["routing"]
        out["routing_untouched"] = routing_disc["routing_untouched"]

    # Unit-level delta — the concrete "what will actually change" the operator
    # cannot see from file summaries alone. Diff the LIVE serving.json units
    # against the resolved NEW serving units so the preview names which units
    # START, STOP, and MOVE (relocate/rescale) and which nodes are touched.
    # Always present (even "no change") so the app is never guessing. Read-only.
    out["serve_delta"] = _diff_serving_delta(read_json(SERVING_JSON), new_serving)
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
                   recreate: bool = False, *, _http_get=None) -> dict:
    """Apply a cluster template (v2 intent). Resolves it against the live cluster,
    then writes configs, provisions models, sets up proxies — transactionally.

    ``_http_get`` is injectable for tests and threaded down to every
    probe-before-write gate on the apply path (``_update_hermes_config``,
    ``_update_worker_model_ids``, ``_apply_routing``) so the suite exercises
    the probes deterministically without real network I/O. Default None → real
    network probe.

    recreate=True is forwarded to _provision_models: every unit whose serve
    command differs from what the running container was started with is forced
    to be recreated (stopped first, then re-run), so a changed flag — the
    v1.6.0 alias being the motivating case — actually reaches vLLM on an
    already-running fleet. Each recreated unit is reported loudly. Units that
    are already serving the current command are still left alone.
    """
    # PRE-FLIGHT GATE — the SAME two-layer validation as `hscc template
    # validate` (validate_template is that command's implementation). One
    # implementation, not two: apply delegates its gate here so the standalone
    # validate command and apply always agree on a template. The gate runs
    # BEFORE anything is loaded, resolved, stopped or started, and blocks —
    # returning without touching the fleet — whenever either layer fails
    # (this saved the fleet on 2026-08-07: a failed apply left it untouched).
    validation = validate_template(template_name)
    if not validation["ok"]:
        return {
            "status": "blocked",
            "success": False,
            "note": "Template is NOT deployable.",
            "errors": (validation["structural"]["errors"]
                       + validation["placement"]["errors"]),
            "validation": validation,
        }

    # Load + resolve against the live topology. The gate above guarantees this
    # resolves clean, so we re-resolve here only to obtain the concrete plan
    # object the apply steps (serving.json, models.json, routing, provision)
    # operate on — not to re-validate.
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
            CONFIG_YAML, lambda d: _update_hermes_config(d, plan, _http_get=_http_get))
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
        # When the template carries a routing: block, that block is the SOLE
        # authority for the delegation consumer, so Step 5's delegation/fallback
        # rewrite is skipped (routing handles present; omission means untouched).
        # Profiles still rewritten — routing has no 'profiles' consumer.
        wm = _update_worker_model_ids(plan, skip_delegation=(tpl.routing is not None),
                                      _http_get=_http_get)
        result["steps"].append({
            "step": "worker-model-ids",
            "status": "ok",
            "model_id": wm["model_id"],
            "config_changed": wm["config_changed"],
            "profiles_changed": wm["profiles_changed"],
        })

        # Step 5b: Apply the template's routing: block (T3). Resolves each
        # symbolic target to its live endpoint and writes the consumer's config
        # keys with probe-before-write. Runs AFTER the worker-model rewrite so an
        # explicit routing target always wins over the default worker re-aim.
        # A routing config change also counts toward the gateway-restart decision.
        ro = _apply_routing(tpl, plan, _http_get=_http_get)
        if ro["changed"]:
            config_changed = True
        result["steps"].append({
            "step": "routing",
            "status": "ok",
            "keys_written": ro["keys_written"],
            "config_changed": ro["changed"],
            "notes": ro["notes"],
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


# ── Layer 1: structural validation (offline, no cluster state, no resolver) ──

# Recognised template schema versions. The engine understands v2 (topology-free,
# parsed unchanged) and v3 (adds explicit nodes/allow_colocation/routing). Any
# other version is a structural error — a template is not trustworthy if we
# cannot decide what its keys mean.
RECOGNISED_VERSIONS = (2, 3)

# Known keys per level. Anything else is a typo and a hard structural error
# (spec: "unknown keys rejected"). Kept in sync with template_intent schema.
_KNOWN_TOP_KEYS = {"name", "version", "description", "orchestrator", "families",
                   "routing"}
_KNOWN_MODEL_KEYS = {"recipe", "tp", "pp", "nodes"}
_KNOWN_FAMILY_KEYS = {"name", "models", "workers", "proxy", "nodes",
                      "allow_colocation"}


def _cluster_node_ips() -> List[str]:
    """Compute node IPs defined in cluster.json (gateway + workers), or [] on a
    missing/corrupt file.

    This is STATIC config, not live cluster state — reading it needs no
    discovery, no resolver, no ssh. NAS storage devices are deliberately NOT
    included (a vLLM model cannot be placed on a storage box), so naming a NAS
    IP in ``nodes:`` is a structural error.
    """
    data = read_json(CLUSTER_JSON)
    if not isinstance(data, dict):
        return []
    ips: List[str] = []
    gw = data.get("gateway")
    if isinstance(gw, dict) and gw.get("ip"):
        ips.append(str(gw["ip"]))
    for w in data.get("workers") or []:
        if isinstance(w, dict) and w.get("ip"):
            ips.append(str(w["ip"]))
    return ips


def _structural_validate(raw: dict, tpl: Any,
                         cluster_ips: Optional[List[str]] = None,
                         recipe_exists: Optional[Any] = None) -> Tuple[List[str], List[str]]:
    """Layer 1 — structural validation. Offline: uses only the static
    cluster.json node list, never discovery/resolver/live state. Returns
    ``(errors, warnings)``. Every error names the offending unit and value so a
    reviewer can fix the template without re-deriving which key is wrong.

    Checks (spec Validation → Layer 1):
      - version recognised
      - unknown keys rejected (typo protection), at every level
      - every node in ``nodes:`` exists in cluster.json
      - ``len(nodes) == tp`` for each unit that names nodes
      - no node claimed by two units unless BOTH set allow_colocation: true
        (when permitted, a warning about VRAM contention, not an error)
      - routing targets resolve to a unit the template defines
      - recipe paths exist on disk

    ``raw`` is the original YAML dict (unknown-key detection needs keys that
    parsing already discarded); ``tpl`` is the parsed ClusterTemplate.
    """
    errors: List[str] = []
    warnings: List[str] = []
    defined = set(cluster_ips) if cluster_ips is not None else set(_cluster_node_ips())

    # ── version recognised ────────────────────────────────────────────────
    if tpl.version not in RECOGNISED_VERSIONS:
        errors.append(
            f"version {tpl.version}: unrecognised (supported versions: "
            f"{', '.join(str(v) for v in RECOGNISED_VERSIONS)})")

    # ── unknown keys (typo protection), every level ───────────────────────
    for k in raw:
        if k not in _KNOWN_TOP_KEYS:
            errors.append(f"template '{tpl.name}': unknown key '{k}'")
    orch_raw = raw.get("orchestrator")
    if isinstance(orch_raw, dict):
        for k in orch_raw:
            if k not in _KNOWN_MODEL_KEYS:
                errors.append(f"orchestrator: unknown key '{k}'")
    fam_raw_list = raw.get("families") or []
    for i, fam_raw in enumerate(fam_raw_list):
        fam = tpl.families[i] if i < len(tpl.families) else None
        label = f"family '{fam.name}'" if fam else f"families[{i}]"
        if not isinstance(fam_raw, dict):
            continue
        for k in fam_raw:
            if k not in _KNOWN_FAMILY_KEYS:
                errors.append(f"{label}: unknown key '{k}'")
        models_raw = fam_raw.get("models")
        if isinstance(models_raw, list):
            for j, m in enumerate(models_raw):
                if isinstance(m, dict):
                    for k in m:
                        if k not in _KNOWN_MODEL_KEYS:
                            errors.append(
                                f"{label} model {j}: unknown key '{k}'")

    # ── recipe paths exist on disk ────────────────────────────────────────
    def _recipe_ok(recipe: str, label: str) -> None:
        exists = (recipe_exists(recipe) if recipe_exists is not None
                  else Path(os.path.expanduser(recipe)).is_file())
        if not exists:
            errors.append(f"{label} recipe not found: {recipe}")

    _recipe_ok(tpl.orchestrator.recipe, "orchestrator")
    for fam in tpl.families:
        for m in fam.models:
            _recipe_ok(m.recipe, f"family '{fam.name}'")

    # ── explicit nodes: exist in cluster.json + len(nodes) == tp ──────────
    # Collect every named span with its colocation flag for the collision check.
    # (full_label, bare_name, node_list, allow_colocation)
    spans: List[Tuple[str, str, List[str], bool]] = []
    if tpl.orchestrator.nodes is not None:
        n = tpl.orchestrator.nodes
        if len(n) != tpl.orchestrator.tp:
            errors.append(
                f"orchestrator: {len(n)} nodes listed but tp={tpl.orchestrator.tp}")
        spans.append(("orchestrator", "orchestrator", n, False))
    for fam in tpl.families:
        if fam.nodes is None:
            continue
        for m in fam.models:
            if len(fam.nodes) != m.tp:
                errors.append(
                    f"family '{fam.name}': {len(fam.nodes)} nodes listed but "
                    f"model tp={m.tp}")
        # Colocation is the cross-UNIT question; the family's own flag is what
        # "both units must set allow_colocation" refers to.
        spans.append((f"family '{fam.name}'", fam.name, fam.nodes,
                      fam.allow_colocation))

    # Every named node must be a compute node defined in cluster.json.
    for label, _, nodes, _ in spans:
        for node in nodes:
            if node not in defined:
                errors.append(
                    f"node '{node}' in {label} is not defined in cluster.json")

    # ── colocation: no node claimed by two units unless BOTH say allow ────
    # The message uses BARE unit names ('orchestrator' / 'reasoning') to match
    # the spec's exact phrasing.
    claims: Dict[str, Tuple[str, bool]] = {}
    for _, bare, nodes, allow in spans:
        for node in nodes:
            if node in claims:
                prev_bare, prev_allow = claims[node]
                if allow and prev_allow:
                    warnings.append(
                        f"node '{node}' claimed by both '{prev_bare}' and "
                        f"'{bare}' "
                        f"(allow_colocation: true on both) — VRAM contention")
                else:
                    errors.append(
                        f"node '{node}' claimed by both '{prev_bare}' and "
                        f"'{bare}' "
                        f"(set allow_colocation: true on both to permit)")
            else:
                claims[node] = (bare, allow)

    # ── routing targets resolve to a unit the template defines ────────────
    if tpl.routing is not None:
        defined_units = {"orchestrator"} | {f"family-{f.name}" for f in tpl.families}
        for consumer, target in tpl.routing.items():
            if target in defined_units:
                continue
            if target.startswith("family-"):
                errors.append(
                    f"routing.{consumer} -> '{target}': no such family in "
                    f"this template")
            else:
                errors.append(
                    f"routing.{consumer} -> '{target}': must be 'orchestrator' "
                    f"or 'family-<name>'")

    return errors, warnings


def validate_template(template_name: str, *,
                      structural_only: bool = False) -> dict:
    """Two-layer preflight of a template — answers different questions.

    Layer 1 (structural) runs ALWAYS and is OFFLINE: version recognised, unknown
    keys rejected, nodes exist in cluster.json, len(nodes)==tp, no unflagged
    colocation, routing targets resolve, recipes exist. It needs no live cluster
    and no resolver, so the template that is currently running the live fleet
    validates deterministically instead of failing on a resolver pool=[] error.

    Layer 2 (placement) requires live cluster state and uses the resolver —
    capacity/occupancy/availability ("can these units be placed right now").
    ``structural_only=True`` skips it (usable in CI / when the cluster is down).

    Returns the spec's JSON shape: ``{template, structural: {ok, errors,
    warnings}, placement: {ok, errors, warnings}, ok}`` where top-level ``ok``
    is the AND of both layers. No writes.
    """
    # Load raw dict + parsed intent ONCE. Raw is needed for unknown-key
    # detection (parse discards unknown keys); parsed for the semantic checks.
    try:
        path = _find_template_file(template_name)
        if path is None:
            raise FileNotFoundError(f"Template not found: {template_name}")
        import yaml
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        tpl = _ti().ClusterTemplate.from_dict(raw)
    except FileNotFoundError as e:
        err = str(e)
        return {"template": template_name,
                "structural": {"ok": False, "errors": [err], "warnings": []},
                "placement": {"ok": True, "errors": [], "warnings": []},
                "ok": False}
    except _ti().TemplateIntentError as e:
        err = str(e)
        return {"template": template_name,
                "structural": {"ok": False, "errors": [err], "warnings": []},
                "placement": {"ok": True, "errors": [], "warnings": []},
                "ok": False}

    # ── Layer 1: structural (offline) ─────────────────────────────────────
    s_errors, s_warnings = _structural_validate(raw, tpl)
    structural = {"ok": not s_errors, "errors": s_errors, "warnings": s_warnings}

    # ── Layer 2: placement (resolver-dependent, live) ─────────────────────
    if structural_only:
        placement = {"ok": True, "errors": [], "warnings": [],
                     "skipped": True}
        return {"template": tpl.name, "structural": structural,
                "placement": placement,
                "ok": structural["ok"]}

    try:
        plan = _resolve(template_name, probe=True)
    except FileNotFoundError as e:
        placement = {"ok": False, "errors": [str(e)], "warnings": []}
    except _ti().TemplateIntentError as e:
        placement = {"ok": False, "errors": [str(e)], "warnings": []}
    except Exception as e:
        placement = {"ok": False, "errors": [f"template invalid: {e}"],
                     "warnings": []}
    else:
        problems = validate_resolved_plan(plan)
        placement = {"ok": not problems, "errors": problems, "warnings": []}

    return {"template": tpl.name, "structural": structural,
            "placement": placement,
            "ok": structural["ok"] and placement["ok"]}


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


def _update_hermes_config(config: dict, plan: Any, *, _http_get=None) -> dict:
    """Update Hermes config.yaml providers from a resolved plan.

    Idempotent: providers keyed by name and rebuilt, so re-running apply never
    duplicates (a prior version appended on every call, corrupting config).

    Also updates the top-level ``model`` block so the orchestrator's served
    model id and base_url are correct — vLLM is strict about model identity.
    The model id written is probe-resolved, exactly parallel to the routing and
    worker paths: the candidate is the orchestrator's STABLE LOGICAL ALIAS
    (``_unit_alias`` → ``orchestrator-model``), and the probe decides what is
    actually written — the alias when the orchestrator endpoint advertises it,
    else the concrete id it serves, else NOTHING (never write an id the endpoint
    cannot resolve). ``_http_get`` is injectable for tests; default is a real
    network probe."""
    o = plan.orchestrator
    orch_url = f"http://{o.node}:{o.port}/v1"

    # ── Top-level model block ────────────────────────────────────────
    model_cfg = config.setdefault("model", {})
    model_cfg["base_url"] = orch_url
    # Preserve existing provider; default to "custom" if absent
    if "provider" not in model_cfg:
        model_cfg["provider"] = "custom"

    # PROBE-BEFORE-WRITE for model.default (same guard as the routing and worker
    # paths). Candidate = the orchestrator's stable alias; write the alias when
    # the orchestrator endpoint advertises it, else the single concrete id it
    # serves, else leave model.default untouched — never write an id the
    # endpoint cannot resolve.
    status, served = _probe_served_models(orch_url, _http_get=_http_get)
    resolved = _resolve_worker_model_id(
        _unit_alias(o, plan), served if status == "ok" else [])
    if resolved is not None:
        model_cfg["default"] = resolved

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
    """The worker/family model CANDIDATE — the stable LOGICAL ALIAS the worker
    proxy family should advertise.

    Returns the unit's role-identity alias via ``_unit_alias``
    (``u is plan.orchestrator`` → ``worker-model``) for the family that owns the
    worker proxy (see ``_owned_worker_family``) — NOT the recipe's concrete id.
    All units in that family share the served model. The probe in
    ``_update_worker_model_ids`` then decides whether the alias or the concrete
    id the proxy actually serves is written. Returns None when no family owns
    the worker proxy — callers must then leave worker ids untouched.
    """
    fam = _owned_worker_family(plan)
    return _unit_alias(fam.units[0], plan) if fam is not None else None


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
    """Probe-before-write: resolve the model id to write against a proxy.

    ``model_id`` is the probe candidate — the unit's STABLE LOGICAL ALIAS
    (``orchestrator-model`` / ``worker-model``, via ``_unit_alias``). The proxy
    may serve the alias, the concrete id, or both. Writing an id the endpoint
    does not advertise makes every call 404.

    So we only ever write an id the endpoint actually serves:
      - if the declared ``model_id`` is served → use it (the alias or concrete);
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
                             _http_get=None, skip_delegation: bool = False) -> dict:
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
    serves (``_resolve_worker_model_id``). The probe candidate is the worker
    unit's stable ALIAS (``worker-model``, via ``_unit_alias``); the proxy may
    advertise the alias, the concrete id, or both. The alias is written when the
    proxy advertises it (post-config-refresh norm); the CONCRETE id the proxy
    serves is written when it does not. If the probe fails or no resolvable id
    is found, nothing is written (the id would 404 every delegated call).
    ``_http_get`` is injectable for tests; default is a real network probe.

    ``skip_delegation=True`` (set by apply_template when the template carries a
    ``routing:`` block): the config.yaml delegation/fallback rewrite is SKIPPED,
    because the routing block is then the sole authority for the ``delegation``
    consumer (a present key routes it; an OMITTED key must leave it untouched —
    the routing hard requirement). The worker role profiles rewrite still runs:
    routing has no ``profiles`` consumer, so it cannot express that concern.

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

    if skip_delegation:
        # routing block governs the delegation consumer — do NOT touch
        # delegation/fallback here. Profiles still rewritten below.
        result["delegation_routed"] = True
    else:
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


# ── T3: apply the template's routing: block to config.yaml ────────────────
# Symbolic target -> that unit's live endpoint, with probe-before-write on the
# model id and strict omission semantics (an absent key is NEVER written).

# The 8 TEXT auxiliaries routing may target. Deliberately NOT vision or
# web_extract — those need capability-specific providers (spec schema table).
ROUTING_AUX_TEXT_TASKS = [
    "kanban_decomposer", "triage_specifier", "profile_describer", "curator",
    "title_generation", "skills_hub", "approval", "mcp",
]

# Every consumer the routing block MAY target (the recognisable set in
# ``_routing_config_keys``). Anything outside this set is a typo apply refuses
# to invent a key for. Used by preview to expose OMISSION as routing_untouched
# rather than folklore.
ROUTING_CONSUMERS = ["delegation", "compaction", "auxiliaries"]


def _routing_target_endpoint(target: str, plan: Any) -> Tuple[str, str]:
    """Resolve a symbolic routing target to that unit's live endpoint.

    Returns ``(base_url, candidate_model_id)``. ``orchestrator`` -> orchestrator
    host:port; ``family-<name>`` -> that family's proxy port when ``proxy: true``
    (``proxy_port`` is not None), else its primary node's endpoint
    (``units[0].node:units[0].port``). The model candidate is the unit's STABLE
    LOGICAL ALIAS (``orchestrator-model`` / ``worker-model``, decided by role
    identity via ``_unit_alias``) — NOT the recipe's concrete id. The probe in
    ``_routing_model_to_write`` then writes the alias when the endpoint
    advertises it and falls back to the concrete id when it does not, so apply
    converges config to the alias exactly as the doctor alias-migration does.

    A target that names no unit the template defines is a hard structural error
    (spec: ``routing.delegation -> 'family-coding': no such family``).
    """
    if target == "orchestrator":
        o = plan.orchestrator
        return f"http://{o.node}:{o.port}/v1", _unit_alias(o, plan)
    if target.startswith("family-"):
        name = target[len("family-"):]
        for fam in plan.families:
            if fam.name == name:
                if fam.proxy_port is not None:
                    # route to the family's proxy; candidate = the family's alias
                    model = _unit_alias(fam.units[0], plan) if fam.units else ""
                    return f"http://localhost:{fam.proxy_port}/v1", model
                # no proxy: the family's primary node exposes the endpoint
                u = fam.units[0]
                return f"http://{u.node}:{u.port}/v1", _unit_alias(u, plan)
        # Dangling routing target -> hard block, never silently ignored.
        raise _ti().TemplateIntentError(
            f"routing target '{target}': no such family in this template")
    raise _ti().TemplateIntentError(
        f"routing target '{target}': must be 'orchestrator' or 'family-<name>'")


def _routing_model_to_write(candidate: str, base_url: str, *, _http_get=None) -> Tuple[Optional[str], str, list]:
    """Probe-before-write: resolve the model id to actually write at ``base_url``.

    The endpoint (esp. a sparkrun proxy) may serve ONLY the concrete id, not the
    logical alias ``candidate``. Writing an alias the endpoint does not advertise
    makes every call 404. So the id written is probe-resolved:
      - candidate IS served -> write candidate (alias or concrete);
      - else exactly one served id -> write that (the concrete fallback);
      - else -> None (refuse: empty or ambiguous /endpoints served list).

    ``None`` for status != "ok" too (unreachable/error) — never write a model id
    we could not confirm the endpoint resolves (standing 404 guard, same posture
    as ``_update_worker_model_ids``). Returns ``(resolved_model, probe_status,
    served_ids)``.
    """
    status, served = _probe_served_models(base_url, _http_get=_http_get)
    if status != "ok":
        return None, status, list(served) if isinstance(served, list) else []
    resolved = _resolve_worker_model_id(candidate, served)
    return resolved, status, list(served)


def _set_config_path(config: dict, path: str, value) -> None:
    """Set ``config['a']['b']['c'] = value`` (dotted path), creating dicts.

    Mutates ``config`` in place. Does not create the path on a scalar/non-dict
    blockage (rare malformed config) — it raises so apply surfaces the error.
    """
    parts = path.split(".")
    node = config
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _routing_config_keys(consumer: str):
    """Map a consumer -> list of (base_url_path, model_path) config key paths.

    ``None`` for an unrecognised consumer — it is NEVER written (the spec table
    is exhaustive; unknown keys are a typo, and apply must not invent keys).
    """
    if consumer == "delegation":
        return [("delegation.base_url", "delegation.model")]
    if consumer == "compaction":
        return [("auxiliary.compression.base_url", "auxiliary.compression.model")]
    if consumer == "auxiliaries":
        return [(f"auxiliary.{t}.base_url", f"auxiliary.{t}.model")
                for t in ROUTING_AUX_TEXT_TASKS]
    return None


def _routing_update_config(config: dict, tpl: Any, plan: Any, *, _http_get=None,
                           written: Optional[list] = None) -> dict:
    """Mutate ``config`` for every routing consumer PRESENT in ``tpl.routing``.

    Omission semantics (STRICTER than fill-empty): a consumer absent from
    ``tpl.routing`` — or the whole block absent, which the caller checks before
    invoking — is NEVER written, regardless of the live value (even if blank).
    Only consumers explicitly stated in the routing block are touched.

    For a present consumer, ``base_url`` is the resolved symbolic endpoint and
    ``model`` is probe-resolved (see ``_routing_model_to_write``): the alias is
    written when the endpoint advertises it, else the single concrete id, else
    the consumer is refused (nothing written) rather than writing an id the
    endpoint might 404 on. ``written`` collects the concrete key paths touched
    (for tests / reporting).
    """
    written = written if written is not None else []
    for consumer, target in (tpl.routing or {}).items():
        keys = _routing_config_keys(consumer)
        if keys is None:
            continue  # unrecognised consumer: never invent a config key
        try:
            base_url, candidate = _routing_target_endpoint(target, plan)
        except _ti().TemplateIntentError as e:
            written.append(f"{consumer}:error:{e}")
            raise
        model, status, served = _routing_model_to_write(
            candidate, base_url, _http_get=_http_get)
        if model is None:
            # Probe could not confirm a model the endpoint resolves. Refuse this
            # consumer (touch NOTHING) — never write an id the endpoint 404s on.
            written.append(f"{consumer}:refused:({status})")
            continue
        for base_path, model_path in keys:
            _set_config_path(config, base_path, base_url)
            _set_config_path(config, model_path, model)
            written.append(base_path)
            written.append(model_path)
    return config


def _apply_routing(tpl: Any, plan: Any, *, config_yaml: Optional[Path] = None,
                   _http_get=None) -> dict:
    """Apply the template's ``routing:`` block to config.yaml (atomic).

    Whole block absent (``tpl.routing is None``) writes NOTHING and returns
    ``{"keys_written": [], "changed": False}``. Otherwise one atomic_yaml_update
    over config.yaml mutates only the consumers the block states — an omitted
    consumer keeps its live value byte-for-byte (hard requirement). ``_http_get``
    injectable for tests (probe-before-write without the network).

    Returns ``{"keys_written": [...], "changed": bool, "notes": [...]}``.
    """
    notes: list = []
    if tpl.routing is None:
        return {"keys_written": [], "changed": False, "notes": notes}
    written: list = []
    conf = config_yaml or CONFIG_YAML
    _, changed = atomic_yaml_update(
        conf, lambda d: _routing_update_config(
            d, tpl, plan, _http_get=_http_get, written=written))
    for w in written:
        if ":refused:" in w or ":error:" in w:
            notes.append(w)
    return {"keys_written": written, "changed": changed, "notes": notes}


def _preview_routing(tpl: Any, plan: Any, *, _http_get=None) -> dict:
    """Build preview's routing disclosure (READ-ONLY).

    Reuses the EXACT SAME three resolution helpers apply uses —
    ``_routing_target_endpoint``, ``_routing_model_to_write``,
    ``_routing_config_keys`` — per declared consumer, so preview can never
    drift from what apply would write. Probe-aware: the ``model`` shown is the
    id that would ACTUALLY be written (the concrete fallback when the endpoint
    does not advertise the alias), not the aspiration. ``_http_get`` is
    injectable for tests, mirroring the apply path.

    Nothing here writes config, provisions, or restarts anything — probing
    ``/v1/models`` is a read-only GET.

    Returns ``{"routing": [...], "routing_untouched": [...]}``. When the whole
    block is absent (``tpl.routing is None``) ``routing`` is ``[]`` and the
    caller omits the section from the preview output entirely.
    """
    if tpl.routing is None:
        return {"routing": [], "routing_untouched": []}
    entries = []
    for consumer, target in tpl.routing.items():
        key_paths = _routing_config_keys(consumer)
        if key_paths is None:
            continue  # unrecognised consumer — apply never invents a key for it
        entry = {"consumer": consumer, "target": target}
        try:
            base_url, candidate = _routing_target_endpoint(target, plan)
        except _ti().TemplateIntentError as exc:
            entry["base_url"] = None
            entry["model"] = None
            entry["note"] = f"error: {exc}"
        else:
            model, status, _served = _routing_model_to_write(
                candidate, base_url, _http_get=_http_get)
            entry["base_url"] = base_url
            entry["model"] = model
            if model is None:
                # apply refuses this consumer (writes NOTHING) when the probe
                # cannot confirm a model the endpoint resolves — say so.
                entry["note"] = f"refused:({status})"
        entry["keys"] = [path for base_path, model_path in key_paths
                         for path in (base_path, model_path)]
        entries.append(entry)
    declared = set(tpl.routing.keys())
    untouched = [c for c in ROUTING_CONSUMERS if c not in declared]
    return {"routing": entries, "routing_untouched": untouched}


def _describe_config_changes(plan, current_models: Optional[dict]) -> list:
    """Describe what config changes will be made (from a resolved plan)."""
    o = plan.orchestrator
    # Disclose the orchestrator's STABLE LOGICAL ALIAS (the id that will be
    # written to model.default), not the recipe's concrete id — same role-identity
    # helper provisioning and apply use, so preview can never drift from apply.
    changes = [f"  orchestrator: {o.node}:{o.port} (model: {_unit_alias(o, plan)})"]
    for fam in plan.families:
        port = fam.proxy_port if fam.proxy_port is not None else "—"
        # Node count covers the FULL tp span of every unit, not just primaries —
        # a single tp=2 unit spans 2 nodes, so report 2, not 1 (t_16dcceb4).
        nodes = sorted({n for u in fam.units for n in u.nodes})
        changes.append(
            f"  family-{fam.name}: localhost:{port} "
            f"({len(fam.units)} units, {len(nodes)} nodes)")
    return changes


def _diff_serving_summary(current: Optional[dict], new: dict) -> str:
    """Human-readable summary of serving.json changes."""
    old_units = len(current.get("units", [])) if current else 0
    new_units = len(new.get("units", []))
    return f"{old_units} units → {new_units} units"


def _serving_signature(unit: dict) -> tuple:
    """A unit's concrete serving signature — what it takes to run it.

    (role, model, sorted nodes, port, tp, pp) fully describes WHERE and HOW a
    unit serves, so two units with an equal signature are interchangeable and
    one with a different signature is a real change. Missing tp/pp default to 1
    so an orchestrator (which has no tp/pp keys) compares cleanly against a
    worker that happens to be 1x1.
    """
    return (
        unit.get("role", ""),
        unit.get("model", ""),
        tuple(sorted(unit.get("nodes") or [])),
        int(unit.get("port", 0)),
        int(unit.get("tp", 1) or 1),
        int(unit.get("pp", 1) or 1),
    )


def _serving_workload(unit: dict) -> tuple:
    """The workload identity: role + model. Survives relocation/rescaling.

    Two units with the same workload but different signatures are the SAME
    model being moved or rescaled — reported as a MOVE, not stop+start as two
    unrelated events.
    """
    return (unit.get("role", ""), unit.get("model", ""))


def _trim_serving_unit(unit: dict) -> dict:
    """A compact, stable view of a serving unit for the delta output — only the
    keys that identify it and matter to an operator, in a fixed order."""
    return {
        "id": unit.get("id"),
        "role": unit.get("role"),
        "model": unit.get("model"),
        "nodes": unit.get("nodes") or [],
        "port": unit.get("port"),
        "tp": unit.get("tp"),
        "pp": unit.get("pp"),
        "family": unit.get("family"),
    }


def _diff_serving_delta(current: Optional[dict], new: dict) -> dict:
    """Compute the unit-level delta between the LIVE serving.json and the NEW
    serving units an apply would write.

    Returns:
      start[]        units present in NEW only (brand-new serving units)
      stop[]         units present in CURRENT only (would be torn down)
      move[]         workloads present in BOTH whose placement/scaling changes
                     (model stays, but nodes/port/tp/pp change)
      unchanged[]    units with an identical serving signature in both
      affected_nodes the sorted union of every node across start/stop/move

    Matching is by full serving signature for start/stop/unchanged, and by
    workload (role+model) for MOVE so a relocation is one event ("moves") and
    not a phantom stop + start. When CURRENT is absent/corrupt every new unit
    is a START and no stop/move is reported — the app must not over-claim
    teardowns it cannot see.
    """
    current_units = (current or {}).get("units") or []
    new_units = new.get("units") or []

    # GUARD against malformed unit lists (non-dict entries) — never crash the
    # preview over a bad serving.json.
    current_units = [u for u in current_units if isinstance(u, dict)]
    new_units = [u for u in new_units if isinstance(u, dict)]

    # Pair up same-workload units 1:1 so a family resizing (grow/shrink) and a
    # relocation never cross-contaminate. A unit whose exact signature also
    # exists in CURRENT is UNCHANGED; otherwise, if the workload has an unused
    # CURRENT unit, it's a MOVE (the model relocates/rescales); else it's a
    # START. Leftover CURRENT units are STOPS.
    start = []
    stop = []
    unchanged = []
    moves = []
    used_current = set()          # indices of current units already consumed
    matched_new = set()           # object ids of new units already accounted for

    # Index current units by signature so each identical pair consumes a
    # distinct current unit (a family grown from 1→2 units must keep its
    # existing unit UNCHANGED, not steal it as a MOVE source for the new one).
    current_by_sig = {}
    for i, cu in enumerate(current_units):
        current_by_sig.setdefault(_serving_signature(cu), []).append(i)

    # PASS 1 — unchanged: a new unit whose exact signature has a free CURRENT
    # partner is unchanged (identical placement/scaling, no action).
    for u in new_units:
        cand = [i for i in current_by_sig.get(_serving_signature(u), [])
                if i not in used_current]
        if not cand:
            continue
        used_current.add(cand[0])
        matched_new.add(id(u))
        unchanged.append(_trim_serving_unit(u))

    # PASS 2 — move / start: every remaining new unit either relocates/rescales
    # an existing CURRENT unit with the same workload (MOVE), or is brand-new
    # (START).
    current_by_workload = {}
    for i, cu in enumerate(current_units):
        if i not in used_current:
            current_by_workload.setdefault(_serving_workload(cu), []).append(i)
    for u in new_units:
        if id(u) in matched_new:
            continue  # already unchanged
        workload = _serving_workload(u)
        open_current = [i for i in current_by_workload.get(workload, [])
                        if i not in used_current]
        if not open_current:
            start.append(_trim_serving_unit(u))   # brand-new serving unit
            continue
        # Same workload already serving but with a different signature → MOVE.
        ci = open_current[0]
        used_current.add(ci)
        moves.append(_move_record(current_units[ci], u))

    # PASS 3 — stop: a CURRENT unit with no matching new unit (by signature or
    # as a move source) would be torn down.
    new_by_sig = {_serving_signature(u) for u in new_units}
    for i, cu in enumerate(current_units):
        if i in used_current or _serving_signature(cu) in new_by_sig:
            continue
        stop.append(_trim_serving_unit(cu))

    affected = set()
    for u in start:
        affected.update(u["nodes"])
    for u in stop:
        affected.update(u["nodes"])
    for m in moves:
        affected.update(m["from_nodes"] or [])
        affected.update(m["to_nodes"] or [])

    return {
        "start": start,
        "stop": stop,
        "move": moves,
        "unchanged": unchanged,
        "affected_nodes": sorted(affected),
    }


def _move_record(current_unit, new_unit) -> dict:
    """Build the from→to record for a workload whose placement changed."""
    rec = {"model": (current_unit or {}).get("model") or (new_unit or {}).get("model"),
           "role": (current_unit or {}).get("role") or (new_unit or {}).get("role")}
    rec["from_nodes"] = (current_unit or {}).get("nodes") or []
    rec["from_port"] = (current_unit or {}).get("port")
    rec["from_tp"] = (current_unit or {}).get("tp")
    rec["from_pp"] = (current_unit or {}).get("pp")
    rec["to_nodes"] = (new_unit or {}).get("nodes") or []
    rec["to_port"] = (new_unit or {}).get("port")
    rec["to_tp"] = (new_unit or {}).get("tp")
    rec["to_pp"] = (new_unit or {}).get("pp")
    return rec


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
