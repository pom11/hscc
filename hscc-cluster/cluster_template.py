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
import math
import os
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import _theme  # guarded themed render helper (peer cli_theme + neutral fallback)

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


def _family_provider_alias(fam: Any, plan: Any) -> str:
    """The model-id alias a ``family-*`` provider must declare.

    A provider's ``model.default`` is a MODEL ID, not an endpoint — the old
    writer put ``localhost:{proxy_port}`` there, which no endpoint resolves as
    a model. The family proxy load-balances its units, and every unit
    advertises its role alias via ``_unit_serve_names``; routing keeps
    single-valued aliases (see ``_unit_serve_names``), so the proxy answers to
    the units' alias. Every unit in a family shares one recipe and therefore
    one alias, so units[0] is representative; a unit that IS the orchestrator
    (co-location/degenerate plans) yields ``orchestrator-model`` via
    ``_unit_alias`` — the same identity rule everywhere. A family with no
    units has no proxy target at all: return the generic alias rather than a
    host:port (the provider entry itself is a config pointer, and the stale-
    family drop keeps entries this plan does not define out).
    """
    units = getattr(fam, "units", None) or []
    return _unit_alias(units[0], plan) if units else "worker-model"


def _unit_serve_names(u: Any, plan: Any) -> str:
    """Alias token(s) this unit advertises via ``--served-model-name``.

    Normally the unit's single role alias (``_unit_alias``). Under a
    ``uniform_pool`` template every unit runs the SAME recipe and must be able
    to serve either role, so it advertises BOTH aliases. Space-separated for
    the same reason the concrete+alias pair is: sparkrun bash-executes the
    rendered command, so spaces arrive as separate argv tokens for vLLM's
    ``nargs='+'`` ``--served-model-name`` and every name registers.

    Routing deliberately still resolves ONE name (``_unit_alias``) — that name
    now resolves to every unit in the pool, which is the whole point. Keeping
    routing single-valued means the probe-before-write gate keeps working
    unchanged.
    """
    if getattr(plan, "uniform_pool", False):
        return "worker-model orchestrator-model"
    return _unit_alias(u, plan)


def _render_serve_cmd(cluster: str, hosts_arg: str, port: int, recipe: str,
                      alias: str, tp: int, model: str = "",
                      gpu_mem: Optional[float] = None) -> List[str]:
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
    # ALWAYS emit --tp, including tp=1. Omitting it let the RECIPE's own
    # `tensor_parallel` default win, so a template could not pin a
    # tp=2-by-default recipe down to a single node — it silently tried to span
    # two. Being explicit makes the template the authority.
    #
    # This is safe against spurious drift only because comparison is
    # normalized: `_serve_cmd_flags` defaults a missing --tp to "1", so a unit
    # recorded before this change still compares equal and is NOT recreated.
    cmd.extend(["--tp", str(tp)])
    # Only when the template says so — omitted means the recipe's own default
    # wins, which is the long-standing behaviour and what every unit that does
    # not co-locate wants.
    if gpu_mem is not None:
        cmd.extend(["--gpu-mem", str(gpu_mem)])
    return cmd


def _serve_cmd_flags(cmd: List[str]) -> dict:
    """Flag/value map of a rendered serve command, NORMALIZED for comparison.

    Normalization exists so that a purely cosmetic change to how a command is
    rendered never reads as fleet drift. Currently: a missing ``--tp`` means
    ``--tp 1``, because commands recorded before tp became explicit omit it
    while an equivalent command rendered today includes it.
    """
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
    out.setdefault("--tp", "1")
    return out


def serve_cmds_equivalent(old_cmd: List[str], new_cmd: List[str]) -> bool:
    """True when two rendered serve commands mean the same launch.

    Used instead of ``==`` for drift detection: a recorded command and a
    freshly rendered one can differ token-for-token while being identical in
    effect (see :func:`_serve_cmd_flags`).
    """
    return _serve_cmd_flags(old_cmd) == _serve_cmd_flags(new_cmd)


