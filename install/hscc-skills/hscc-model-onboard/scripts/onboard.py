#!/usr/bin/env python3
"""onboard.py — bring a new model online across the DGX Spark cluster.

End-to-end new-model onboarding: validate/scaffold a sparkrun recipe, populate
the offline NAS->node-local HF cache, wire every HSCC config layer, launch vLLM
on each serving node, and verify health + a coherent completion.

Topology is READ from ~/.hscc/config.json (never hardcoded) so it tracks the
real cluster (gateway + workers + NAS + ssh_user + port).

Phases (each is a subcommand; `all` runs them in order):
    plan    <model> <recipe>          # dry-run: show topology, cache, config diff
    cache   <model>                   # ensure NAS + offline NAS->node-local cache
    wire    <model> <recipe> --yes    # backup + write all HSCC config layers
    launch  <recipe>          --yes   # sparkrun run on each serving node
    verify  <model>                   # health + completion on all serving nodes
    all     <model> <recipe> --yes    # plan -> cache -> wire -> launch -> verify

Safety:
    - NODE PAUSE IS AUTHORITATIVE. A node held by ~/.hscc/node_pauses.json is
      never picked: it is skipped by cache (no rsync onto a machine the operator
      parked), reported as parked by plan, and reported expected-absent (NOT
      DOWN) by verify. `launch` has no pause awareness of its own because it
      delegates to `hscc cluster up`, which partitions the plan against the
      overlay. There is no --include-paused here either — see
      docs/ops/node-pause.md.
    - launch is a CALL to `hscc cluster up`, not a hand-built `sparkrun run`.
      sparkrun finds and re-issues a workload by an INTENT HASH of recipe plus
      --port / --served-model-name / --tp, so an argv built by hand that drops
      one of those starts a workload no later stop can match (t_8ad06a53) and
      that `--ensure` answers with a PARALLEL DUPLICATE (t_e1ff0e8e). The door
      builds the argv from serving.json; this script does not.
    - Mutating phases (wire/launch/all) require --yes.
    - wire backs up ~/.hscc + hermes root config + EVERY profile config first.
    - The hermes root is resolved at call time (never an import-time
      $HERMES_HOME join): under a profile session $HERMES_HOME is the profile
      dir, and mirroring model.default from there skips every profile (t_629a6842).
      $HSCC_HERMES_ROOT overrides the resolution explicitly.
    - Cache sync is additive (rsync -a, no --delete); NEVER rm -rf.
    - hermes config.yaml is edited line-targeted (model.default only) so the
      file's secrets are never rewritten/printed.

Pitfalls honored (see references/nas-cache.md):
    - macOS rsync has no remote->remote: run rsync FROM the NAS-mount host (gateway).
    - System python3 has no `yaml` module: YAML edits are line-based.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HSCC_HOME = os.path.expanduser(os.environ.get("HSCC_HOME", "~/.hscc"))
# Deliberately NO sparkrun constant here. This script must not be able to build
# a sparkrun argv: the launch/stop argv is sparkrun's INTENT HASH (recipe plus
# --port / --served-model-name / --tp), and only ``hscc_daemon/serving.py`` may
# derive it (``_unit_run_cmd`` / ``unit_stop_cmd``). A constant pointing at the
# binary is what made the old ``phase_launch`` look like it was allowed to call
# it directly — and it was not (see :func:`phase_launch`). ``hscc`` is the door.
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[0;33m"; B = "\033[0;34m"; NC = "\033[0m"


# ── Hermes ROOT resolution (t_629a6842) ──────────────────────────────────────
# This used to be three module-level constants:
#
#     HERMES_HOME  = os.environ.get("HERMES_HOME", "~/.hermes")
#     HERMES_CONFIG = HERMES_HOME/config.yaml
#     PROFILES_DIR  = HERMES_HOME/profiles
#
# ``HERMES_HOME`` is NOT the Hermes root. Hermes exports it as the *profile* dir
# (``<root>/profiles/<name>``) for every profile session — the fleet's normal run
# mode, not a misconfiguration — so from a dispatched worker ``PROFILES_DIR``
# resolved to ``<profile>/profiles``, which does not exist. The consequence at
# :func:`wire_hermes_and_profiles`: the per-profile ``model.default`` mirroring
# iterated nothing and silently skipped EVERY profile, while the "root" config it
# did edit was the profile's own — the real root config and all sibling profiles
# kept the old model with a green line printed. Measured on a fabricated root
# with two profiles: root and beta stayed on the old model; only the active
# profile moved.
#
# The root is therefore resolved at CALL time (import-time env is fragile for a
# script run from mixed contexts — a bare terminal, a profile session, a cron
# watcher) through the shared homes ladder in ``hscc_daemon/hermes_python.py``
# (t_314eb13c), with the same guarded fallback + drift pin used by
# ``hscc-bootstrap/apply_patches.py`` and ``ensure_review_feature.py``.
#
# ``HSCC_HERMES_ROOT`` is an explicit operator override: when set it IS the root,
# no ladder, no existence probe (so a non-standard install location works).

#: Explicit override for the Hermes ROOT (see block comment above).
HERMES_ROOT_ENV = "HSCC_HERMES_ROOT"
#: What makes a candidate a Hermes ROOT rather than a profile dir. A root
#: carries ``profiles/`` and/or a root ``config.yaml``; a profile dir ALSO
#: carries its own ``config.yaml``, so the marker test alone is not enough —
#: :func:`_is_profile_dir` (parent named ``profiles``, the same rule the ladder
#: and ``doctor.hermes_root_from_home`` use) excludes it.
_ROOT_MARKERS = ("profiles", "config.yaml")


def _load_hermes_python():
    """Import ``hscc_daemon/hermes_python.py`` for the shared homes ladder.

    Deferred + guarded, same pattern as ``apply_patches._load_hermes_python``:
    the resolution rule lives with the shared ladder (t_314eb13c) so this skill
    script cannot drift from how the rest of HSCC finds the Hermes install.
    Returns the module, or None when ``hscc_daemon`` is not reachable from this
    script (a skills-only install), in which case :func:`_fallback_homes` covers
    us. Walk-up scan rather than a fixed ``../../..`` because the script sits at
    different depths in the repo (``install/hscc-skills/<skill>/scripts/``) than
    in the deployed tree, and the deployed payload keeps both dirs as siblings.
    """
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(8):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        if os.path.isfile(os.path.join(d, "hscc_daemon", "hermes_python.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                from hscc_daemon import hermes_python
                return hermes_python
            except Exception:
                return None
    return None


def _fallback_homes(env=None, *, home=None):
    """Candidate Hermes ROOTS when ``hscc_daemon`` is unavailable.

    Deliberately the SAME three steps as ``hermes_python.hermes_homes`` —
    exported ``$HERMES_HOME``, its ``profiles/`` grandparent, then
    ``~/.hermes`` — because a shorter list here is exactly the bug being fixed:
    any resolver that looks only at the exported home misses every sibling
    profile from a profile session. ``test_ladder_drift`` pins the two against
    each other so this cannot drift silently.
    """
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    homes = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in homes:
            homes.append(path)

    _push(env.get("HERMES_HOME", "").strip())
    if homes:
        parent = os.path.dirname(homes[0])
        if parent and os.path.basename(parent) == "profiles":
            _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser(str(home)), ".hermes"))
    return homes


def hermes_home_candidates(env=None, *, home=None):
    """Candidate Hermes ROOTS, in the order they are tried.

    An explicit ``$HSCC_HERMES_ROOT`` short-circuits the ladder: it is the only
    candidate, so an operator override can never be second-guessed by the
    profile walk-up or by ``~/.hermes``.
    """
    env = os.environ if env is None else env
    override = (env.get(HERMES_ROOT_ENV) or "").strip()
    if override:
        return [os.path.expanduser(override)]
    hp = _load_hermes_python()
    if hp is not None:
        return hp.hermes_homes(env, home=home)
    return _fallback_homes(env, home=home)


def _is_profile_dir(path):
    """True when ``path`` looks like ``<root>/profiles/<name>``.

    The same parent-name rule as ``hscc_daemon.hermes_python.hermes_homes``,
    ``hscc-bootstrap/doctor.hermes_root_from_home`` and ``rolelib``. It is
    needed here because a profile dir carries its OWN ``config.yaml``, so the
    root-marker test alone would happily accept the profile a session is
    running under — which is the bug this function exists to prevent.
    """
    parent = os.path.dirname(os.path.normpath(str(path)))
    return bool(parent) and os.path.basename(parent) == "profiles"


def resolve_hermes_root(env=None, *, home=None):
    """Resolve the Hermes ROOT for config mirroring: ``(root_or_None, searched)``.

    Walks :func:`hermes_home_candidates` in priority order (exported
    ``$HERMES_HOME``, its ``profiles/`` grandparent, ``~/.hermes``) and takes the
    first candidate that (a) is not itself a ``profiles/<name>`` dir and (b)
    carries a root marker — ``profiles/`` or a root ``config.yaml``. Condition
    (a) is what makes this fix work: under a profile session the ladder's first
    candidate IS the profile dir, and it has a ``config.yaml``.

    An explicit ``$HSCC_HERMES_ROOT`` is honoured verbatim — no profile-dir
    exclusion, no marker requirement — because an operator who names a root
    knows their layout better than a heuristic does.

    Returns ``(None, searched_detail)`` when nothing qualifies. Callers must
    report that as "no Hermes home here", never let it read as a no-op: a silent
    skip is exactly how this step printed green while every profile kept the old
    model (t_629a6842).
    """
    env = os.environ if env is None else env
    override = (env.get(HERMES_ROOT_ENV) or "").strip()
    cands = hermes_home_candidates(env, home=home)
    if override:
        return (cands[0], "") if os.path.isdir(cands[0]) else (None, cands[0])
    for cand in cands:
        try:
            if _is_profile_dir(cand):
                continue
            if any(os.path.exists(os.path.join(cand, m)) for m in _ROOT_MARKERS):
                return cand, ""
        except OSError:  # pragma: no cover - unreadable path is not a hit
            continue
    return None, ", ".join(cands) or "no candidate paths"


def hermes_paths(env=None, *, home=None):
    """``(root, config_path, profiles_dir)`` for the resolved Hermes ROOT.

    ``(None, None, None)`` when there is no home. :func:`wire_hermes_and_profiles`
    and :func:`backup_configs` both go through here so ONE resolution decides what
    gets read, backed up and written — they can no longer disagree about which
    tree is "the" one (the old code did the join twice, at :42-43 and again at
    :207-212/:317-323).
    """
    root, _ = resolve_hermes_root(env, home=home)
    if root is None:
        return None, None, None
    return (root,
            os.path.join(root, "config.yaml"),
            os.path.join(root, "profiles"))


def c(color, msg):
    return f"{color}{msg}{NC}"


def die(msg):
    print(c(R, f"ERROR: {msg}"))
    sys.exit(1)


# ── Node-pause overlay (the skip seam) ───────────────────────────────────────
#
# ``hscc node pause`` parks one node and edits NOTHING the cluster is built
# from (``hscc_daemon/node_pauses.py``). That is exactly what makes an
# overlay-blind onboarding wrong: ``serving.json`` still lists the parked node's
# units, so a script that reads only config.json + serving.json sees a node that
# should be running and "fixes" it — rsyncing gigabytes onto a machine the
# operator parked for a training run, then launching vLLM back onto it.
#
# THREE RULES, all taken from node_pauses' own docstring ("WHAT A SKIP SEAM
# MUST ASK"):
#
#   1. Ask ``covering`` / ``paused_nodes``, NEVER ``paused``. A pause record is
#      keyed by the host it was issued against and a tensor-parallel unit's
#      other hosts ride in its ``span_nodes``; they are just as parked. A seam
#      that asked ``paused(host)`` per node would relaunch the peer of a span
#      the operator parked.
#   2. Expiry does not release a node. A record whose ``until`` has passed is
#      still a pause until the daemon's own auto-resume (or ``hscc node
#      resume``) ends it; treating "deadline passed" as free relaunches the unit
#      a few seconds before the daemon was about to.
#   3. Unreadable overlay -> FAIL OPEN to "nothing parked", and SAY so. A
#      missing file genuinely means nothing is paused (``load()`` cannot raise);
#      a corrupt one must not strand a cutover, but silently ignoring a
#      readable record would.
#
# Read through ``hscc_daemon.node_pauses`` when it is importable (the shared
# implementation, so this script cannot drift from how the daemon and the CLI
# read the same file), with a standalone fallback pinned against it by
# ``test_overlay_drift`` in test_model_onboard_hermes_root.py — same
# ladder-plus-drift-pin discipline as ``_fallback_homes``.

OVERLAY_NAME = "node_pauses.json"


def _load_node_pauses():
    """Import ``hscc_daemon.node_pauses``, or None when hscc_daemon is absent.

    Deferred + guarded (see :func:`_load_hermes_python` for the walk-up
    rationale): the overlay's parse rules live with the overlay, and a
    skills-only install has to fall back rather than die.
    """
    try:
        from hscc_daemon import node_pauses
        return node_pauses
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(8):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        if os.path.isfile(os.path.join(d, "hscc_daemon", "node_pauses.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                from hscc_daemon import node_pauses
                return node_pauses
            except Exception:
                return None
    return None


def _fallback_parked_nodes(directory=None):
    """Standalone read of the overlay: keyed hosts ∪ their ``span_nodes``.

    The SAME set ``node_pauses.paused_nodes`` returns, reached without the
    package, mirroring its normalisation rule for rule: no clock (rule 2); a
    non-string host key or a NON-OBJECT record drops the whole record — so the
    keyed host is NOT parked either, which is the shared reader's fail-open and
    not a licence to park; a non-list ``span_nodes`` degrades to empty instead
    of crashing; a blank span entry is dropped. A fallback that quietly
    disagrees with the daemon is the bug, so
    :mod:`test_model_onboard_pause_aware_t_ed77d757` diffs the two on fabricated
    overlays every run, the way ``test_hermes_home_ladder.py::test_ladder_drift``
    pins the Hermes-home ladder.
    """
    path = os.path.join(directory or HSCC_HOME, OVERLAY_NAME)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    pauses = data.get("pauses") if isinstance(data, dict) else None
    if not isinstance(pauses, dict):
        return set()
    out = set()
    for host, entry in pauses.items():
        if not isinstance(host, str) or not host.strip():
            continue
        if not isinstance(entry, dict):
            continue                    # unusable record: fails open, like the reader
        out.add(host.strip())
        span = entry.get("span_nodes")
        if isinstance(span, (list, tuple)):
            out.update(n.strip() for n in span if isinstance(n, str) and n.strip())
    return out


def _overlay_readable(directory=None):
    """Whether ``node_pauses.json`` can be read, independent of what it says.

    The shared reader normalises a corrupt overlay to "no pauses" and WARNs on
    its OWN log, so its return value alone cannot distinguish "nothing is
    parked" from "your pause record is unreadable and every seam just failed
    open" — and the second one is the situation the operator must hear about,
    because it means a node they parked is about to be healed under them. So the
    SET comes from the shared reader (parity) and this check comes from the file
    (honesty). An absent file IS readable: no overlay genuinely means nothing is
    parked.
    """
    path = os.path.join(directory or HSCC_HOME, OVERLAY_NAME)
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            json.load(f)
        return True
    except (OSError, ValueError):
        return False


def parked_nodes(directory=None):
    """Every node the overlay holds (keyed hosts ∪ span nodes).

    ``(nodes, readable)``. ``readable`` is False only when the shared reader
    AND the file both gave nothing usable, so a caller can tell "nothing is
    parked" from "I could not tell" and say which — the difference an operator
    needs before concluding their parked node is broken.

    The directory is ALWAYS passed explicitly, never left to the module default:
    ``node_pauses.HSCC_DIR`` is a module-level ``~/.hscc`` captured at import,
    so calling ``paused_nodes()`` bare reads the OPERATOR'S overlay even when
    this process set ``HSCC_HOME`` (a sandboxed run, a test, a second cluster).
    Passing it is what keeps this read inside the same root every other ``~/.hscc``
    read in this script is inside.
    """
    directory = directory or HSCC_HOME
    np = _load_node_pauses()
    if np is not None:
        try:
            # The set from the shared reader (parity with every other seam); the
            # readable flag from the file, because the reader normalises
            # corruption away and its return value cannot report it.
            return set(np.paused_nodes(directory)), _overlay_readable(directory)
        except Exception:
            # A raising shared reader is an unreadable overlay, not "no pauses":
            # fall through so the caller still learns it could not tell.
            return _fallback_parked_nodes(directory), False
    path = os.path.join(directory, OVERLAY_NAME)
    if os.path.exists(path):
        return _fallback_parked_nodes(directory), _overlay_readable(directory)
    return set(), True


# ── Topology ────────────────────────────────────────────────────────────────

def load_topology():
    """Read cluster topology from ~/.hscc/config.json (single source of truth).

    ``serving_nodes`` is the WHOLE declared fleet and ``parked`` is the subset
    the node-pause overlay holds; a caller that must not disturb a parked node
    filters on the second set (see the overlay block above), never by dropping
    nodes from the first — plan and verify still have to REPORT a parked node,
    and a node that simply vanished from the topology reads as a broken cluster.
    """
    path = os.path.join(HSCC_HOME, "config.json")
    if not os.path.exists(path):
        die(f"missing {path} — is HSCC initialized?")
    cfg = json.load(open(path))
    gw = cfg["gateway_ip"]
    parked, readable = parked_nodes()
    all_nodes = [gw] + list(cfg.get("workers", []))
    return {
        "gateway": gw,
        "workers": list(cfg.get("workers", [])),
        "nas_ip": cfg.get("nas_ip"),
        "ssh_user": cfg.get("ssh_user", "spark"),
        "port": int(cfg.get("vllm_port", 8000)),
        # The NAS is mounted at /mnt/nas on the gateway; rsync source host = gateway.
        "nas_mount_host": gw,
        # serving nodes = gateway (orchestrator) + workers
        "serving_nodes": all_nodes,
        # The declared fleet MINUS the parked set: what cache/verify may touch.
        "active_nodes": [h for h in all_nodes if h not in parked],
        "parked": parked,
        "overlay_readable": readable,
    }


def note_parked(topo, label="node_pauses"):
    """Print the parked set once per phase (plan/cache/verify), if any.

    A skip that is not printed is indistinguishable from a node nobody
    remembered, and this script runs a cutover an operator is watching.
    """
    if not topo.get("overlay_readable", True):
        print(c(Y, f"  {label}: overlay unreadable — treating NO node as parked; "
                   f"check {os.path.join(HSCC_HOME, OVERLAY_NAME)} before trusting "
                   f"the skips below"))
    for h in sorted(topo.get("parked") or ()):
        print(c(Y, f"  {label}: {h} is PARKED — not picked. Put it back with "
                   f"'hscc node resume {h}' (hscc node paused lists why)"))


def ssh(user, host, cmd, timeout=30):
    """Run an SSH command; return (stdout, ok)."""
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={min(timeout,15)}",
             f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        return "", False
    except Exception as e:
        return str(e), False


# ── Model / cache helpers ─────────────────────────────────────────────────────

def hf_repo(model_id):
    """nvidia/Qwen3.6-35B-A3B-NVFP4 -> models--nvidia--Qwen3.6-35B-A3B-NVFP4"""
    return "models--" + model_id.replace("/", "--")


def nas_ref(topo, model_id):
    """refs/main hash for the model on NAS, or '' if absent."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], topo["nas_mount_host"],
                  f"cat /mnt/nas/hub/{repo}/refs/main 2>/dev/null")
    return out if ok else ""


def node_ref(topo, host, model_id):
    """refs/main hash for the model in a node's local cache, or '' if absent."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], host,
                  f"cat /home/<ssh_user>/.cache/huggingface/hub/{repo}/refs/main 2>/dev/null")
    return out if ok else ""


def nas_snapshot_files(topo, model_id, ref):
    """Count of files in the NAS snapshot dir for completeness comparison."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], topo["nas_mount_host"],
                  f"find /mnt/nas/hub/{repo}/snapshots/{ref}/ -type f 2>/dev/null | wc -l")
    return int(out) if ok and out.isdigit() else 0


def node_snapshot_files(topo, host, model_id, ref):
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], host,
                  f"find /home/<ssh_user>/.cache/huggingface/hub/{repo}/snapshots/{ref}/ -type f 2>/dev/null | wc -l")
    return int(out) if ok and out.isdigit() else 0


def rsync_nas_to_node(topo, host, model_id):
    """Additive rsync of the whole repo dir from NAS-mount host to a node.

    Runs FROM the gateway (which has /mnt/nas) because macOS rsync cannot do
    remote->remote. Uses -a (preserves layout) WITHOUT --delete (never removes
    files on the node). Creates the parent dir first.
    """
    repo = hf_repo(model_id)
    user = topo["ssh_user"]
    src = f"/mnt/nas/hub/{repo}/"
    dst = f"/home/<ssh_user>/.cache/huggingface/hub/{repo}/"
    ssh(user, host, f"mkdir -p {dst}")
    inner = (f'rsync -a -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no '
             f'-o UserKnownHostsFile=/dev/null" {src} {user}@{host}:{dst}')
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", f"{user}@{topo['nas_mount_host']}", inner],
            capture_output=True, text=True, timeout=1800)
        return r.returncode == 0, r.stderr.strip()[:300]
    except subprocess.TimeoutExpired:
        return False, "rsync timed out (30m)"