def _diff_serve_cmds(old_cmd: List[str], new_cmd: List[str]) -> str:
    """Human-readable description of WHAT changed between two rendered serve
    commands (flag: old -> new), or '' when they are equivalent."""
    om, nm = _serve_cmd_flags(old_cmd), _serve_cmd_flags(new_cmd)
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
        alias = _unit_serve_names(u, plan)
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
        # INVARIANT: tp must not exceed the span. A tp=N unit pinned to fewer
        # than N hosts can NEVER start — vLLM rejects it at argument validation
        # ("World size (N) is larger than the number of available GPUs (1) in
        # this node"), so the container crash-loops forever while holding VRAM
        # and a restart slot on a node that is usually a tp PEER of a healthy
        # unit. Observed twice: an auto-heal force-recreate-via-template-apply
        # after a power outage left a solo tp=2 orphan on .244 alongside the
        # real orchestrator span. Refuse the launch instead of spawning a
        # container that is guaranteed to fail.
        if isinstance(tp, int) and tp > len(want_span):
            result["failed"].append(
                {"node": hosts_arg, "port": port, "recipe": recipe,
                 "error": (f"refused: tp={tp} exceeds span of {len(want_span)} "
                           f"host(s) ({hosts_arg or 'none'}) — vLLM would reject "
                           "this at startup and the container would crash-loop")})
            continue
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
            rendered = _render_serve_cmd(
                cluster, hosts_arg, port, recipe, alias, tp,
                getattr(unit, "model", ""),
                getattr(unit, "gpu_memory_utilization", None))
            rec = serving_records.get(unit_id)
            rec_cmd = (rec or {}).get("serve_cmd")
            if rec_cmd is None:
                drift_unchecked.append(f"{hosts_arg}:{port}:{recipe.split('/')[-1]}")
            elif not serve_cmds_equivalent(rec_cmd, rendered):
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
            cmd = _render_serve_cmd(
                cluster, hosts_arg, port, recipe, alias, tp,
                getattr(unit, "model", ""),
                getattr(unit, "gpu_memory_utilization", None))
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

    # Endpoints disclosure (v4 §1.5): the same helpers `_apply_endpoints` uses,
    # so the preview cannot advertise a route apply would not write. Only the
    # DECLARED consumers appear — a defaulted or omitted one is not a change the
    # operator is about to make. Read-only: probing /v1/models is a GET.
    # same profiles_dir apply passes, so preview's disclosure is built against
    # the profiles that actually exist (§1.5: preview must not drift).
    ep_rows = _preview_endpoints(resolved, _endpoints_decision(
        resolved, profiles_dir=PROFILES_DIR), _http_get=_http_get)
    if ep_rows:
        out["endpoints"] = ep_rows

    # Balancer disclosure: what proxy.yaml region apply would write through
    # sparkrun, and whether that would require the operator restart unit. The
    # preview NEVER writes and NEVER restarts.
    bal_policy, bal_source = balancing_declaration(tpl, resolved)
    if bal_policy is not None:
        out["proxy_balancing"] = {
            "policy": bal_policy, "source": bal_source,
            "region": ".".join(BALANCING_REGION) + ".balancing",
            "note": ("written through sparkrun's own ProxyConfig write path; a "
                     "changed policy needs the operator proxy restart unit "
                     "(docs/audits/sparkrun_proxy_venv_cutover.sh) — apply "
                     "never restarts the proxy"),
        }

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

    v4 plan-level leg (spec §5): under ``uniform_pool`` every declared endpoint
    target must resolve to a family that HAS a proxy (``proxy_port is not
    None``). A target family without ``proxy: true`` has no stable alias
    endpoint — writing it would silently re-introduce the node pin the flag
    exists to remove, so it is a hard plan error, not a warning.
    """
    errors: List[str] = []

    try:
        from . import recipe_cost as _rc
    except ImportError:
        import recipe_cost as _rc

    def _recipe_missing(recipe: str) -> bool:
        # Registry-aware: a recipe may be a filesystem path OR a registry name
        # (@reg/name). A bare is_file() check reported every registry recipe as
        # missing, which blocked any template sourcing from the community /
        # eugr / atlas / official registries.
        return not _rc.recipe_exists(recipe)

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

    # ── v4: declared endpoint targets must land on a family WITH a proxy ──
    # Only uniform_pool is checked: §5 scopes the requirement there, and the
    # non-uniform exemption (§1.3) deliberately allows a no-proxy target to
    # mean "leave it as tuned".
    ep = getattr(plan, "endpoints", None)
    if getattr(plan, "uniform_pool", False) and isinstance(ep, dict):
        by_name = {f.name: f for f in plan.families}
        targets: List[Tuple[str, Any]] = [
            (f"endpoints.{k}", ep.get(k)) for k in ("chat", "delegation")]
        profs = ep.get("profiles")
        if isinstance(profs, dict):
            targets += [(f"endpoints.profiles.{s}", t) for s, t in profs.items()]
        for key, target in targets:
            if not isinstance(target, str) or not target.startswith("family-"):
                continue          # orchestrator / absent / non-symbolic: not ours
            fam = by_name.get(target[len("family-"):])
            if fam is None:
                continue          # dangling name is the structural layer's call
            if fam.proxy_port is None:
                errors.append(
                    f"{key} -> '{target}': family '{fam.name}' has no proxy "
                    f"(proxy: true required) — under uniform_pool an endpoint "
                    f"must resolve to the family proxy, and this family has "
                    f"no stable alias endpoint")
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
    # The v4 declaration results, initialised so the applied-state write below
    # can always name them (an exception path never reaches that write, but the
    # names must be bound for the code to read cleanly).
    ep_decision: Optional[dict] = None
    ep: dict = {}
    px: dict = {}
    cp: dict = {}

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
        # Carry forward recorded serve commands; see _carry_serve_cmds for why
        # dropping them made every subsequent apply warn and report failure.
        carried = _carry_serve_cmds(serving)
        write_json(SERVING_JSON, serving, backup=True)
        result["steps"].append({"step": "serving.json", "status": "ok",
                                "units": len(serving["units"]),
                                "serve_cmds_carried": carried})

        # Step 2: Write models.json
        models = _build_models_json(plan)
        write_json(MODELS_JSON, models, backup=True)
        result["steps"].append({"step": "models.json", "status": "ok", "models": len(models["models"])})

        # Step 3: Update Hermes config.yaml. The v4 `endpoints:` decision is
        # computed ONCE here (spec §1.2 "one computation point") from the
        # resolved plan and threaded to every consumer on the apply path, so no
        # routing decision is computed outside the declared block. None = the
        # §1.3 exemption (no block, non-uniform) — every consumer then takes its
        # historical path byte-for-byte (§7).
        ep_decision = _endpoints_decision(plan, profiles_dir=PROFILES_DIR)
        _, config_changed = atomic_yaml_update(
            CONFIG_YAML, lambda d: _update_hermes_config(
                d, plan, _http_get=_http_get, decision=ep_decision))
        result["steps"].append({"step": "config.yaml", "status": "ok",
                                "changed": config_changed})

        # Step 3b: Write the declared proxy regions (§2.3) through sparkrun's own
        # write path, BEFORE the proxies start so a freshly started proxy reads
        # the declared policy. Never hand-crafts proxy.yaml / litellm_config.yaml,
        # and never restarts anything (§2.3 restart rule + addendum item 4).
        #
        # A FAILED WRITE is only fatal for a DECLARED policy: a template that
        # states balancer intent must not have that intent dropped in silence.
        # The uniform_pool DEFAULT is an operator-blessed preference the template
        # never mentioned, so when the write path is unreachable it is reported
        # and the apply still succeeds — an untouched old template must not
        # newly fail to apply because a preference could not be written.
        px = _apply_proxy_declaration(tpl, plan)
        # A step is a CLAIM about this apply. A template that declared no
        # `proxy:` block AND is not uniform_pool (so the §2.2 default does not
        # even apply) has nothing to claim, and reporting a no-op step would
        # change the step sequence of an untouched v3 template — which §7
        # forbids ("must not change a single byte"). The leg still RUNS (that
        # is cheap and it is what proves it is inert); only the report is gated.
        px_declared = isinstance(getattr(tpl, "proxy", None), dict) or bool(
            getattr(plan, "uniform_pool", False))
        proxy_step = {
            "step": "proxy-declaration",
            "status": ("warn" if px["error"] and px["source"] == "declared"
                       else "ok"),
            "balancing": px["policy"],
            "source": px["source"],
            "written": px["written"],
            "changed": px["changed"],
            "restart_required": px["restart_required"],
            "inert_recorded": px["inert_recorded"],
            "note": px["note"] if not px["error"] else px["error"],
        }
        if px_declared:
            result["steps"].append(proxy_step)

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
        #
        # Who owns the delegation consumer (§1.5 — this REPLACES the old
        # "routing block present" heuristic): when the template declares
        # `endpoints:`, that block decides per consumer. A DECLARED delegation is
        # written by Step 5c (which runs last, so `endpoints:` wins over
        # `routing:` per §1.4); an OMITTED one is left untouched by every step.
        # With no `endpoints:` block the rule is exactly what it was: a routing
        # block owns the consumer, otherwise this legacy re-aim does.
        delegation_entry = (ep_decision or {}).get("delegation")
        endpoints_owns_delegation = ep_decision is not None and (
            delegation_entry is None
            or delegation_entry.get("source") == "declared")
        wm = _update_worker_model_ids(
            plan, skip_delegation=(endpoints_owns_delegation
                                   or tpl.routing is not None),
            _http_get=_http_get)
        result["steps"].append({
            "step": "worker-model-ids",
            "status": "ok",
            "model_id": wm["model_id"],
            "config_changed": wm["config_changed"],
            "profiles_changed": wm["profiles_changed"],
            # Surfaced so the apply report states how many ORCH-role profiles
            # were re-aimed at the pool; without it the work was invisible.
            "orch_profiles_changed": wm.get("orch_profiles_changed", 0),
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

        # Step 5c: Apply the DECLARED endpoint consumers (§1.2 rows 5 + 7). Runs
        # AFTER routing so `endpoints:` wins for a consumer both blocks name
        # (§1.4 — validate already warned about the shadowing). Writes only what
        # the template DECLARED; default/legacy entries are no-ops here, which is
        # what keeps an untouched template byte-identical (§7).
        ep = _apply_endpoints(plan, ep_decision, _http_get=_http_get)
        if ep["changed"]:
            config_changed = True
        ep_step = {
            "step": "endpoints",
            "status": "ok",
            "keys_written": ep["keys_written"],
            "config_changed": ep["changed"],
            "notes": ep["notes"],
            # §1.5: the report shows which family each consumer was pointed at
            # and the probe status that gated each write.
            "resolved": ep["resolved"],
        }
        # Reported only when the template actually declared `endpoints:` — the
        # same no-phantom-claim rule as Step 3b (§7 byte-identity).
        if isinstance(getattr(tpl, "endpoints", None), dict):
            result["steps"].append(ep_step)

        # Step 5d: Write DECLARED kanban caps (§3). Explicit values only — the
        # §3.1 derivation is derive-and-REPORT (advisory; its input is INERT),
        # so a derived cap is printed, never written.
        cp = _apply_caps_declaration(tpl)
        caps_step = {
            "step": "caps",
            "status": "ok",
            "written": cp["written"],
            "changed": cp["changed"],
            "keys": cp["keys"],
            "declared": cp["declared"],
            "derived": cp["derived"],
            "note": cp["note"],
        }
        if isinstance(getattr(tpl, "caps", None), dict):
            result["steps"].append(caps_step)

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
        decl_record = _applied_declaration_record(tpl, plan, ep_decision,
                                                  ep, px, cp)
        try:
            write_json(APPLIED_STATE, {
                "template": tpl.name,
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "orchestrator_node": plan.orchestrator.node,
                "families": [f.name for f in plan.families],
                "units": len(serving["units"]),
                # Which routing RULESET is live, recorded at apply. The
                # verify.provider_routing guard reads this to decide whether
                # the "no direct fleet node in any hermes config" invariant
                # applies; a state written before this field existed reads as
                # False there (non-uniform = check skipped, documented).
                "uniform_pool": bool(getattr(plan, "uniform_pool", False)),
                # §7: "applied_template.json records the resolved declarations
                # (targets + knob values) alongside uniform_pool, so doctor's
                # 'applied template's declared X' always reads ONE file." Nested
                # under ONE key so a reader can tell "this apply declared
                # nothing" (key absent) from "it declared and resolved empty"
                # (key present) — flat keys would make those two states
                # indistinguishable. Absent entirely for a pre-v4 apply, which
                # keeps an old record byte-identical (§7).
                **({"declarations": decl_record} if decl_record else {}),
            }, backup=False)
        except Exception:
            pass

    return result


def _applied_declaration_record(tpl: Any, plan: Any, decision: Optional[dict],
                                endpoints_result: dict, proxy_result: dict,
                                caps_result: dict) -> dict:
    """The v4 declaration block for ``applied_template.json`` (§7).

    Doctor's contract is "the applied template's declared X" read from ONE file,
    so what apply RESOLVED is recorded next to what the template declared —
    including the probe status that gated each write, so a consumer that was
    refused is visible in the record instead of silently absent.

    Returns ``{}`` when the template declared nothing new: an old template's
    applied state must stay exactly the keys it has always had (§7 byte-
    identity — `uniform_pool` is recorded separately and unchanged).
    """
    declared_endpoints = isinstance(getattr(tpl, "endpoints", None), dict)
    declared_proxy = isinstance(getattr(tpl, "proxy", None), dict)
    declared_caps = isinstance(getattr(tpl, "caps", None), dict)
    if not (declared_endpoints or declared_proxy or declared_caps):
        # An untouched uniform_pool template still gains balancer/caps intent
        # from the DEFAULTS, and doctor must be able to tell the applied state
        # apart — but recording anything here would change the shape of an old
        # template's record, so the default-only case stays empty and the C3
        # doctor reads `uniform_pool` for that case.
        return {}

    record: dict = {}
    if declared_endpoints:
        record["endpoints"] = {
            "declared": tpl.endpoints,
            "mode": (decision or {}).get("mode"),
            "resolved": (endpoints_result or {}).get("resolved", {}),
        }
    if declared_proxy:
        record["proxy"] = {
            "declared": tpl.proxy,
            "balancing": (proxy_result or {}).get("policy"),
            "balancing_source": (proxy_result or {}).get("source"),
            "written": (proxy_result or {}).get("written", False),
            "restart_required": (proxy_result or {}).get("restart_required",
                                                         False),
            # §2.3 item 3 — recorded so the §4.3 hook (and doctor) can see the
            # intent without proxy.yaml carrying keys sparkrun would drop.
            "inert": (proxy_result or {}).get("inert_recorded", []),
        }
    if declared_caps:
        record["caps"] = {
            "declared": tpl.caps,
            "written_keys": (caps_result or {}).get("keys", []),
            "derived": (caps_result or {}).get("derived"),
        }
    return record


def applied_status() -> dict:
    """Report which template is currently applied (from APPLIED_STATE).

    Returns {"applied": <state>} or {"applied": None} if nothing recorded.
    """
    state = read_json(APPLIED_STATE)
    return {"applied": state or None,
            "note": "" if state else "No template applied yet (or applied before status tracking)."}


# ── Layer 1: structural validation (offline, no cluster state, no resolver) ──

# Recognised template schema versions. The engine understands v2 (topology-free,
# parsed unchanged), v3 (adds explicit nodes/allow_colocation/routing) and v4
# (adds the endpoints/proxy/caps declarations —
# docs/design/2026-10-15-template-declarations.md §1.6). Any other version is a
# structural error — a template is not trustworthy if we cannot decide what its
# keys mean.
RECOGNISED_VERSIONS = (2, 3, 4)

# Known keys per level. Anything else is a typo and a hard structural error
# (spec: "unknown keys rejected"). Kept in sync with template_intent schema.
_KNOWN_TOP_KEYS = {"name", "version", "description", "orchestrator", "families",
                   "routing", "uniform_pool", "endpoints", "proxy", "caps"}
_KNOWN_MODEL_KEYS = {"recipe", "tp", "pp", "nodes", "gpu_memory_utilization"}
_KNOWN_FAMILY_KEYS = {"name", "models", "workers", "proxy", "nodes",
                      "allow_colocation"}

# ── v4 declaration vocabulary (spec docs/design/2026-10-15-template-declarations.md)
#
# These tables are the single source of truth for what a declaration may say.
# §8 ("Knob enforcement table") is LAW: no knob may be reported, documented or
# summarised as working unless it appears in _ENFORCED_KNOBS, which is derived
# from the spec's own ENFORCED rows. Everything §8 marks INERT is accepted,
# recorded and labelled INERT — accepting a declaration is not promising it.

# §2.1 — the keys v1 recognises under `proxy.families.<name>`. The ORDER is the
# order the spec's own error text lists them in ("known: routing,
# max_parallel_requests, weight, timeout, num_retries"), so it is a contract,
# not a preference.
PROXY_KNOWN_KNOBS = ("routing", "max_parallel_requests", "weight", "timeout",
                     "num_retries")

# §8 — only `routing: least-in-flight` is ENFORCED end-to-end (balancer
# CustomLogger; 4-fake-backend 2/2/2/2 proof + slow-backend soak 224 vs 52), and
# `routing_strategy` is enforced only as verbatim emit. The four per-deployment
# knobs are INERT-PENDING-SPARKRUN (sparkrun crafts litellm_params itself and
# drops any other key — §4.3 is the open hook) and `weight` is INERT even after
# that hook under the balancer, whose pick() ignores it.
PRO_ENFORCED_KNOBS = ("routing", "routing_strategy")
PRO_INERT_KNOBS = ("max_parallel_requests", "timeout", "num_retries", "weight")

# §2.2 — the balancer is a LiteLLM CustomLogger overlay, NOT a
# `routing_strategy`. 1.82.6's Literal (types/router.py:62-68) admits exactly
# these four and nothing else, so writing the balancer's name there fails at
# proxy startup. `least-busy` is additionally broken on its own merits.
LITELLM_ROUTING_STRATEGIES = ("simple-shuffle", "least-busy",
                              "usage-based-routing", "latency-based-routing")
# The balancer's own name, in the spellings an operator might reach for.
BALANCER_NAME_SPELLINGS = ("least-in-flight", "least_in_flight",
                           "leastinflight", "in-flight", "in_flight")
# What `proxy.*.routing` may say: the balancer ON, or the base strategy with
# the balancer OFF (the shipped toggle is a global on/off, §2.2).
PROXY_ROUTING_POLICIES = ("least-in-flight", "simple-shuffle")

# §1.1 / §3 — block shapes.
ENDPOINT_MODES = ("proxy-alias", "explicit")
KNOWN_ENDPOINT_KEYS = ("mode", "chat", "delegation", "profiles")
KNOWN_PROXY_KEYS = ("families",)
KNOWN_CAPS_KEYS = ("max_in_progress", "max_in_progress_per_profile",
                   "expected_requests_per_card")
# §3 — migration default when neither declaration nor derivation speaks: today's
# static fill-if-absent values in hscc-bootstrap/enable_plugins.py, unchanged.
CAPS_MIGRATION_DEFAULTS = {"max_in_progress": 30, "max_in_progress_per_profile": 10}

# §1.1 — "A target is never an address." Host-shaped detection for the
# declaration text itself: an IPv4 literal, or a dotted hostname. `family-x` /
# `orchestrator` never match (no colon, no dot).
_HOST_SHAPED_RE = re.compile(
    r"(?:^|[^0-9])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])"      # ipv4, optional :port
    r"|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\.[A-Za-z]{2,}$"  # dotted hostname
)


def _target_is_address(target: str) -> bool:
    """True if an `endpoints:`/`routing:` target names an address rather than a
    symbolic unit — a colon (``host:port``) or a host-shaped string (§1.1)."""
    return ":" in target or bool(_HOST_SHAPED_RE.search(target))


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


# ── v4 declaration validation (spec §5) ─────────────────────────────────────
#
# Each helper takes the PARSED template plus the raw block it validates and
# appends to shared errors/warnings lists, so the whole structural layer keeps
# one voice and one pass. They are offline by construction: they read nothing
# but the template itself. Nothing here can change what apply writes — this is
# the validate layer, and the write path is C2.


def _validate_endpoints_block(tpl: Any, errors: List[str],
                              warnings: List[str]) -> None:
    """§1.1/§5 — the `endpoints:` block: symbolic-only targets, known keys.

    §1.3 is the exemption this must respect: an `endpoints:` block on a
    NON-uniform template is an optional override, so an explicit
    ``orchestrator: <ip>:<port>``-style target stays legal there (that is what
    the v1.7 role split means). Under `uniform_pool` the same target would
    re-introduce a node pin behind an alias, so it is refused (§5 row 1).
    """
    block = tpl.endpoints
    if block is None:
        return
    if not isinstance(block, dict):
        errors.append(f"endpoints: expected a mapping, got "
                      f"{type(block).__name__}")
        return

    for k in block:
        if k not in KNOWN_ENDPOINT_KEYS:
            errors.append(f"endpoints: unknown key '{k}' (known: "
                          f"{', '.join(KNOWN_ENDPOINT_KEYS)})")

    mode = block.get("mode")
    if mode is not None and mode not in ENDPOINT_MODES:
        errors.append(f"endpoints.mode: expected one of "
                      f"{', '.join(ENDPOINT_MODES)}, got {mode!r}")

    # `profiles:` is a selector -> target mapping (§1.1). A selector is a
    # role-profile glob ("default", "*-orch"), so it is NOT subject to the
    # family-name resolution check — its VALUE is.
    profiles = block.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        errors.append(f"endpoints.profiles: expected a mapping of "
                      f"selector -> target, got {type(profiles).__name__}")
        profiles = None

    # (dotted key, target) for every declared target.
    targets: List[Tuple[str, Any]] = [(f"endpoints.{k}", block.get(k))
                                      for k in ("chat", "delegation")]
    if isinstance(profiles, dict):
        for sel, tgt in profiles.items():
            targets.append((f"endpoints.profiles.{sel}", tgt))

    defined_units = {"orchestrator"} | {f"family-{f.name}" for f in tpl.families}
    for key, target in targets:
        if target is None:
            continue                      # chat/delegation omitted = untouched
        if not isinstance(target, str) or not target:
            errors.append(f"{key} -> {target!r}: expected a target name")
            continue
        # §1.1 — "A target is never an address." Under uniform_pool this is a
        # hard error (§5); elsewhere an address target is the legitimate
        # role-split pin §1.3 exempts, so it is not refused here.
        if tpl.uniform_pool and _target_is_address(target):
            errors.append(
                f"{key} -> '{target}': uniform_pool endpoints must name a "
                f"family (family-<name>) or 'orchestrator', never an address")
            continue
        if target in defined_units:
            continue
        if target.startswith("family-"):
            errors.append(f"{key} -> '{target}': no such family in this "
                          f"template")
        elif not _target_is_address(target):
            errors.append(f"{key} -> '{target}': must be 'orchestrator' or "
                          f"'family-<name>'")

    # §1.4 — `endpoints:` wins over `routing:` for a consumer both declare;
    # validate warns so the redundancy is visible instead of silent.
    for consumer in ("delegation",):
        if block.get(consumer) is not None and (tpl.routing or {}).get(consumer):
            warnings.append(
                f"endpoints.{consumer} shadows routing.{consumer}: "
                f"endpoints wins (both declared: "
                f"{block[consumer]!r} vs {tpl.routing[consumer]!r})")


def _validate_proxy_block(tpl: Any, errors: List[str],
                          warnings: List[str]) -> None:
    """§2.1/§2.2/§5 — the `proxy:` block: mapping shape, known knobs, and the
    balancer-is-not-a-strategy rule.

    §8 constrains what may be SAID about each knob, not whether it is accepted:
    the per-deployment knobs are recorded (parse keeps them verbatim for C2 to
    pass through) and reported INERT here. `routing_strategy` is refused
    outright because there is no spelling of it that works.
    """
    block = tpl.proxy
    if block is None:
        return
    if not isinstance(block, dict):
        errors.append(f"proxy: expected a mapping (generic pass-through dict), "
                      f"got {type(block).__name__}")
        return

    for k in block:
        if k not in KNOWN_PROXY_KEYS:
            errors.append(f"proxy: unknown key '{k}' (known: "
                          f"{', '.join(KNOWN_PROXY_KEYS)})")

    fams = block.get("families")
    if fams is None:
        return
    if not isinstance(fams, dict):
        errors.append("proxy.families: expected a mapping of family -> knobs, "
                      f"got {type(fams).__name__}")
        return

    declared_policy: Dict[str, str] = {}
    for fam_name, knobs in fams.items():
        label = f"proxy.families.{fam_name}"
        if not isinstance(knobs, dict):
            errors.append(f"{label}: expected a mapping of knobs, got "
                          f"{type(knobs).__name__}")
            continue
        declared_family = {f.name for f in tpl.families}
        if fam_name not in declared_family:
            errors.append(f"{label}: no such family in this template")
        for knob in knobs:
            if knob == "routing_strategy" or knob in BALANCER_NAME_SPELLINGS:
                # §5 rows 4-5: the balancer name in a LiteLLM-strategy slot.
                errors.append(_balancer_not_a_strategy_error(knob))
                continue
            if knob not in PROXY_KNOWN_KNOBS:
                errors.append(
                    f"family '{fam_name}' proxy knob: unknown key '{knob}' "
                    f"(known: {', '.join(PROXY_KNOWN_KNOBS)})")
                continue
            val = knobs[knob]
            if knob == "routing":
                if val not in PROXY_ROUTING_POLICIES:
                    errors.append(_routing_value_error(val))
                else:
                    declared_policy[str(fam_name)] = str(val)
                continue
            if knob in PRO_INERT_KNOBS:
                _reject_non_int(knob, val, label, errors)
                # §8: recording it is not promising it. Name the status so a
                # template author reading `validate` output cannot mistake
                # acceptance for enforcement.
                warnings.append(
                    f"{label}.{knob}: recorded, INERT — see the §8 knob "
                    f"enforcement table ('weight' is ignored by the balancer; "
                    f"the per-deployment knobs need sparkrun's §4.3 "
                    f"pass-through hook)")

    # §2.2 — the shipped toggle is GLOBAL. Two policies are not expressible
    # today and the spec refuses to invent a mechanism.
    if len(set(declared_policy.values())) > 1:
        disagree = ", ".join(f"'{f}': {p}"
                             for f, p in sorted(declared_policy.items()))
        errors.append(f"proxy: balancing is global today — families disagree "
                      f"({disagree}); declare one value")


def _balancer_not_a_strategy_error(knob: Any) -> str:
    """§5 row 4 — the balancer written where a LiteLLM strategy belongs."""
    return (f"proxy knob '{knob}': the balancer is not a LiteLLM routing "
            f"strategy — LiteLLM 1.82.6 admits only "
            f"{' / '.join(LITELLM_ROUTING_STRATEGIES)}, and least-busy is "
            f"broken there (its in-flight counter is never incremented on the "
            f"async path); use 'routing: least-in-flight'")


def _routing_value_error(val: Any) -> str:
    """§5 row 5 — `routing:` with a value that is not the shipped toggle."""
    text = str(val)
    if text in BALANCER_NAME_SPELLINGS:
        # The name is right but the slot is wrong somewhere else in the value.
        return _balancer_not_a_strategy_error(text)
    if text == "least-busy":
        # Rejected on its own merits, not just as a Literal member: 1.82.6's
        # in-flight counter is incremented only from a sync hook
        # Router.acompletion never calls, so every deployment reads 0 and the
        # tie goes to the first model_list entry.
        return ("proxy knob 'routing': 'least-busy' is broken on LiteLLM "
                "1.82.6 (its in-flight counter is never incremented on the "
                "async path, so every deployment reads 0 and the tie goes to "
                "the first model_list entry); use 'routing: least-in-flight'")
    return (f"proxy knob 'routing': expected "
            f"{' or '.join(repr(p) for p in PROXY_ROUTING_POLICIES)}, got "
            f"{text!r}")


def _reject_non_int(knob: str, val: Any, label: str,
                    errors: List[str]) -> None:
    """A knob whose value must be an int (bool is NOT an int here: `weight:
    true` is a typo for a weight, not a 1)."""
    if isinstance(val, bool) or not isinstance(val, int):
        errors.append(f"{label}.{knob}: expected an integer, got {val!r}")


def _validate_caps_block(tpl: Any, errors: List[str],
                         warnings: List[str]) -> None:
    """§3/§5 — the `caps:` block: int-only values, positive divisor.

    §3.1's derivation is ADVISORY (its inputs are INERT per §8), so this layer
    computes it, warns about a shadowed derivation, and never treats it as a
    ceiling. Derivation and shadowing are reported; explicit `caps.*` always
    wins.
    """
    block = tpl.caps
    if block is None:
        return
    if not isinstance(block, dict):
        errors.append(f"caps: expected a mapping, got {type(block).__name__}")
        return

    for k in block:
        if k not in KNOWN_CAPS_KEYS:
            errors.append(f"caps: unknown key '{k}' (known: "
                          f"{', '.join(KNOWN_CAPS_KEYS)})")
            continue
        val = block[k]
        if k == "expected_requests_per_card":
            # A float divisor is legitimate (a card may hold ~1.5 requests in
            # another workload shape); int-valued knobs are not.
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                errors.append(f"caps.{k}: expected a number, got {val!r}")
            elif val <= 0:
                errors.append(f"caps.{k}: must be > 0, got {val!r}")
            continue
        if isinstance(val, bool) or not isinstance(val, int):
            errors.append(f"caps.{k}: expected an integer, got {val!r}")

    # §3.1 — derive-and-report. Only when the cap itself was not declared and
    # some family declared a per-backend parallelism.
    derived = derived_max_in_progress(tpl)
    if derived is None:
        return
    declared = block.get("max_in_progress") if isinstance(block, dict) else None
    if declared is not None and isinstance(declared, int) and not isinstance(declared, bool):
        warnings.append(
            f"caps.max_in_progress: declared {declared} wins over derived "
            f"{derived[0]} (derived {derived[1]}); the derivation is a "
            f"starting point, and it is ADVISORY while "
            f"max_parallel_requests is INERT (§8)")
    else:
        warnings.append(
            f"caps.max_in_progress not declared: derived {derived[0]} "
            f"({derived[1]}) — reported, not written (advisory while "
            f"max_parallel_requests is INERT, §8)")


def derived_max_in_progress(tpl: Any) -> Optional[Tuple[int, str]]:
    """§3.1 — ``floor(sum(N over declared backends) / expected_requests_per_card)``.

    Returns ``(value, human_basis)`` or None when no family declares
    `max_parallel_requests` (then there is nothing to derive from). Pure and
    read-only; the value is ADVISORY — §8 marks its only input INERT, so this
    is a plan value, not a physical ceiling.
    """
    fams = (tpl.proxy or {}).get("families") if isinstance(tpl.proxy, dict) else None
    if not isinstance(fams, dict):
        return None
    total = 0
    backends = 0
    for knobs in fams.values():
        if not isinstance(knobs, dict):
            continue
        n = knobs.get("max_parallel_requests")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            total += n
            backends += 1
    if backends == 0:
        return None
    divisor = 1
    caps = tpl.caps if isinstance(tpl.caps, dict) else {}
    raw_div = caps.get("expected_requests_per_card")
    if isinstance(raw_div, (int, float)) and not isinstance(raw_div, bool) and raw_div > 0:
        divisor = raw_div
    value = int(math.floor(total / divisor))
    # The basis names the INPUTS, never restates the result — the caller writes
    # "derived <value> from <basis>". The spec's illustrative example line is
    # prose ("derived 6 from 4 backends x 2"); this is the same information in a
    # form a test can pin and a human can re-derive by hand.
    basis = f"{backends} declared backends"
    if divisor != 1:
        basis += f" / {raw_div} requests per card"
    return value, basis


def declaration_summary(tpl: Any) -> str:
    """§7 — the one-line `validate` summary of the declared blocks.

    The spec's example line is the contract::

        proxy: 2 families, balancing=least-in-flight (global); endpoints: chat
        family-coding, delegation family-coding, profiles default->family-coding;
        caps: max_in_progress=6 (declared; derived 6 from 4 backends x 2)

    Wording notes: ``->`` and ``x`` instead of the arrows/multiplication sign
    are deliberate, so the line is stable under a non-UTF8 locale and through
    the ANSI-stripped render path. The INERT/ADVISORY labels (§8) belong to the
    warnings, never here: a summary must not read as a promise.
    """
    parts: List[str] = []

    fams = (tpl.proxy or {}).get("families") if isinstance(tpl.proxy, dict) else None
    if isinstance(fams, dict) and fams:
        policies = {str(v.get("routing")) for v in fams.values()
                    if isinstance(v, dict) and v.get("routing")}
        pol = ", ".join(sorted(policies)) or "default"
        parts.append(f"proxy: {len(fams)} families, balancing={pol} (global)")

    ep = tpl.endpoints if isinstance(tpl.endpoints, dict) else None
    if ep:
        seg = [f"chat {ep['chat']}" if ep.get("chat") else None,
               f"delegation {ep['delegation']}" if ep.get("delegation") else None]
        profs = ep.get("profiles")
        if isinstance(profs, dict) and profs:
            seg.append("profiles " + ", ".join(
                f"{sel}->{tgt}" for sel, tgt in profs.items()))
        parts.append("endpoints: " + ", ".join(s for s in seg if s))

    caps = tpl.caps if isinstance(tpl.caps, dict) else None
    derived = derived_max_in_progress(tpl)
    if caps:
        declared = caps.get("max_in_progress")
        seg = []
        if declared is not None:
            how = "declared"
            if derived:
                how += f"; derived {derived[0]} from {derived[1]}"
            seg.append(f"max_in_progress={declared} ({how})")
        elif derived:
            seg.append(f"max_in_progress=derived {derived[0]} ({derived[1]})")
        per_profile = caps.get("max_in_progress_per_profile")
        if per_profile is not None:
            seg.append(f"max_in_progress_per_profile={per_profile}")
        if not seg:
            seg.append(f"declared {sorted(caps)}")
        parts.append("caps: " + "; ".join(seg))
    elif derived:
        v, basis = derived
        parts.append(f"caps: max_in_progress=derived {v} ({basis}); "
                     f"advisory — INERT input (§8)")

    return "; ".join(parts)


def _structural_validate(raw: dict, tpl: Any,
                         cluster_ips: Optional[List[str]] = None,
                         recipe_exists: Optional[Any] = None) -> Tuple[List[str], List[str]]:
    """Layer 1 — structural validation. Offline: uses only the static
    cluster.json node list, never discovery/resolver/live state. (Recipe
    existence for a ``@reg/name`` token is resolved by ``sparkrun show
    --no-vram``, which reads sparkrun's LOCAL registry cache — no live cluster
    state and no network.) Returns
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

    # ── uniform_pool means uniform: one recipe everywhere ─────────────────
    # The flag makes every unit advertise BOTH aliases, so a request for
    # `orchestrator-model` may land on any unit. If the units were different
    # recipes, the same alias would silently resolve to different MODELS
    # depending on which backend the proxy picked — a correctness bug, not a
    # performance one. Refuse it here rather than serve a lottery.
    if tpl.uniform_pool:
        recipes = {tpl.orchestrator.recipe}
        for fam in tpl.families:
            for m in fam.models:
                recipes.add(m.recipe)
        if len(recipes) > 1:
            errors.append(
                "uniform_pool: every unit must use the SAME recipe (found "
                + ", ".join(sorted(r.split("/")[-1] for r in recipes))
                + ") — otherwise one alias resolves to different models "
                  "depending on which backend the proxy picks")
        if tpl.version < 3:
            errors.append("uniform_pool requires version: 3")

    # ── recipes resolve (filesystem path OR @reg/name) ────────────────────
    try:
        from . import recipe_cost as _rc_mod
    except ImportError:
        import recipe_cost as _rc_mod

    def _recipe_ok(recipe: str, label: str) -> None:
        # Default is registry-aware: a recipe may be a filesystem path OR a
        # registry name (@reg/name). The previous is_file() default reported
        # every registry recipe as missing. Injected `recipe_exists` still wins,
        # so tests keep full control.
        exists = (recipe_exists(recipe) if recipe_exists is not None
                  else _rc_mod.recipe_exists(recipe))
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
            # §5 row 1 extends the address rule to `routing` too: under
            # uniform_pool a host or host:port target here is the same node pin
            # the endpoints block forbids. Non-uniform routing keeps today's
            # behaviour exactly (v3 templates are unaffected).
            if tpl.uniform_pool and _target_is_address(str(target)):
                errors.append(
                    f"routing.{consumer} -> '{target}': uniform_pool endpoints "
                    f"must name a family (family-<name>) or 'orchestrator', "
                    f"never an address")
                continue
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

    # ── v4 declarations: endpoints / proxy / caps (spec §5) ───────────────
    _validate_endpoints_block(tpl, errors, warnings)
    _validate_proxy_block(tpl, errors, warnings)
    _validate_caps_block(tpl, errors, warnings)

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