def node_cache_ready(topo, host, model_id, nref, nfiles):
    """True if a node's local cache matches NAS (same ref hash, same snapshot file count)."""
    if node_ref(topo, host, model_id) != nref or not nref:
        return False
    return node_snapshot_files(topo, host, model_id, nref) >= nfiles > 0


# ── Recipe helpers ────────────────────────────────────────────────────────────

def recipe_model(path):
    if not os.path.exists(path):
        return None
    for line in open(path):
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip()
    return None


CANONICAL_RECIPE = os.path.expanduser(
    "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml")


def scaffold_recipe(path, model_id):
    """Create a starter recipe from the canonical NVFP4 recipe with the model
    swapped and a TODO banner. Does NOT auto-launch — human must review."""
    if not os.path.exists(CANONICAL_RECIPE):
        die(f"no canonical recipe to scaffold from at {CANONICAL_RECIPE}")
    text = open(CANONICAL_RECIPE).read()
    text = re.sub(r"^model:.*$", f"model: {model_id}", text, count=1, flags=re.M)
    banner = (f"# SCAFFOLDED by onboard.py for {model_id} on "
              f"{datetime.now().isoformat(timespec='seconds')}\n"
              f"# REVIEW before use: quant, mods (exp-w4a16 for NVFP4), "
              f"max_model_len, container.\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(banner + text)
    print(c(Y, f"  scaffolded {path} — REVIEW then re-run"))


# ── Config wiring (with backup) ───────────────────────────────────────────────

def backup_configs():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(HSCC_HOME, f"_onboard-backup-{stamp}")
    os.makedirs(dest, exist_ok=True)
    for name in ("serving.json", "models.json", "provision.json", "agents.json"):
        src = os.path.join(HSCC_HOME, name)
        if os.path.exists(src):
            shutil.copy2(os.path.realpath(src), os.path.join(dest, name))
    root, hermes_config, profiles_dir = hermes_paths()
    if not (root and hermes_config and profiles_dir):
        print(c(Y, "  hermes: no Hermes home resolved — config NOT backed up"))
    else:
        if os.path.exists(hermes_config):
            shutil.copy2(hermes_config, os.path.join(dest, "hermes-config.yaml"))
        # Back up every profile that has a config.yaml: wire mirrors model.default
        # across ROOT/profiles/*, so a backup set that only carried the old
        # hard-coded "worker" (and, under a profile session, carried nothing at
        # all because PROFILES_DIR was <profile>/profiles) was not a rollback
        # point for what wire actually writes.
        for n in _profile_names(profiles_dir):
            p = os.path.join(profiles_dir, n, "config.yaml")
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(dest, f"profile-{n}-config.yaml"))
    print(c(B, f"  backup -> {dest}"))
    return dest


def _load(name):
    return json.load(open(os.path.realpath(os.path.join(HSCC_HOME, name))))


def _save(name, data):
    path = os.path.realpath(os.path.join(HSCC_HOME, name))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _load_serving_module():
    """``hscc_daemon.serving``, or None when the daemon package is unreachable.

    Same deferred + guarded walk-up as :func:`_load_hermes_python` /
    :func:`_load_node_pauses`: the fleet's launch argv has ONE builder
    (``serving.fleet_up_plan`` -> ``_unit_run_cmd``) and the only way for this
    script to record a ``serve_cmd`` that agrees with it is to ask it. A
    skills-only install gets None and the caller degrades honestly (see
    :func:`wire_serving`), never by inventing an argv.
    """
    try:
        from hscc_daemon import serving
        return serving
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(8):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        if os.path.isfile(os.path.join(d, "hscc_daemon", "serving.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                from hscc_daemon import serving
                return serving
            except Exception:
                return None
    return None


def _refreshed_serve_cmds(d):
    """``{unit_id: sanctioned serve argv}`` from the daemon's OWN plan builder.

    Returns ``{}`` when ``hscc_daemon.serving`` is unreachable or the plan is
    empty. Empty is NOT "no drift": the caller must then clear the stale records
    rather than keep them, because a recorded ``serve_cmd`` is a promise about
    the exact argv a resume will replay — and after :func:`wire_serving` moved
    every unit to a new ``model``/``recipe``, the old promise is false.

    ``fleet_up_plan`` is used because it is the public entry to the one builder
    (``_unit_run_cmd``) and because it derives every argv from the unit's FIELDS
    — ``prefer_serve_cmd`` is the node-resume path only, so the stale record
    already in ``d`` cannot leak into what comes back. That is the property this
    function depends on, and
    :func:`test_wire_serving_refresh_derives_from_fields_not_the_record` is what
    fails if anyone ever routes it through the record.
    """
    serving = _load_serving_module()
    if serving is None:
        return {}
    try:
        plan = serving.fleet_up_plan(d)
    except Exception:
        return {}
    out = {}
    for entry in plan or []:
        if isinstance(entry, dict) and entry.get("unit_id") and entry.get("cmd"):
            out[str(entry["unit_id"])] = list(entry["cmd"])
    return out


def wire_serving(model_id, recipe):
    """Point every declared unit at the new model + recipe, keeping the record true.

    ``model`` and ``recipe`` are the fields; ``serve_cmd`` is the RECORDED argv
    the apply path wrote and the node-resume path replays. Leaving it describing
    the old launch costs three things, all of them silent at wire time:
    ``_unit_run_cmd(prefer_serve_cmd=True)`` replays a placement-matching record
    with its non-placement flags intact (so ``--tp`` keeps the old value while
    the field says otherwise), ``serving._serve_cmd_mismatch`` fires the serve_cmd
    drift canary on every daemon tick afterwards, and the moment the recipe
    differs the planner logs ``serve_cmd refused ... using derived command`` on
    every plan. The record is therefore refreshed from the sanctioned plan
    builder in the same write, or CLEARED with a printed reason. Never left
    describing a launch that no longer exists.

    Placement (``nodes``, ``port``, ``tp``, ``role``, ``keepalive``,
    ``max_num_seqs``) is untouched: this verb changes WHAT is served, never
    WHERE, and rewriting placement here would fight the template apply that owns
    it.
    """
    d = _load("serving.json")
    units = d.get("units", []) or []
    # Fields FIRST, plan SECOND. ``fleet_up_plan`` derives --served-model-name
    # from ``model`` and the recipe position from ``recipe``; deriving before
    # the rewrite re-rendered the record from the values we had just replaced
    # and printed a confident "re-rendered" over a stale intent.
    for u in units:
        u["model"] = model_id
        u["recipe"] = recipe
    fresh = _refreshed_serve_cmds(d)
    stale = 0
    for u in units:
        key = str(u.get("id") or ",".join(u.get("nodes") or []))
        if fresh.get(key):
            u["serve_cmd"] = fresh[key]
        elif "serve_cmd" in u:
            u.pop("serve_cmd")
            stale += 1
    _save("serving.json", d)
    print(c(G, f"  serving.json: {len(units)} units -> {model_id}"))
    if fresh:
        print(c(G, f"  serving.json: {len(fresh)} recorded serve_cmd(s) "
                   f"re-rendered from the fleet plan builder"))
    elif stale:
        print(c(Y, f"  serving.json: {stale} stale serve_cmd(s) CLEARED — "
                   f"hscc_daemon.serving unreachable, so the sanctioned argv "
                   f"could not be re-rendered. 'hscc cluster template apply' "
                   f"(or one 'hscc cluster up') re-records them; resume uses the "
                   f"derived command until then"))
    return units



def wire_models(model_id):
    d = _load("models.json")
    d["primary_model"] = model_id
    models = d.get("models", [])
    if models:
        models[0]["name"] = model_id
        models[0]["status"] = "cached"
    else:
        d["models"] = [{"name": model_id, "type": "llm", "status": "cached",
                        "location": "vLLM", "tp": 1, "pp": 1}]
    _save("models.json", d)
    print(c(G, f"  models.json: primary_model -> {model_id}"))


def wire_provision(recipe):
    d = _load("provision.json")
    n = 0
    for _, mp in d.get("mappings", {}).items():
        mp["recipe"] = recipe
        n += 1
    _save("provision.json", d)  # history left untouched (audit log)
    print(c(G, f"  provision.json: {n} active mappings -> recipe (history kept)"))


def wire_agents(model_id):
    """Swap only the model part of each agent's `vllm-<ip>/<model>` string,
    preserving the per-agent routing IP."""
    path = os.path.realpath(os.path.join(HSCC_HOME, "agents.json"))
    d = json.load(open(path))
    n = 0
    for a in d.get("agents", []):
        m = a.get("model", "")
        new = re.sub(r"^(vllm-\d+\.\d+\.\d+\.\d+/).*", r"\1" + model_id, m)
        if new != m:
            a["model"] = new
            n += 1
    d["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    print(c(G, f"  agents.json: {n} agents re-pointed (routing IPs preserved)"))


def _profile_names(profiles_dir):
    """Sorted profile directory names under ``profiles_dir`` (skips dot-dirs)."""
    if not profiles_dir or not os.path.isdir(profiles_dir):
        return []
    return sorted(n for n in os.listdir(profiles_dir)
                  if not n.startswith(".") and os.path.isdir(os.path.join(profiles_dir, n)))


def set_yaml_model_default(path, model_id):
    """Line-targeted edit of model.default in a YAML file. Only the value on the
    `default:` line inside the top-level `model:` block is changed; the rest of
    the file (including secrets) is preserved byte-for-byte."""
    if not os.path.exists(path):
        return False
    lines = open(path).readlines()
    in_model = False
    changed = False
    for i, line in enumerate(lines):
        if re.match(r"^model:\s*$", line):
            in_model = True
            continue
        if in_model:
            # left the block: a new top-level key (col 0, non-space)
            if line and not line[0].isspace() and not line.startswith("#"):
                in_model = False
                continue
            m = re.match(r"^(\s+default:\s*).*$", line)
            if m:
                lines[i] = f"{m.group(1)}{model_id}\n"
                changed = True
                in_model = False
    if changed:
        tmp = path + ".tmp"
        open(tmp, "w").writelines(lines)
        os.replace(tmp, path)
    return changed


def wire_hermes_and_profiles(model_id):
    """Mirror ``model.default`` into the Hermes root config and EVERY profile.

    The walk is over ``ROOT/profiles/*`` — resolved by :func:`resolve_hermes_root`,
    not by joining ``$HERMES_HOME`` (a profile session's ``$HERMES_HOME`` is the
    profile dir, so the old walk found no ``profiles/`` under it and skipped every
    profile while editing the profile's own config as if it were the root —
    t_629a6842). A root that cannot be resolved is reported, not swallowed: this
    step used to be green on exactly the silence it is now required to break.
    """
    root, hermes_config, profiles_dir = hermes_paths()
    if not (root and hermes_config and profiles_dir):
        _, searched = resolve_hermes_root()
        print(c(R, f"  hermes: NO Hermes home resolved (searched: {searched}) "
                   f"— model.default NOT mirrored"))
        return False
    print(c(B, f"  hermes root: {root}"))
    ok = bool(set_yaml_model_default(hermes_config, model_id))
    if ok:
        print(c(G, "  hermes config.yaml: model.default updated"))
    else:
        print(c(Y, "  hermes config.yaml: model.default NOT found (skipped)"))
    missing = []
    for n in _profile_names(profiles_dir):
        p = os.path.join(profiles_dir, n, "config.yaml")
        if not os.path.exists(p):
            missing.append(n)
            continue
        if set_yaml_model_default(p, model_id):
            print(c(G, f"  profile {n}: model.default updated"))
        else:
            print(c(Y, f"  profile {n}: model.default NOT found (skipped)"))
    if missing:
        print(c(Y, f"  profiles without a config.yaml (skipped): {', '.join(missing)}"))
    return ok


# ── Phases ────────────────────────────────────────────────────────────────────

def phase_plan(topo, model_id, recipe):
    print(c(B, "── PLAN ──"))
    print(f"  gateway:  {topo['gateway']}  workers: {topo['workers']}  nas: {topo['nas_ip']}")
    print(f"  serving:  {topo['serving_nodes']}  port: {topo['port']}")
    note_parked(topo)
    rm = recipe_model(recipe)
    if rm is None:
        print(c(Y, f"  recipe:   {recipe} (MISSING — will scaffold on cache/all)"))
    elif rm != model_id:
        print(c(R, f"  recipe:   {recipe} model={rm} != {model_id} (MISMATCH)"))
    else:
        print(c(G, f"  recipe:   {recipe} model={rm} OK"))
    nref = nas_ref(topo, model_id)
    nfiles = nas_snapshot_files(topo, model_id, nref) if nref else 0
    if nref:
        print(c(G, f"  NAS:      cached ref={nref[:12]} ({nfiles} snapshot files)"))
    else:
        print(c(R, "  NAS:      MISSING (cache phase will need a download)"))
    for h in topo["serving_nodes"]:
        if h in topo["parked"]:
            # Not "needs sync": the node is parked, and the cache verdict is
            # deliberately not collected for it (no ssh onto a parked machine).
            print(c(Y, f"    {h}: PARKED — cache not probed, node not picked"))
            continue
        if nref and node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"    {h}: cache READY"))
        else:
            print(c(Y, f"    {h}: cache needs sync"))
    print(c(B, "  config changes wire would make:"))
    print("    serving.json units, models.json primary_model, provision.json mappings,")
    print("    agents.json (model part only), hermes ROOT config.yaml + every "
          "profile config.yaml model.default")
    print(c(B, "  launch is delegated to `hscc cluster up` (it owns the sparkrun "
              "argv; see phase_launch)"))
    return nref, nfiles


def phase_cache(topo, model_id, recipe):
    print(c(B, "── CACHE ──"))
    if recipe and recipe_model(recipe) is None:
        scaffold_recipe(recipe, model_id)
        die("recipe was missing; scaffolded a starter. Review it, then re-run.")
    note_parked(topo)
    nref = nas_ref(topo, model_id)
    if not nref:
        die(f"model not on NAS. Download first, e.g.:\n"
            f"  ssh {topo['ssh_user']}@{topo['nas_mount_host']} "
            f"'HF_HUB_OFFLINE=0 hf download {model_id} --cache-dir /mnt/nas/hub'")
    nfiles = nas_snapshot_files(topo, model_id, nref)
    print(c(G, f"  NAS ref={nref[:12]} ({nfiles} snapshot files)"))
    for h in topo["serving_nodes"]:
        if h in topo["parked"]:
            # Skipped, and said out loud. A parked machine is parked for a
            # reason the operator did not write into serving.json; pushing a
            # multi-gigabyte rsync at it is disturbing a node on purpose. The
            # cache is not lost either: `hscc node resume` restarts the recorded
            # units with --ensure, and re-running `cache` after the resume
            # fills it — resume does NOT depend on this pass having run.
            print(c(Y, f"  {h}: PARKED — rsync skipped (resume, then re-run cache)"))
            continue
        if node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"  {h}: already complete — skip"))
            continue
        print(c(Y, f"  {h}: rsync from NAS (additive)…"))
        ok, err = rsync_nas_to_node(topo, h, model_id)
        if not ok:
            print(c(R, f"  {h}: rsync FAILED: {err}"))
            continue
        if node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"  {h}: cache READY"))
        else:
            print(c(R, f"  {h}: still incomplete after rsync"))