def _carry_serve_cmds(serving: dict) -> int:
    """Carry recorded ``serve_cmd`` values across a serving.json rewrite.

    ``to_serving_json`` rebuilds units from the plan and carries no
    ``serve_cmd``, so Step 1 overwrote every recorded command. Provision only
    re-stamps units it ACTUALLY (re)launched, and under ``--ensure`` an
    already-running unit is skipped — so after a single no-op apply every
    record was gone, drift could never be checked again, and every later apply
    reported "no previously recorded serve command", returned success=False,
    and therefore never updated applied_template.json. That stale record is
    what let auto-heal act on the PREVIOUS template.

    Preserving by unit id is the conservative choice: if a unit's flags really
    did change, the carried value differs from the freshly rendered one and the
    drift check reports real drift — which is its job — instead of silently
    degrading to "not checked".

    Returns how many records were carried.
    """
    try:
        prev = read_json(SERVING_JSON) or {}
    except Exception:
        return 0
    recorded = {u.get("id"): u.get("serve_cmd")
                for u in (prev.get("units") or [])
                if isinstance(u, dict) and u.get("id") and u.get("serve_cmd")}
    if not recorded:
        return 0
    carried = 0
    for u in (serving.get("units") or []):
        if not isinstance(u, dict) or u.get("serve_cmd"):
            continue
        sc = recorded.get(u.get("id"))
        if sc:
            u["serve_cmd"] = sc
            carried += 1
    return carried