def phase_wire(topo, model_id, recipe, yes):
    print(c(B, "── WIRE ──"))
    if recipe_model(recipe) != model_id:
        die(f"recipe {recipe} model != {model_id}; fix recipe before wiring")
    if not yes:
        die("wire mutates live HSCC config. Re-run with --yes to proceed.")
    backup_configs()
    wire_serving(model_id, recipe)
    wire_models(model_id)
    wire_provision(recipe)
    wire_agents(model_id)
    wire_hermes_and_profiles(model_id)
    print(c(G, "  wired. NOTE: gateway/daemon still hold old config until restarted."))


def _hscc_argv_prefix():
    """The argv prefix that reaches the unified ``hscc`` CLI, or None.

    ``HSCC_BIN`` names it explicitly (a test plants a fake there, an operator
    may have it off PATH); otherwise ``hscc`` on PATH. Returns a LIST so callers
    never build a command line through a shell. None = no door, and the caller
    must stop rather than improvise a sparkrun call: hand-rolling the launch is
    exactly what this phase used to do and what it can no longer do (see
    :func:`phase_launch`).
    """
    explicit = (os.environ.get("HSCC_BIN") or "").strip()
    if explicit:
        return [explicit]
    found = shutil.which("hscc")
    return [found] if found else None


def phase_launch(topo, recipe, yes):
    """Bring the serving layer up by calling ``hscc cluster down|up``.

    This phase used to build its own ``sparkrun run <recipe> --hosts <node>``
    per node. Two independent reasons that had to go:

    * **Intent parity (t_8ad06a53, t_e1ff0e8e).** ``sparkrun`` identifies a
      workload by an INTENT HASH of the recipe plus ``--port`` /
      ``--served-model-name`` / ``--tp`` — every flag the launch carried has to
      ride on any later ``sparkrun stop`` or it matches nothing and stops
      nothing while the caller logs a stop; and a launch that carries DIFFERENT
      flags from the fleet's declared unit makes ``--ensure`` a parallel
      duplicate instead of a re-issue. The only safe place to build that argv is
      ``hscc_daemon/serving.py`` (``_unit_run_cmd`` / ``_unit_intent`` /
      ``unit_stop_cmd``), reached through ``hscc cluster up``, which takes the
      unit's recipe, node span, port, model, role and tp from ``serving.json``.
      A skill script cannot reimplement that and keep agreeing with the daemon.
    * **Node pause.** ``cmd_cluster_up`` partitions its plan against
      ``node_pauses`` and skips a unit whose span has even one parked node
      (reported under the additive ``paused`` key; ``result: "all-paused"`` when
      everything is parked). A hand-rolled per-node loop has no such seam, so
      the old launch relaunched precisely the machine the operator parked.

    Down THEN up: ``wire`` has just rewritten every unit's ``recipe``/``model``,
    so the intent hash of a still-running OLD container no longer matches the
    declared unit — an ``--ensure`` up over it is the duplicate case above.
    ``hscc cluster down`` issues the one sanctioned ``sparkrun stop --all``
    (scoped to the cluster), which needs no intent flags at all and so cannot be
    defeated by the rewrite; a fleet that is already down is a SUCCESS for it,
    so this stays correct for a first bring-up.

    No ``--yes``, no mutation: the door's own ``--dry-run`` prints the exact
    plan (including which units it will skip as parked) and nothing is launched.
    """
    print(c(B, "── LAUNCH ──"))
    declared = {os.path.realpath(os.path.expanduser(u.get("recipe") or ""))
                for u in _serving_units()}
    if declared and os.path.realpath(os.path.expanduser(recipe)) not in declared:
        die(f"serving.json declares no unit with recipe {recipe}; wire "
            f"<model> <recipe> --yes first (launch uppers the DECLARED units, "
            f"not a recipe named on the side)")
    if not yes:
        print(c(Y, "  no --yes: previewing via `hscc cluster up --dry-run`, "
                   "nothing is stopped or started"))
        return _call_cluster(["cluster", "up", "--dry-run"], required=False)
    note_parked(topo)
    if not _call_cluster(["cluster", "down"]):
        return False
    return _call_cluster(["cluster", "up"])