# ── v4 `endpoints:` — the ONE computation point (spec §1.2/§1.3/§1.5) ───────
#
# §1.2's invariant is that no routing decision is computed outside the declared
# block: "chat_url and the probe-resolved alias are decided ONCE from endpoints:
# at the top of _update_hermes_config; both consumers read that single value".
# Everything below exists to make that true, and it computes DECISIONS (target +
# URL + model candidate) only — the probe-before-write gate stays where it has
# always been, at the write.
#
# §1.3 is the exemption that keeps old templates byte-identical (§7): a template
# that is NOT uniform_pool and declares NO `endpoints:` gets ``None`` from
# ``_endpoints_decision``, and every consumer then takes its historical path.
# Under uniform_pool the ABSENT block resolves to the §1.3 default (every
# consumer → the family that owns the worker proxy), which is behaviour-
# preserving: it is precisely what t_ac795424 made apply compute.

# The two §1.2 consumers `endpoints:` addresses directly (plus `profiles:`).
ENDPOINT_CONSUMERS = ("chat", "delegation")


def _profile_name_from_path(pfile: Any) -> str:
    """The role-profile name for ``<profiles>/<name>/config.yaml``."""
    return Path(pfile).parent.name


def _proxy_front_for_target(target: str, plan: Any) -> Optional[Tuple[str, str]]:
    """``(base_url, model_candidate)`` for the PROXY of a symbolic target.

    ``proxy-alias`` doctrine (§1.2): a target resolves to its family's proxy URL
    and the alias that proxy answers to — never a node address. ``orchestrator``
    under uniform_pool is the POOL, so it resolves to the same proxy front the
    chat consumer uses (every pool unit advertises both aliases), which is what
    §1.3's "all consumers → the worker-proxy-owning family" means.
    """
    if target == "orchestrator":
        front = _owned_worker_family(plan) or next(
            (f for f in plan.families if f.proxy_port is not None and f.units), None)
        if front is None:
            return None
        return (f"http://localhost:{front.proxy_port}/v1",
                _unit_alias(plan.orchestrator, plan))
    if target.startswith("family-"):
        fam = next((f for f in plan.families
                    if f.name == target[len("family-"):]), None)
        if fam is None or fam.proxy_port is None or not fam.units:
            return None
        return (f"http://localhost:{fam.proxy_port}/v1",
                _family_provider_alias(fam, plan))
    return None


def _endpoint_resolution(target: str, plan: Any, *, proxy_alias: bool) -> dict:
    """Resolve one declared target to what apply would write for it.

    ``proxy_alias`` mode routes through the family proxy only (§1.2). ``explicit``
    mode (a non-uniform template that DID declare the block) keeps the historical
    target semantics of `routing:` — an address/orchestrator target is legitimate
    there (§1.3: "omitting an endpoint means leave it as tuned, not route it") —
    by reusing ``_routing_target_endpoint``, the same resolver `routing:` uses, so
    the two blocks can never disagree about what a target means.
    """
    if proxy_alias:
        resolved = _proxy_front_for_target(target, plan)
        if resolved is None:
            return {"target": target, "url": None, "candidate": None,
                    "reason": "no proxy for target"}
        url, candidate = resolved
        return {"target": target, "url": url, "candidate": candidate,
                "reason": None}
    try:
        url, candidate = _routing_target_endpoint(target, plan)
    except _ti().TemplateIntentError as exc:
        return {"target": target, "url": None, "candidate": None,
                "reason": str(exc)}
    return {"target": target, "url": url, "candidate": candidate, "reason": None}