def _serving_units():
    """The units ``serving.json`` declares, or [] when it cannot be read."""
    path = os.path.join(HSCC_HOME, "serving.json")
    try:
        with open(os.path.realpath(path)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    units = data.get("units") if isinstance(data, dict) else None
    return [u for u in units if isinstance(u, dict)] if isinstance(units, list) else []


def _call_cluster(args, required=True):
    """Call the sanctioned ``hscc cluster ...`` door and stream its own report.

    The door's stdout IS the report (unit ids, the parked skips, the per-unit
    launch results) — re-rendering it here would be a second renderer allowed to
    disagree with the first, so this only adds a phase-prefixed status line and
    returns the success verdict. ``required`` True means a missing door is a
    hard stop (a real up) and False means a warning (a dry-run preview is
    worthless, not dangerous).
    """
    prefix = _hscc_argv_prefix()
    if prefix is None:
        msg = ("the `hscc` CLI is not on PATH and HSCC_BIN is unset — launch "
               "refuses rather than hand-building a sparkrun argv (only "
               "hscc_daemon.serving may build one)")
        if required:
            die(msg)
        print(c(Y, f"  {msg}"))
        return False
    argv = prefix + args
    print(c(B, f"  $ {' '.join(argv)}"))
    try:
        r = subprocess.run(argv, timeout=1800)
    except subprocess.TimeoutExpired:
        print(c(R, f"  {' '.join(argv)} timed out (30m) — the fleet may be "
                   f"mid-transition; re-run 'hscc cluster status'"))
        return False
    except OSError as e:
        print(c(R, f"  could not run {' '.join(argv)}: {e}"))
        return False
    if r.returncode != 0:
        print(c(R, f"  {' '.join(argv)} exited {r.returncode}"))
        return False
    return True


def _health(host, port, timeout=5):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _completion(host, port, model_id, timeout=40):
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: online"}],
        "max_tokens": 16, "temperature": 0,
    }).encode()
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"].strip()[:60]
    except Exception as e:
        return f"ERR {e}"


def phase_verify(topo, model_id, wait_secs=0):
    """Health + a completion per serving node, with PARKED read as absent.

    A parked node is deliberately running nothing, so its port not answering is
    the CORRECT state, not an outage. The proxy seam already reads it that way
    (``health._proxy_backend_drift_sets`` returns ``expected_absent`` and drops
    those hosts from both drift directions before any repair is decided, so a
    pause never triggers a ``sparkrun proxy stop``). verify has to agree with
    that or the two surfaces disagree on the same tick: the operator sees a red
    DOWN for a node they parked themselves, and learns to distrust the report.

    So a parked host gets one PAUSED line, no probe, no wait, and no failure
    credit either way — and the count of them is stated so an all-paused fleet
    cannot read as a clean pass.
    """
    print(c(B, "── VERIFY ──"))
    port = topo["port"]
    deadline = time.time() + wait_secs
    parked = topo.get("parked") or set()
    checked = 0
    for h in topo["serving_nodes"]:
        if h in parked:
            print(c(Y, f"  {h}: PAUSED — expected-absent, not probed "
                       f"(this is the pause working; 'hscc node resume {h}' to "
                       f"bring it back)"))
            continue
        checked += 1
        ok = _health(h, port)
        while not ok and time.time() < deadline:
            time.sleep(15)
            ok = _health(h, port)
        if not ok:
            print(c(R, f"  {h}: DOWN"))
            continue
        comp = _completion(h, port, model_id)
        bad = comp.startswith("ERR")
        print(f"  {h}: {c(G,'health 200')} | completion: {c(R if bad else G, comp)}")
    if parked and not checked:
        print(c(Y, "  every declared serving node is parked — nothing was "
                   "verified, which is not a pass"))
    elif parked:
        print(c(Y, f"  {len(parked)} node(s) parked and excluded from this verdict"))