def _endpoints_decision(plan: Any, *,
                        profiles_dir: Optional[Path] = None) -> Optional[dict]:
    """The declared routing decisions for this plan, or None = legacy behavior.

    ``profiles_dir`` scopes WHICH profiles the declaration can select — apply
    passes the directory it is about to write so a selector is matched against
    the profiles that actually exist (see ``_declared_profile_names``); preview
    omits it and falls back to the role vocabulary, which costs it only the
    disclosure of profiles it cannot see.

    Reads the declaration off the RESOLVED plan (C1 put ``endpoints`` there for
    exactly this reason: the writer must not re-read the template file from
    disk). Returns ``None`` ONLY for the §1.3 exemption (no `endpoints:` block on
    a non-uniform template) — that is the byte-identity door §7 pins, so nothing
    may widen it. Otherwise a dict::

        {"mode": "proxy-alias" | "explicit",
         "chat":       {"source": "declared"|"default", ...resolution},
         "delegation": ditto, or None when OMITTED (leave untouched),
         "profiles":   {profile_name: ditto},
         "untouched":  [consumer whose declaration is omitted]}

    ``source`` is what the apply report and `applied_template.json` show, so
    "declared" vs "defaulted" is visible instead of folklore. A ``default`` entry
    is deliberately pinned to what apply computed BEFORE this feature existed
    (including the orchestrator-alias model candidate for ``chat``), so an
    untouched uniform_pool template renders exactly as it does today.
    """
    block = getattr(plan, "endpoints", None)
    block = block if isinstance(block, dict) else None
    uniform = bool(getattr(plan, "uniform_pool", False))
    if block is None and not uniform:
        return None                              # §1.3 exemption — legacy path

    proxy_alias = (block or {}).get("mode") != "explicit"
    # §1.3 default target under uniform_pool: the family that owns the proxy.
    default_fam = _owned_worker_family(plan) if uniform else None
    default_target = (f"family-{default_fam.name}" if default_fam is not None
                      else "orchestrator")

    decision: dict = {"mode": "proxy-alias" if proxy_alias else "explicit",
                      "chat": None, "delegation": None, "profiles": {},
                      "untouched": []}

    for consumer in ENDPOINT_CONSUMERS:
        declared = block.get(consumer) if block else None
        if declared is None:
            if not uniform:
                if consumer == "delegation":
                    # §1.5: with a declared block, an OMITTED delegation means
                    # untouched — apply's legacy re-aim must not write it.
                    decision["untouched"].append(consumer)
                    continue
                # chat has no "untouched" state: the model block is always
                # written by the writer below (it always has been). Recording it
                # as legacy keeps the disclosure honest instead of implying
                # apply left the model block alone.
                decision["chat"] = {"source": "legacy", "target": None,
                                    "url": None, "candidate": None,
                                    "reason": None}
                continue
            source, target = "default", default_target
        else:
            source, target = "declared", declared
        entry = dict(_endpoint_resolution(target, plan,
                                          proxy_alias=proxy_alias), source=source)
        if source == "default" and consumer == "chat" and entry["url"]:
            # The pre-declaration writer probed the ORCHESTRATOR alias at the
            # pool proxy (every pool unit advertises both aliases). Keep that
            # candidate so an untouched uniform_pool template writes the same
            # model id it writes today.
            entry["candidate"] = _unit_alias(plan.orchestrator, plan)
        decision[consumer] = entry

    profiles = (block or {}).get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    for name in _declared_profile_names(plan, profiles_dir=profiles_dir):
        selector, target = _match_profile_selector(profiles, name)
        if target is None:
            # Nothing in the declaration names this profile: the historical
            # intent-based heuristics stay in charge (§1.3 — "omitting an
            # endpoint means leave it as tuned, not route it"; under
            # uniform_pool that is the worker/orch role re-aim t_ac795424
            # established, and reproducing it exactly is what keeps an
            # untouched uniform_pool template byte-identical).
            decision["profiles"][name] = {"source": "legacy", "target": None,
                                          "url": None, "candidate": None,
                                          "reason": None}
            continue
        entry = dict(_endpoint_resolution(target, plan,
                                          proxy_alias=proxy_alias),
                     source="declared", selector=selector)
        decision["profiles"][name] = entry
    return decision


def _declared_profile_names(plan: Any,
                            profiles_dir: Optional[Path] = None) -> List[str]:
    """Profile names the declaration may select: every directory under
    ``profiles/``, else (no readable dir) the unit roles.

    §1.1 makes ``endpoints.profiles`` keys PROFILE-NAME selectors — "a selector
    matching the directory name under ``profiles/``" — so the candidates must be
    the real directory names. Enumerating ROLES instead was a real bug: the
    spec's own ``"*-orch"`` example matches no role (roles are ``worker`` /
    ``orchestrator``; the ``-orch`` suffix is a directory convention), so an
    orchestrator-role profile declared by the spec's own example silently got
    the legacy path — the expensive failure mode, since a declaration that
    matches nothing writes nothing and reports nothing.

    The role vocabulary is the fallback for callers with no filesystem view
    (preview on a plan built offline, unit tests), which keeps every existing
    caller working; the WRITE leg always passes the directory it is about to
    iterate, so what apply routes is what apply can actually see.
    """
    names: List[str] = []
    # NO implicit PROFILES_DIR here: a decision computed without a caller-
    # supplied directory must not read the operator's real ~/.hscc/profiles (a
    # preview/test would then depend on whatever is on this host). Callers that
    # intend the live directory pass it — apply and preview both do.
    if profiles_dir is not None:
        try:
            pd = Path(profiles_dir)
            if pd.is_dir():
                # Trust the directory absolutely, INCLUDING when it is empty:
                # if the write leg will iterate this dir and find nothing, a
                # role-named candidate here would make PREVIEW disclose a route
                # apply will not write (§1.5 — preview must not drift).
                return sorted({c.parent.name for c in pd.glob("*/config.yaml")})
        except (OSError, TypeError):
            return []
        return []
    # No filesystem view (a plan built offline, or a unit test): the role
    # vocabulary plus `default`, which is how this code named the hermes
    # default profile before directory enumeration. Whatever the vocabulary,
    # an entry the write leg cannot match falls through to the legacy path, so
    # the cost of a mismatch is a missing disclosure, never a wrong write.
    names = ["default"]
    for u in getattr(plan, "all_units", []) or []:
        role = getattr(u, "role", None)
        if role and role not in names:
            names.append(role)
    return names


def _match_profile_selector(profiles: Dict[str, Any],
                            name: str) -> Tuple[Optional[str], Optional[str]]:
    """Find the selector governing role-profile ``name``.

    An exact selector wins; otherwise the first glob that matches (fnmatch, the
    same vocabulary the schema documents: ``"*-orch"``). ``default`` is the
    fallback for any profile whose role is not listed (§1.1) and never matches a
    profile literally named ``default`` by glob accident.
    """
    import fnmatch
    if name in profiles:
        return name, profiles[name]
    for sel, target in profiles.items():
        if sel == "default" or not isinstance(sel, str):
            continue
        if fnmatch.fnmatchcase(name, sel):
            return sel, target
    default = profiles.get("default")
    # Report the selector that GOVERNED, not None: the apply report and the
    # applied record show why a profile got the target it got, and "the
    # `default` selector did" is the answer.
    return ("default" if default is not None else None), default


def _update_hermes_config(config: dict, plan: Any, *, _http_get=None,
                          decision: Optional[dict] = None) -> dict:
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
    network probe.

    Under ``uniform_pool`` (with a family proxy owning the worker port) NOTHING
    this function writes names a fleet node: the top-level ``model`` block and
    the ``custom`` provider both point at the family proxy (``chat_url``) with
    the probe-resolved alias as their model id, and every ``family-*`` provider
    carries its units' alias as ``model.default`` — a model id, never a
    host:port. The pool's proxy is hermes' only chat route; a direct node URL
    here bypasses routing and dies with the node. Non-uniform plans keep the
    historical direct-node ``custom`` entry byte-for-byte.

    ``decision`` (from ``_endpoints_decision``) is the v4 single computation
    point: when present, ``model.base_url`` / ``custom.base_url`` / the probe
    candidate come from the DECLARED chat target and nothing computes a chat
    route here. ``None`` is the §1.3 exemption and takes the historical path
    below verbatim — that is the byte-identity §7 pins."""
    o = plan.orchestrator
    orch_url = f"http://{o.node}:{o.port}/v1"

    # ── The chat decision (spec §1.2: computed ONCE, read by both consumers) ──
    # Under a uniform_pool template EVERY unit advertises the orchestrator
    # alias, so pinning chat to the one nominal orchestrator node wastes the
    # pool and leaves chat with no redundancy: that node dying takes chat with
    # it even though N-1 identical units are healthy. Point it at the family
    # proxy instead, which load-balances the same alias across all of them.
    # Non-uniform templates keep the pinned orchestrator endpoint exactly as
    # before — the role split is real there.
    chat_candidate = _unit_alias(o, plan)
    # §7/§1.3 byte-identity rule: the decision OVERRIDES a consumer only when
    # the template DECLARED it (source "declared"). A source-"default" entry
    # exists to REPORT what apply computes by default (what §1.3 calls
    # "naming what apply already did") — the computation itself stays on the
    # historical path, so an old template cannot render a different byte even
    # in a degenerate corner (e.g. uniform_pool with no proxy-owning family).
    chat = (decision or {}).get("chat") or {}
    chat_declared = decision is not None and chat.get("source") == "declared"
    if not chat_declared:
        chat_url = orch_url
        _pool_fam = None
        if getattr(plan, "uniform_pool", False):
            _pool_fam = _owned_worker_family(plan)
            if _pool_fam is not None:
                chat_url = f"http://localhost:{_pool_fam.proxy_port}/v1"
        chat_refused = None
        alias_custom = _pool_fam is not None
    else:
        chat_url = chat.get("url")
        chat_refused = chat.get("reason") if chat_url is None else None
        if chat.get("candidate"):
            chat_candidate = chat["candidate"]
        # proxy-alias mode is what makes the custom entry alias-valued;
        # `explicit` mode means the operator accepted a concrete endpoint.
        alias_custom = (decision or {}).get("mode") == "proxy-alias"

    # ── Top-level model block ────────────────────────────────────────
    model_cfg = config.setdefault("model", {})
    # A chat target that cannot resolve to a proxy front is REFUSED (§1.2
    # probe-before-write doctrine, one level up): write nothing rather than
    # re-introduce the node pin the declaration exists to remove.
    if chat_refused is None:
        model_cfg["base_url"] = chat_url
    # Preserve existing provider; default to "custom" if absent
    if "provider" not in model_cfg:
        model_cfg["provider"] = "custom"

    # PROBE-BEFORE-WRITE for model.default (same guard as the routing and worker
    # paths). Candidate = the declared chat target's stable alias; write the
    # alias when the endpoint advertises it, else the single concrete id it
    # serves, else leave model.default untouched — never write an id the
    # endpoint cannot resolve.
    if chat_refused is None:
        status, served = _probe_served_models(chat_url, _http_get=_http_get)
    else:
        status, served = "refused", []
    resolved = _resolve_worker_model_id(
        chat_candidate, served if status == "ok" else [])
    if resolved is not None:
        model_cfg["default"] = resolved

    # ── Providers list ───────────────────────────────────────────────
    existing = config.get("providers")
    by_name: dict = {}
    if isinstance(existing, list):
        for p in existing:
            if isinstance(p, dict) and p.get("name"):
                by_name[p["name"]] = p

    # `custom` provider. NON-uniform plans keep the historical direct-node entry
    # byte-for-byte — direct is legitimate when there is no proxy in front of
    # the orchestrator. Under ``uniform_pool`` the pool's front (the family
    # proxy) is the ONLY endpoint hermes may talk to: base_url is ``chat_url``
    # — the very URL the top-level model block above uses, never a second
    # crafted one — and model.default reuses the SAME probe-before-write
    # result as that block: the orchestrator alias when the proxy advertises
    # it, else the single concrete id it serves, else NOTHING new (carry the
    # existing entry's default rather than invent an id the endpoint does not
    # serve). Writing the node URL here re-infected a config the model block
    # had just been corrected (operator report 2026-10-15).
    #
    # With a v4 decision the SAME single value rule holds for whatever target
    # `endpoints.chat` declared (§1.2 row 2: "literally the same value, not a
    # second crafted URL") — only the alias-vs-direct SHAPE of the legacy
    # fallback changes; the probe-before-write result does not.
    if chat_refused is not None:
        pass  # declared chat target has no endpoint — leave the entry untouched
    elif decision is not None or alias_custom:
        custom: dict = {"name": "custom", "base_url": chat_url}
        default = resolved
        if default is None and isinstance(by_name.get("custom"), dict):
            prev = (by_name["custom"].get("model") or {}).get("default")
            # Carry the existing id on a failed probe (leave-untouched
            # doctrine) — EXCEPT a host:port-shaped one: that is the very
            # value class this fix removes, and it can never resolve as a
            # model id at the proxy.
            if isinstance(prev, str) and prev and (
                    "://" in prev or (":" in prev and "/" not in prev)):
                prev = None
            default = prev
        if default is not None:
            custom["model"] = {"default": default}
        by_name["custom"] = custom
    else:
        by_name["custom"] = {
            "name": "custom",
            "model": {"default": f"{o.node}:{o.port}"},
            "base_url": f"http://{o.node}:{o.port}/v1",
        }
    for fam in plan.families:
        if fam.proxy_port is None:
            continue
        name = f"family-{fam.name}"
        # model.default is a MODEL ID, not an endpoint. The old value
        # ``localhost:{port}`` was a host:port no endpoint can resolve as a
        # model; the family proxy load-balances its units and every unit
        # advertises its role alias, so the family alias (``_unit_alias`` —
        # ``worker-model`` for any non-orchestrator unit) is what the proxy
        # answers to.
        by_name[name] = {
            "name": name,
            "model": {"default": _family_provider_alias(fam, plan)},
            "base_url": f"http://localhost:{fam.proxy_port}/v1",
        }

    # Drop `family-*` providers this plan does NOT define. by_name is seeded
    # from the existing list and only ever added to, so switching templates
    # left the previous template's families behind forever (observed: a stale
    # `family-reasoning` surviving the move to a `family-coding` template).
    # Scoped to the `family-` prefix on purpose: those entries are ours to own,
    # while `custom` and any operator-defined provider are not.
    wanted = {f"family-{fam.name}" for fam in plan.families
              if fam.proxy_port is not None}
    for stale in [n for n in by_name
                  if n.startswith("family-") and n not in wanted]:
        by_name.pop(stale)

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


# ── BEGIN mirrored block: lan_curl_fallback (t_391b084e) ──────────────────
# macOS binds Privacy > Local Network to the *signing identity* of the
# executable, so an adhoc-signed interpreter — a Homebrew keg's Python.app, a
# python.org Framework build — is denied on EVERY private-range connect, no
# matter how often the Python entries are re-toggled. urllib then reports
# `OSError(65, 'No route to host')` (EHOSTUNREACH) for a host that is up and
# answering, while Apple-signed /usr/bin/curl — which keeps its own TCC grant
# — reaches the same URL fine. That is what made `hscc cluster monitor --live`
# render every backend `down` on a healthy fleet (host stats survived only
# because they ride ssh). Do NOT try to fix this with TCC/system changes: it is
# an OS identity rule, not our bug. The workaround is to retry exactly that one
# failure through curl.
#
# THREE PLUGINS CARRY THIS BLOCK VERBATIM (hscc_daemon/cluster_live.py,
# hscc-bootstrap/doctor.py, hscc-cluster/cluster_template.py). It is NOT a
# cross-plugin import on purpose: each plugin deploys standalone into
# ~/.hermes/plugins and the hyphenated dirs are not importable package names,
# so none of them can import another. The block is pinned byte-identical by
# hscc-cluster/tests/test_lan_curl_fallback.py::test_mirrored_block_is_identical
# — edit all three copies in the same commit, or that test fails.
#
# Deliberately narrow. The fallback fires ONLY when urllib reports errno 65
# SPECIFICALLY on a PRIVATE host:
#   * Linux EHOSTUNREACH is 113 and means there genuinely is no route, so it
#     must NOT be retried by anything — a "helpful" fallback there would hide a
#     dead node behind a second probe.
#   * A public host is not the Local Network story at all (TCC gates only local
#     traffic), so falling back there would paper over a real firewall or DNS
#     problem.
#   * Loopback never falls back: TCC does not gate localhost, so errno 65
#     there is not this bug.
# HTTPError, timeouts and every other errno propagate UNCHANGED, so callers
# keep their existing 'probe error' vs 'unreachable' distinction.
import ipaddress
import queue
import urllib.error
import urllib.parse

LAN_CURL_FALLBACK_NOTICE = "LAN via curl fallback (Local Network permission)"

_CURL_BIN = "/usr/bin/curl"
_CURL_UNUSABLE_HINT = (" — /usr/bin/curl fallback unavailable: restore it, or grant "
                       "macOS Local Network to this interpreter")

# One-shot ticket: filled at import, consumed by the first successful fallback.
# Consume-once is the whole point — `cluster monitor --live` re-renders every
# tick and the daemon loop ticks forever, so the notice must not repeat. A
# maxsize-1 Queue rather than a bool because the monitor scrapes backends from
# a thread pool, where a read-then-write flag would let two workers announce.
_CURL_FALLBACK_TICKET: "queue.Queue" = queue.Queue(maxsize=1)
_CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)

# 100.64.0.0/10 is CGNAT — where Tailscale hands out addresses — and Python
# does NOT classify it private, so it is named explicitly. fe80::/10 and
# fc00::/7 cover IPv6 link-local and ULA.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_ULA_NET = ipaddress.ip_network("fc00::/7")


def _lan_private_host(host) -> bool:
    """True for a private/LAN host the Local Network gate can deny.

    RFC1918, 100.64.0.0/10 (tailnet CGNAT), 169.254.0.0/16 + fe80::/10
    link-local, fc00::/7 ULA, and ``.local`` mDNS names. Loopback — numeric or
    ``localhost`` — is False: TCC does not gate it, so errno 65 there is not
    this bug. A name that is not an IP is False unless it is ``.local``, so a
    public hostname can never reach the fallback.
    """
    h = (host or "").strip().rstrip(".").lower()
    if not h or h == "localhost":
        return False
    if h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    # Family guard: `IPv4Address in <IPv6Network>` raises TypeError, so a
    # membership test must only run against the network of the same family.
    if ip.version == 4:
        return bool(ip.is_private or ip.is_link_local or ip in _CGNAT_NET)
    return bool(ip.is_private or ip.is_link_local or ip in _ULA_NET)


def _lan_errno(exc):
    """The errno behind a urllib failure, or None when it carries none.

    ``URLError.reason`` is the OSError raised by the connect. ``HTTPError`` is
    a URLError subclass whose reason is a *status string*, so it lands here as
    None — a status is never a connectivity denial, and never a fallback.
    """
    return getattr(getattr(exc, "reason", None), "errno", None)


def _lan_curl_notice_once() -> None:
    """Print the fallback notice the first time only, then stay quiet.

    stderr, not stdout: the two probe mirrors print structured output on stdout
    (doctor's checklist, template's ``--json``) and must not be polluted, and
    the daemon log picks stderr up — one line, then silence.
    """
    import sys
    try:
        _CURL_FALLBACK_TICKET.get_nowait()
    except queue.Empty:
        return
    print(LAN_CURL_FALLBACK_NOTICE, file=sys.stderr, flush=True)


def _lan_curl_reset_notice() -> None:
    """Refill the one-shot notice ticket.

    Test seam only — no runtime path calls it. A test that asserts 'the notice
    appears exactly once across N fallbacks' has to start from a known state,
    and the suite raises far more than one fallback per process, so without
    this the assertion would silently depend on test order.
    """
    while not _CURL_FALLBACK_TICKET.empty():
        _CURL_FALLBACK_TICKET.get_nowait()
    _CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)


def _lan_curl_run(argv, timeout):
    """Run one curl argv LIST (never a shell) -> (returncode, stdout, stderr).

    Injectable seam: the suite replaces this module attribute with a stub that
    inspects argv — which is also how 'the api_key never reaches argv' is
    proven, since every test asserts on the argv the stub was handed.
    """
    import subprocess
    p = subprocess.run(argv, capture_output=True, timeout=timeout + 10)
    return p.returncode, p.stdout, p.stderr


def _lan_curl_unusable(original, cause) -> Exception:
    """What the caller sees when curl could not be run at all.

    Same ``URLError`` family the caller already handles, and its text still
    carries the ORIGINAL errno-65 message — the hint is appended, never
    substituted, so a reader still learns the endpoint was denied.
    """
    return urllib.error.URLError(f"{original}{_CURL_UNUSABLE_HINT} [{cause}]")


def _lan_curl_argv(url, timeout, header_file=None) -> list:
    """The curl argv.

    ``-sS`` quiet but loud on error, ``--fail`` so a 4xx/5xx becomes exit 22
    (urllib's HTTPError equivalent), ``-L`` because urllib follows redirects
    too, ``--max-time`` so a hung node cannot hang the monitor. The URL is
    always the LAST token, and no token ever holds a secret.
    """
    argv = [_CURL_BIN, "-sS", "--fail", "-L", "--max-time", str(timeout)]
    if header_file:
        argv += ["-H", f"@{header_file}"]
    argv.append(url)
    return argv


def _lan_curl_write_header(api_key) -> str:
    """Write the Bearer header to a 0600 temp file and return its path.

    The api_key must NEVER be an argv token: ``ps`` shows every argument of a
    child process for the life of the request, so ``-H "Authorization: Bearer
    <key>"`` would leak the key to anyone on the box. curl reads the header
    from the file instead (``-H @path``), and the caller unlinks it.
    """
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix="hscc-lan-hdr-")
    try:
        os.fchmod(fd, 0o600)  # mkstemp is 0600 already; pin it explicitly
        with os.fdopen(fd, "w") as fh:
            fh.write(f"Authorization: Bearer {api_key}\n")
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _lan_curl_http_error(url, stderr) -> Exception:
    """Map curl's exit-22 back onto urllib's HTTPError family.

    Parity matters more than the number: callers branch on HTTPError to say
    'endpoint reachable, probe/auth wrong' as opposed to 'unreachable', so the
    fallback raises the same family urllib would, carrying the status curl
    reported. ``code`` is 0 when curl did not name one — the exception TYPE is
    what callers branch on.
    """
    import re
    m = re.search(r"returned error: (\d{3})",
                  (stderr or b"").decode("utf-8", "replace"))
    code = int(m.group(1)) if m else 0
    reason = f"curl fallback: HTTP {code}" if code else "curl fallback: HTTP error"
    return urllib.error.HTTPError(url, code, reason, None, None)  # type: ignore[arg-type]


def _lan_curl_fallback(url, timeout, exc, api_key=None):
    """Retry one denied GET through /usr/bin/curl -> the body bytes, or None.

    Returns the response body when the retry succeeded; None means 'not
    eligible, or curl could not reach it either' and the caller MUST re-raise
    the original error unchanged. ``api_key`` is only ever sent as a header
    from a 0600 temp file — never in argv, never in a message.
    """
    import os
    if _lan_errno(exc) != 65:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not _lan_private_host(parsed.hostname):
        return None
    header_file = None
    try:
        if api_key:
            header_file = _lan_curl_write_header(api_key)
        rc, out, err = _lan_curl_run(_lan_curl_argv(url, timeout, header_file),
                                     timeout)
    except Exception as cause:      # curl missing / not executable / timed out
        raise _lan_curl_unusable(exc, cause) from exc
    finally:
        if header_file:
            try:
                os.unlink(header_file)
            except OSError:
                pass
    if rc == 22:
        raise _lan_curl_http_error(url, err) from exc
    if rc != 0:
        # curl could not reach it either — it really is down. Let the original
        # errno-65 error stand with the caller's own wording.
        return None
    _lan_curl_notice_once()
    return out
# ── END mirrored block: lan_curl_fallback ──────────────────────────────────

def _http_get_default(url: str, api_key: Optional[str] = None) -> str:
    """Fetch ``url`` and return its body as text.

    Mirrors ``hscc-bootstrap.doctor._http_get_default``: sends ``Authorization:
    Bearer <key>`` when a key is provided (the configs carry api_key next to
    base_url; an endpoint that requires a key returns 401 without it), and
    raises ``urllib.error`` on failure so callers can tell a probe/config error
    apart from an unreachable endpoint.
    """
    import urllib.error
    import urllib.request
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        # A macOS Local-Network denial (errno 65 on a private host) retries
        # once through /usr/bin/curl; everything else propagates unchanged, so
        # callers still tell a probe error apart from an unreachable endpoint.
        body = _lan_curl_fallback(url, 10, e, api_key=api_key)
        if body is None:
            raise
        return body.decode("utf-8")


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
    # Never clobber an ORCHESTRATOR-role profile. Matching on base_url alone was
    # safe only while orchestrator profiles pointed at a node: under
    # uniform_pool they are re-aimed at this very proxy, so a URL-only test
    # suddenly matched all 15 of them and rewrote model.default to the worker
    # alias — erasing the orchestrator role's intent and leaving
    # _set_orchestrator_profile_to_pool (which selects on that alias) unable to
    # repair them. Key on INTENT, not just on where the profile points today.
    if model_cfg.get("default") == "orchestrator-model":
        return config
    if model_cfg.get("default") != model_id:
        model_cfg["default"] = model_id
    return config


def _set_orchestrator_profile_to_pool(config: dict, pool_url: str,
                                      alias: str = "orchestrator-model") -> dict:
    """Re-aim an ORCHESTRATOR-role profile at the pool. ``uniform_pool`` only.

    ``_set_worker_model_in_profile`` deliberately leaves orchestrator-facing
    profiles alone, and under a normal template that is correct: the
    orchestrator really is one pinned node. Under ``uniform_pool`` it is not —
    every unit advertises the orchestrator alias, so leaving every ``*-orch``
    profile pinned sends ALL orchestrator work to one GPU while N-1 identical
    units idle, and takes every orchestrator down with that single node.

    Profiles are selected by INTENT (``model.default`` is the orchestrator
    alias) rather than by their current URL, so a profile left pointing at a
    stale or dead endpoint is repaired too — e.g. one found aimed at
    ``127.0.0.1:8000``, where nothing serves. Worker-facing profiles and
    profiles with no model block are untouched. Idempotent.
    """
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        return config
    if model_cfg.get("default") != alias:
        return config
    if model_cfg.get("base_url") != pool_url:
        model_cfg["base_url"] = pool_url
    return config


def _declared_profile_write(config: dict, model_id: str,
                            url: str) -> dict:
    """Point one role profile's model block at its declared endpoint.

    Both keys move together — ``model.base_url`` to the declared URL and
    ``model.default`` to the id probe-confirmed at that URL — because a profile
    aimed at a proxy with the previous family's model id 404s every call (the
    worker-tier switch footgun this file documents).

    The historical intent guard (``_set_worker_model_in_profile`` refusing
    ORCH-role profiles, and the base_url gate on every one) is deliberately NOT
    applied here: it exists because URL matching could not tell an orchestrator
    profile from a worker one. In declared mode the OPERATOR named this
    profile's target, which is exactly the intent that guard was approximating.
    A profile the declaration does not name keeps the guarded legacy path.
    """
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
        config["model"] = model_cfg
    if model_cfg.get("base_url") != url:
        model_cfg["base_url"] = url
    if model_cfg.get("default") != model_id:
        model_cfg["default"] = model_id
    return config


def _declared_endpoint_model(url: str, candidate: str, *, _http_get=None,
                             cache: Optional[dict] = None):
    """Probe-confirmed model id to write at ``url`` (memoised per apply).

    Same gate as every other write in this file — ``_routing_model_to_write`` —
    just shared between the delegation and profile consumers of one declared
    target so a template that points five profiles at one proxy probes it once.
    """
    key = (url, candidate)
    if cache is not None and key in cache:
        return cache[key]
    out = _routing_model_to_write(candidate, url, _http_get=_http_get)
    if cache is not None:
        cache[key] = out
    return out


def _update_worker_model_ids(plan: Any, *, profiles_dir: Optional[Path] = None,
                             config_yaml: Optional[Path] = None,
                             _http_get=None, skip_delegation: bool = False,
                             decision: Optional[dict] = None) -> dict:
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

    ``skip_delegation=True`` (set by apply_template when the ``delegation``
    consumer is governed elsewhere): the config.yaml delegation/fallback rewrite
    is SKIPPED. Pre-v4 that meant "the template carries a ``routing:`` block",
    because routing is then the sole authority for the consumer (a present key
    routes it; an OMITTED key must leave it untouched). v4 extends the same rule
    to ``endpoints:`` (§1.5): when a block is present and delegation is either
    DECLARED or deliberately omitted, this legacy heuristic write is skipped and
    ``_apply_endpoints`` owns the consumer. A source-"default" delegation entry
    (uniform_pool, nothing declared) still takes this path, which is precisely
    what apply did before the block existed. The worker role profiles rewrite
    still runs: routing has no ``profiles`` consumer, and declared profiles are
    applied after this step so the declaration always wins.

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
        # Under uniform_pool the orchestrator role is the POOL, so orch-role
        # profiles get re-aimed too (see _set_orchestrator_profile_to_pool).
        if getattr(plan, "uniform_pool", False):
            _, och = atomic_yaml_update(
                pfile,
                lambda d, u=proxy_url: _set_orchestrator_profile_to_pool(d, u))
            if och:
                result["orch_profiles_changed"] = \
                    result.get("orch_profiles_changed", 0) + 1
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


def _apply_endpoints(plan: Any, decision: Optional[dict], *,
                     config_yaml: Optional[Path] = None,
                     profiles_dir: Optional[Path] = None,
                     _http_get=None) -> dict:
    """Write the DECLARED endpoint consumers (spec §1.2 rows 5 + 7).

    Runs AFTER the routing step (precedence §1.4: "a key in both — endpoints
    wins") and after the worker-model re-aim, so the declaration always lands
    last on the consumers it names. Writes NOTHING for a consumer whose entry
    has source "default" or "legacy" — those are the §1.3 "leave it as tuned"
    and "naming what apply already did" cases, and writing them here would be
    exactly the overreach §7 forbids.

    The model id is probe-confirmed at the target URL by the same gate every
    other write in this file uses; a target that cannot confirm an id is
    REFUSED (nothing written) rather than pointed at an endpoint that would 404.

    Returns ``{"keys_written": [...], "changed": bool, "notes": [...],
    "resolved": {...}}`` — ``resolved`` is the per-consumer disclosure §1.5 asks
    apply to print ("which family each consumer was pointed at, and the probe
    status that gated each write").
    """
    empty = {"keys_written": [], "changed": False, "notes": [], "resolved": {}}
    if not decision:
        return empty

    probe_cache: dict = {}
    resolved: Dict[str, dict] = {}
    written: list = []
    notes: list = []
    changed_any = False

    delegation = decision.get("delegation")
    if delegation and delegation.get("source") == "declared":
        url, candidate = delegation.get("url"), delegation.get("candidate")
        entry = {"target": delegation.get("target"), "url": url}
        if url is None:
            entry.update(model=None, probe="refused",
                         note=delegation.get("reason"))
            notes.append(f"delegation:refused:({delegation.get('reason')})")
        else:
            model, status, _served = _declared_endpoint_model(
                url, candidate or "", _http_get=_http_get, cache=probe_cache)
            entry.update(model=model, probe=status)
            if model is None:
                notes.append(f"delegation:refused:({status})")
            else:
                conf = config_yaml or CONFIG_YAML
                _, ch = atomic_yaml_update(
                    conf, lambda d, m=model, u=url: _set_worker_model_in_config(
                        d, m, u))
                changed_any = changed_any or ch
                written.extend(["delegation.base_url", "delegation.model",
                                "fallback_providers[].base_url",
                                "fallback_providers[].model"])
        resolved["delegation"] = entry

    declared_profiles = {name: prof for name, prof in
                         (decision.get("profiles") or {}).items()
                         if prof.get("source") == "declared"}
    if declared_profiles:
        pd = profiles_dir or PROFILES_DIR
        for pfile in sorted(pd.glob("*/config.yaml")):
            if not pfile.is_file():
                continue
            name = _profile_name_from_path(pfile)
            prof = declared_profiles.get(name)
            if prof is None:
                continue
            url, candidate = prof.get("url"), prof.get("candidate")
            entry = {"target": prof.get("target"), "url": url,
                     "selector": prof.get("selector")}
            if url is None:
                entry.update(model=None, probe="refused",
                             note=prof.get("reason"))
                notes.append(f"profile {name}:refused:({prof.get('reason')})")
                resolved[f"profiles.{name}"] = entry
                continue
            model, status, _served = _declared_endpoint_model(
                url, candidate or "", _http_get=_http_get, cache=probe_cache)
            entry.update(model=model, probe=status)
            if model is None:
                notes.append(f"profile {name}:refused:({status})")
            else:
                _, ch = atomic_yaml_update(
                    pfile, lambda d, m=model, u=url: _declared_profile_write(
                        d, m, u))
                changed_any = changed_any or ch
                written.append(f"profiles/{name}/config.yaml:model")
            resolved[f"profiles.{name}"] = entry

    for consumer in decision.get("untouched") or []:
        resolved[consumer] = {"target": None, "url": None, "model": None,
                              "probe": "omitted",
                              "note": "not declared — left untouched"}
    return {"keys_written": written, "changed": changed_any, "notes": notes,
            "resolved": resolved}


def _preview_endpoints(plan: Any, decision: Optional[dict], *,
                       _http_get=None) -> list:
    """Preview disclosure for the DECLARED endpoint consumers (READ-ONLY).

    Uses the same resolution + probe helpers ``_apply_endpoints`` uses, so the
    preview can never claim a route apply would not write (the doctrine
    ``_preview_routing`` established). Probing ``/v1/models`` is a read-only GET;
    nothing here writes.
    """
    if not decision:
        return []
    cache: dict = {}
    rows = []
    for consumer in ENDPOINT_CONSUMERS:
        entry = decision.get(consumer)
        if not entry or entry.get("source") != "declared":
            continue
        row = {"consumer": consumer, "target": entry.get("target"),
               "url": entry.get("url")}
        model, status, _ = _declared_endpoint_model(
            entry.get("url") or "", entry.get("candidate") or "",
            _http_get=_http_get, cache=cache)
        row["model"] = model
        if model is None:
            row["note"] = (entry.get("reason")
                           or f"refused:({status})")
        rows.append(row)
    for name, entry in sorted((decision.get("profiles") or {}).items()):
        if entry.get("source") != "declared":
            continue
        row = {"consumer": f"profiles.{name}", "target": entry.get("target"),
               "url": entry.get("url"), "selector": entry.get("selector")}
        model, status, _ = _declared_endpoint_model(
            entry.get("url") or "", entry.get("candidate") or "",
            _http_get=_http_get, cache=cache)
        row["model"] = model
        if model is None:
            row["note"] = entry.get("reason") or f"refused:({status})"
        rows.append(row)
    return rows


# ── v4 `proxy:` + `caps:` apply legs (spec §2.3 / §3) ───────────────────────
#
# §2.3 gives apply ownership of exactly two proxy.yaml regions and NO MORE:
#   1. `router_settings:`        — from declared knobs that map to LiteLLM
#                                  router_settings. v1: NONE by default (§2.1
#                                  keeps `routing_strategy` out of the template
#                                  vocabulary; §5 refuses it), so this region
#                                  is intentionally not written here.
#   2. `gateways.litellm.balancing:` — from `proxy.*.routing` (§2.2), with the
#                                  blessed default `least-in-flight` under
#                                  uniform_pool (2026-10-19 addendum item 3).
#   3. per-deployment extras     — recorded and validated by C1, passed through
#                                  in the shape §4.3 consumes LATER. Writing
#                                  them today is a no-op the spec calls INERT
#                                  (§8), so apply records them into
#                                  applied_template.json and does NOT write
#                                  them into proxy.yaml: a key sparkrun drops
#                                  would read as enforced intent.
#
# Both writes go through ONE function — `sparkrun_bridge.proxy_config_write` —
# which drives sparkrun's own ProxyConfig under sparkrun's venv python. hscc
# never opens proxy.yaml and never touches the regenerated
# ~/.cache/sparkrun/proxy/litellm_config.yaml (§4.4 / §9).

#: The proxy.yaml section + key the balancing toggle lives in — NESTED, per the
#: 2026-10-19 addendum (sparkrun 0.4.0 reads ``gateways.litellm.router_settings``
#: / ``...balancing`` via ``ProxyConfig.gateway_settings("litellm")``; the
#: top-level key is honored only by the 0.3.10 rollback line).
BALANCING_REGION = ("gateways", "litellm")

#: The §2.2 default: a uniform_pool apply balances least-in-flight unless the
#: template says otherwise. NOT applied to non-uniform templates (§7 — that
#: would be a behavior change on an untouched template).
BALANCING_DEFAULT = "least-in-flight"

#: Bound for monkeypatch visibility only (tests/conftest.py replaces
#: ``_sparkrun_proxy_write`` and records calls here); the code never reads it.
_SPARKRUN_WRITE_CALLS: list = []


def balancing_declaration(tpl: Any, plan: Any) -> Tuple[Optional[str], str]:
    """The balancing policy apply must write, and WHY it chose it.

    Returns ``(policy, source)`` where ``source`` is one of ``declared``,
    ``uniform_pool-default``, ``none``. ``declared`` wins verbatim; the default
    fires ONLY under uniform_pool (§7 pins that scope — a v3 non-uniform
    template must not gain a proxy.yaml key).
    """
    fams = (tpl.proxy or {}).get("families") if isinstance(tpl.proxy, dict) else None
    policies = []
    if isinstance(fams, dict):
        for knobs in fams.values():
            if isinstance(knobs, dict) and knobs.get("routing"):
                pol = str(knobs["routing"])
                if pol not in policies:
                    policies.append(pol)
    if len(policies) > 1:
        # §2.2/§5: two policies are not expressible today. The structural layer
        # refuses this template, so apply should never see it — but decide
        # deterministically rather than let dict order pick a winner.
        return None, f"conflicting:{sorted(policies)}"
    if policies:
        return policies[0], "declared"
    if getattr(plan, "uniform_pool", False):
        return BALANCING_DEFAULT, "uniform_pool-default"
    return None, "none"


def _apply_proxy_declaration(tpl: Any, plan: Any, *, _write=None) -> dict:
    """Write the declared proxy regions through sparkrun's own write path.

    §2.3's restart rule: "write, and restart the proxy only when the region
    actually changed". This card NEVER restarts the proxy — the operator moved
    restarts to the detached cutover unit (2026-10-19 addendum item 4), and the
    spec's own §9 keeps that in operator/orchestrator territory. A change
    therefore reports ``restart_required`` plus the unit to run.

    ``_write(regions)`` is injectable for tests (t_ac795424 doctrine); the
    default is ``sparkrun_bridge.proxy_config_write``. Fail-closed: when the
    write path reports failure, the step is ``warn`` (apply is then not a
    success) and NOTHING is written by hand as a fallback.
    """
    policy, source = balancing_declaration(tpl, plan)
    result: dict = {"policy": policy, "source": source, "written": False,
                    "changed": False, "restart_required": False,
                    "inert_recorded": [], "error": None, "note": ""}
    if source.startswith("conflicting"):
        result["error"] = (f"proxy: balancing is global today — families disagree "
                           f"({source.split(':', 1)[1]}); declare one value")
        return result
    if policy is None:
        # Nothing to write: non-uniform template with no `proxy:` routing
        # declaration. Byte-identity (§7) requires the write path stay COLD here.
        result["note"] = "no balancing declared — proxy.yaml untouched"
        return result

    regions = {BALANCING_REGION[0]: {BALANCING_REGION[1]: {"balancing": policy}}}
    write = _write or _sparkrun_proxy_write
    outcome = write(regions)
    if not outcome.get("ok"):
        result["error"] = outcome.get("error") or "sparkrun write path failed"
        result["note"] = ("declared balancing NOT written (sparkrun write path "
                          "failed); nothing was hand-written")
        return result
    result["written"] = True
    result["changed"] = bool(outcome.get("changed"))
    result["restart_required"] = bool(outcome.get("changed"))
    result["mode"] = outcome.get("mode")
    if result["changed"]:
        result["note"] = (
            f"gateways.litellm.balancing -> {policy} ({source}); LiteLLM reads "
            f"router settings only at startup, so the proxy must be restarted "
            f"by the operator cutover unit "
            f"docs/audits/sparkrun_proxy_venv_cutover.sh — apply never "
            f"restarts it")
    else:
        result["note"] = (f"gateways.litellm.balancing already {policy} — "
                          f"no change, no restart needed")

    # §2.3 item 3: pass the per-deployment knobs through in the shape §4.3
    # consumes later. Recorded, never written, and labelled INERT (§8).
    fams = (tpl.proxy or {}).get("families") if isinstance(tpl.proxy, dict) else None
    if isinstance(fams, dict):
        for name, knobs in fams.items():
            if not isinstance(knobs, dict):
                continue
            inert = {k: v for k, v in knobs.items() if k in PRO_INERT_KNOBS}
            if inert:
                result["inert_recorded"].append(
                    {"family": name, "knobs": inert,
                     "status": "INERT-PENDING-SPARKRUN (§4.3/§8)"})
    return result


def _sparkrun_proxy_write(regions: dict) -> dict:
    """Indirection point for sparkrun's proxy.yaml write (test seam)."""
    try:
        from . import sparkrun_bridge  # package-relative (deployed as hscc-cluster)
    except ImportError:
        import sparkrun_bridge
    return sparkrun_bridge.proxy_config_write(regions)


def _apply_caps_declaration(tpl: Any, *, config_yaml: Optional[Path] = None,
                            _report_only: bool = True) -> dict:
    """Write declared kanban caps into hermes config.yaml (§3).

    Precedence (§3.1): explicit ``caps.*`` wins over derived, and the
    DERIVATION IS ADVISORY — §8 marks its only input (``max_parallel_requests``)
    INERT, so ``derived_max_in_progress`` is a plan value, not a physical
    ceiling. Per §3.1's own instruction, the first implementation card DERIVES
    AND REPORTS and never derives-and-writes, so only an EXPLICIT value is
    written here.

    When nothing is declared and no ``proxy:`` block exists, this writes
    NOTHING: the migration default is "30/10 fill-if-absent, exactly
    ``enable_plugins.py`` today" (§3.1/§7), and that fill-if-absent belongs to
    bootstrap, not to apply — writing the same numbers from here would give two
    owners of one key.

    Idempotent (the update fn only assigns when the value differs) and
    probe-free.
    """
    caps = tpl.caps if isinstance(tpl.caps, dict) else {}
    result: dict = {"written": False, "changed": False, "keys": [],
                    "declared": {}, "derived": None, "note": ""}

    writes = {}
    for key in ("max_in_progress", "max_in_progress_per_profile"):
        val = caps.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            writes[key] = val
    result["declared"] = dict(writes)

    derived = derived_max_in_progress(tpl)
    if derived is not None:
        result["derived"] = {"value": derived[0], "basis": derived[1],
                             "advisory": "input INERT (§8) — derive-and-report"}
        if "max_in_progress" in writes:
            result["note"] = (f"derived cap {derived[0]}; declared "
                              f"{writes['max_in_progress']}; declared wins "
                              f"(§3.1)")
        else:
            result["note"] = (f"derived cap {derived[0]} from {derived[1]}; "
                              f"NOT written — derivation is advisory until its "
                              f"inputs are enforced (§3.1/§8)")

    if not writes:
        if not result["note"]:
            result["note"] = ("no caps declared — migration default stands "
                              "(30/10 fill-if-absent, enable_plugins.py)")
        return result

    def _upd(d: dict) -> dict:
        kanban = d.get("kanban")
        if not isinstance(kanban, dict):
            kanban = {}
            d["kanban"] = kanban
        for key, val in writes.items():
            if kanban.get(key) != val:
                kanban[key] = val
        return d

    conf = config_yaml or CONFIG_YAML
    _, changed = atomic_yaml_update(conf, _upd)
    result["written"] = True
    result["changed"] = bool(changed)
    result["keys"] = [f"kanban.{k}" for k in sorted(writes)]
    return result


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

def _emit_template(args, result) -> None:
    """Emit a template command result: raw JSON with ``--json``, else themed.

    The machine ``--json`` path stays ``json.dumps(result, indent=2)``
    byte-identical (daemon / scripts parse it). The human path renders a themed
    panel through the shared ``_theme`` layer (plain on non-TTY). Content-derived
    lines are escaped so literal Rich markup in template content renders
    literally.
    """
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return
    esc = _theme.escape
    console = _theme.make_console()
    if isinstance(result, dict) and result.get("error"):
        usage = result.get("usage")
        body = esc(str(result["error"]))
        if usage:
            body = f"{body}\n\nusage: {esc(str(usage))}"
        console.print(_theme.status_panel(
            body, status="error", title=f"template {args.command}"))
        return

    def _flatten(value, depth=0):
        pad = "  " * depth
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{pad}{esc(str(k))}:")
                    _flatten(v, depth + 1)
                elif isinstance(v, list) and not v:
                    lines.append(f"{pad}{esc(str(k))}: (none)")
                else:
                    lines.append(f"{pad}{esc(str(k))}: {esc(str(v))}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    _flatten(item, depth + 1)
                else:
                    lines.append(f"{pad}- {esc(str(item))}")
        else:
            lines.append(f"{pad}{esc(str(value))}")

    lines = []
    _flatten(result)
    console.print(_theme.panel(f"template {args.command}", "\n".join(lines)))


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="HSCC Cluster Template Manager")
    parser.add_argument("--json", action="store_true",
                        help="emit raw JSON (machine path, byte-identical)")
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
        _emit_template(args, result)
    
    elif args.command == "preview":
        result = preview_template(args.template)
        _emit_template(args, result)
    
    elif args.command == "apply":
        result = apply_template(args.template, confirm=args.confirm)
        _emit_template(args, result)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