# ── CLI ───────────────────────────────────────────────────────────────────────

def usage():
    print(__doc__)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1]
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    yes = "--yes" in sys.argv
    topo = load_topology()

    if cmd == "plan":
        if len(args) < 2:
            die("plan <model> <recipe>")
        phase_plan(topo, args[0], os.path.expanduser(args[1]))
    elif cmd == "cache":
        if not args:
            die("cache <model> [recipe]")
        recipe = os.path.expanduser(args[1]) if len(args) > 1 else None
        phase_cache(topo, args[0], recipe)
    elif cmd == "wire":
        if len(args) < 2:
            die("wire <model> <recipe> --yes")
        phase_wire(topo, args[0], os.path.expanduser(args[1]), yes)
    elif cmd == "launch":
        if not args:
            die("launch <recipe> --yes")
        phase_launch(topo, os.path.expanduser(args[0]), yes)
    elif cmd == "verify":
        if not args:
            die("verify <model>")
        wait = 300 if "--wait" in sys.argv else 0
        phase_verify(topo, args[0], wait)
    elif cmd == "all":
        if len(args) < 2:
            die("all <model> <recipe> --yes")
        model, recipe = args[0], os.path.expanduser(args[1])
        phase_plan(topo, model, recipe)
        phase_cache(topo, model, recipe)
        phase_wire(topo, model, recipe, yes)
        phase_launch(topo, recipe, yes)
        phase_verify(topo, model, wait_secs=420)
    else:
        usage()


if __name__ == "__main__":
    main()
