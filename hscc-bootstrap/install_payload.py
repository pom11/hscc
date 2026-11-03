"""Copy HSCC plugin files from the work repo into the Hermes runtime dir.

The repo is the source of truth; bootstrap installs a copy into
``~/.hermes/plugins/`` so the git checkout and the runtime dir stay separate
(edit in the repo, run bootstrap, runtime updates — a real user install).

Backup-then-overwrite: an existing runtime ``<dir>`` is moved aside to a
sibling ``plugins-backups/<dir>.bak-<ts>`` (never inside the scanned
plugins_dir) before the fresh copy lands. Build artifacts and tests are
not shipped to the runtime dir.

Operator-drift guard (GAP-E-1, audit E t_f7d94a76): before the tree moves,
every deployed file whose content differs from the incoming payload (or that
exists only on the deployed side) is MOVED into
``plugins-backups/<dir>.drifted.bak-<ts>/`` and reported in the summary's
``drifted`` key — operator-modified plugin files must never be silently
overwritten at bootstrap. Files are never auto-merged: the payload wins the
live path, the operator copy survives in plugins-backups, and the drift is
flagged. An operator who has decided the payload should simply win passes
``force=True`` (``--force``), which skips detection and overwrites as before.
The generalises the preserve precedent that previously covered only
``autodown.json`` (bootstrap.sh + preserve_autodown.py).
"""

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

try:
    import backup_util
except ImportError:  # pragma: no cover - import-path robustness only
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backup_util

# Never ship these into the runtime dir.
_EXCLUDE = {"__pycache__", ".pytest_cache", "tests", ".git"}

# Extra deploy destinations for payload entries whose repo staging path is NOT
# a location Hermes' plugin scanner can discover. Hermes scans each plugins dir
# for manifests at two depths only (hermes_cli.plugins_discovery.scan_directory
# recurses ONE category level below the root), so a provider plugin staged at
# install/hscc-plugins/video_gen/<name>/ would sit four levels under
# ~/.hermes/plugins and never load. The extra dest puts a second, identical
# copy at the category level <plugins>/video_gen/<name>/, which the scanner
# keys "video_gen/openai_compat" — the exact key plugins.enabled must carry
# (user-source plugins are opt-in; bundled auto-load does not apply).
# Additive, not a redirect: the staging path still deploys under
# plugins_dir/<staging-name>, because hscc_daemon/verify.py::
# check_plugin_payload compares every payload name at its same-named deployed
# location — a redirect there would flag permanent phantom "not deployed"
# drift. Keys: repo-relative staging path; values: tuple of
# plugins_dir-relative extra destinations.
PAYLOAD_EXTRA_DESTS = {
    "install/hscc-plugins/video_gen/openai_compat": ("video_gen/openai_compat",),
}


def _ignore(_dir, names):
    """shutil.copytree ignore: drop caches/tests/pyc from the runtime copy."""
    drop = {n for n in names if n in _EXCLUDE or n.endswith(".pyc")}
    return drop


def _backups_dir(plugins_dir):
    """Return the sibling backup directory next to ``plugins_dir``."""
    return plugins_dir.parent / "plugins-backups"


def _sweep_old_backups(plugins_dir, backups_dir):
    """Move any pre-existing ``<name>.bak-<ts>`` entries out of ``plugins_dir``.

    Best-effort — never raises; silently skips anything that fails.
    """
    try:
        candidates = [e for e in plugins_dir.iterdir() if ".bak-" in e.name]
    except OSError:
        return
    if not candidates:
        return
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for entry in candidates:
        try:
            shutil.move(str(entry), str(backups_dir / entry.name))
        except OSError:
            pass


# ── operator-drift guard (GAP-E-1, audit E t_f7d94a76) ───────────────────────
# The copy below moves the whole deployed tree to plugins-backups and lands a
# fresh payload over it. Before this guard the ONLY filter was _EXCLUDE, so a
# file the operator patched directly in ~/.hermes/plugins/ (the exact scenario
# audit E flagged for hscc.py / kanban_cli.py / kanban_blocked.py) was destroyed
# with zero signal. The precedent (bootstrap.sh + preserve_autodown.py, which
# protected autodown.json alone) is now generalised: detect deployed files that
# differ from the incoming payload by content hash, keep the operator's copy in
# plugins-backups, flag every one of them in the summary. NEVER auto-merge —
# the payload always wins the live path; the operator's version survives only
# as a preserved, named backup until the operator decides otherwise.

#: Sentinel returned by ``_detect_entry_drift`` when the deployed entry and the
#: incoming payload entry are different *shapes* (dir vs file, or a symlink) —
#: the whole old entry is destroyed, so the whole old entry is preserved.
_WHOLE_ENTRY = "*"

#: Suffix shape for the per-install collection of drifted files:
#: ``<label>.drifted.bak-<stamp>``. The ``.drifted`` marker sits BEFORE the
#: ``.bak-`` infix so backup_util groups it as its OWN family
#: (``<label>.drifted``) instead of sharing the whole-tree backup's retain
#: window — persistent drift then cannot halve the whole-tree backup history,
#: and the drift evidence keeps its own keep-N window (still machine-stamped,
#: so the pile stays self-limiting, t_9462260b).
_DRIFTED_MARK = ".drifted"


def _sha_file(path):
    """sha256 of a file's bytes; ``None`` if it cannot be read.

    An unreadable file is treated as drifted (fail-safe: preserve-and-flag,
    never silently overwrite something we could not even verify).
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _iter_shipped_files(root):
    """Relative paths of every file the copy would ship under ``root``.

    Mirrors ``_ignore`` (the copytree filter) exactly — the drift comparison
    and the copy must never disagree about what "covered by the payload"
    means, or caches would surface as phantom drift. Only regular files: a
    symlinked dir is not descended (os.walk's default), matching how caches/
    pyc noise is excluded; the fleet payload ships no symlinked dirs.
    """
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE)
        rel_dir = Path(dirpath).relative_to(base)
        for fn in sorted(filenames):
            if fn.endswith(".pyc"):
                continue
            rel = Path(fn) if str(rel_dir) == "." else rel_dir / fn
            yield rel.as_posix()


def _detect_entry_drift(src, dst):
    """Posix paths (relative to ``dst``) the copy would silently destroy.

    A deployed file is *drifted* when its content differs from the incoming
    payload file (hash compare) or it exists only on the deployed side. An
    empty list means the deployed tree is identical to what is about to be
    copied — that install overwrites silently (acceptance: a clean tree must
    not cry drift). Caller passes dirs/files as they will be copied.
    """
    if not (dst.exists() or dst.is_symlink()):
        return []
    if src.is_dir() and dst.is_dir() and not dst.is_symlink():
        incoming = {rel: _sha_file(src / rel)
                    for rel in _iter_shipped_files(src)}
        drifted = []
        for rel in _iter_shipped_files(dst):
            if rel not in incoming or _sha_file(dst / rel) != incoming[rel]:
                drifted.append(rel)
        return drifted
    if src.is_file() and dst.is_file() and not dst.is_symlink():
        return [] if _sha_file(src) == _sha_file(dst) else [_WHOLE_ENTRY]
    # dir vs file, or either side is a symlink: nothing to compare per-file;
    # the entire old entry is replaced, so the entire old entry is drifted.
    return [_WHOLE_ENTRY]


def _preserve_drifted(label, stamp, drifted, bak, bak_dir):
    """Copy the drifted deployed files out of the whole-tree backup ``bak``.

    They already live inside ``bak`` (the whole-tree move ran first), so this
    is a pure *additional* collection:
    ``plugins-backups/<label>.drifted.bak-<stamp>/`` mirrors the deployed
    layout one-for-one. Additive by design — the whole-tree backup stays
    byte-identical to what it was before this guard existed.

    Never raises: a file that cannot be preserved is still reported
    (``preserved: False``) so the operator learns it was destroyed anyway.
    """
    dest = bak_dir / f"{label.replace('/', '__')}{_DRIFTED_MARK}.bak-{stamp}"
    records = []
    for rel in drifted:
        rec = {"path": f"{label}/{rel}" if rel != _WHOLE_ENTRY else label,
               "backup": dest.name, "preserved": False}
        try:
            dest.mkdir(parents=True, exist_ok=True)
            if rel == _WHOLE_ENTRY:
                if Path(bak).is_dir():
                    shutil.copytree(bak, str(dest), symlinks=False)
                else:
                    shutil.copy2(bak, dest / Path(label).name)
            else:
                src_in_bak = Path(bak) / rel
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_in_bak, target)
            rec["preserved"] = True
        except (OSError, shutil.Error):
            pass  # flagged with preserved=False below — never fail the install
        records.append(rec)
    return records


def install_payload(repo_root, plugins_dir, payload, *, backup=True, ts=None,
                    force=False):
    """Copy each ``payload`` entry from ``repo_root`` into ``plugins_dir``.

    ``payload`` is a list of names (dirs or files) relative to the repo root.
    Entries listed in ``PAYLOAD_EXTRA_DESTS`` are ALSO copied to extra
    plugins_dir-relative destinations (a staging path that Hermes' plugin
    scanner cannot discover needs a second copy at a scannable location — see
    the PAYLOAD_EXTRA_DESTS comment). Returns a summary dict. If ``repo_root``
    and ``plugins_dir`` resolve to the same directory (the old in-place layout,
    or a clone into the runtime dir), the copy is skipped — there is nothing to
    install.

    Backups are written to the sibling ``plugins-backups/`` directory (never
    inside the scanned ``plugins_dir``) so stale copies can never shadow
    freshly-installed plugins.

    Drift guard (GAP-E-1): unless ``force`` is set, deployed files that differ
    from the incoming payload are detected by hash, preserved into
    ``plugins-backups/<entry>.drifted.bak-<ts>/`` and reported under the
    summary's ``drifted`` key. ``force=True`` skips detection entirely — the
    operator's explicit opt-in to "the payload wins, no ceremony". A clean
    (identical) deployed tree is never reported as drift, so the normal
    reinstall path stays silent.
    """
    repo_root = Path(repo_root).expanduser().resolve()
    plugins_dir = Path(plugins_dir).expanduser().resolve()

    if repo_root == plugins_dir:
        return {
            "skipped": True,
            "reason": "repo_root == plugins_dir (in-place layout); nothing to copy",
            "installed": [], "backed_up": [], "missing": [], "drifted": [],
            "drift_detected": False,
        }

    stamp = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Sweep stale .bak-* entries out of the scanned plugins_dir (best effort).
    _sweep_old_backups(plugins_dir, _backups_dir(plugins_dir))

    installed, backed_up, missing, drifted = [], [], [], []

    def _copy_into(src, dst, label):
        """One backup-then-overwrite copy (dir or file) into ``dst``."""
        if dst.exists() or dst.is_symlink():
            # Detect operator drift BEFORE the tree moves — after the move the
            # deployed files are no longer at dst. force=True is the operator's
            # opt-in to skip detection and let the payload overwrite silently.
            entry_drift = [] if force else _detect_entry_drift(src, dst)
            if backup:
                bak_dir = _backups_dir(plugins_dir)
                bak_dir.mkdir(parents=True, exist_ok=True)
                # Flatten path separators so nested dests (video_gen/openai_compat)
                # back up as ONE name directly inside bak_dir (the sweep +
                # prune only see direct children of bak_dir).
                bak = bak_dir / f"{label.replace('/', '__')}.bak-{stamp}"
                shutil.move(str(dst), str(bak))
                backed_up.append(bak.name)
                if entry_drift:
                    # Additional, explicitly-named preservation of exactly the
                    # files that differ (see _preserve_drifted); the whole-tree
                    # backup above is left byte-identical to its pre-guard form.
                    drifted.extend(
                        _preserve_drifted(label, stamp,
                                          entry_drift, bak, bak_dir))
                # Retain-limit the pile this stage creates (t_9462260b):
                # plugins-backups/ grew to 3,385 entries / 1.2 GB here.
                # Default stamped_only keeps operator-labelled bookmarks alive.
                backup_util.prune_backup_dir(bak_dir)
            elif entry_drift:
                # backup=False AND drift: the old files are about to be gone
                # with NO backup anywhere. Still detected, still flagged —
                # preserved=False records the destruction honestly.
                drifted.extend(
                    {"path": f"{label}/{r}" if r != _WHOLE_ENTRY else label,
                     "backup": None, "preserved": False}
                    for r in entry_drift)
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            elif dst.exists() or dst.is_symlink():
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore, symlinks=False)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        installed.append(label)

    for name in payload:
        src = repo_root / name
        if not src.exists():
            missing.append(name)
            continue
        _copy_into(src, plugins_dir / name, name)
        # Extra destinations for entries whose staging path is not scannable
        # by Hermes' plugin discovery (see PAYLOAD_EXTRA_DESTS).
        for extra in PAYLOAD_EXTRA_DESTS.get(name, ()):
            _copy_into(src, plugins_dir / extra, extra)

    return {
        "skipped": False,
        "installed": installed,
        "backed_up": backed_up,
        "missing": missing,
        # GAP-E-1: every deployed file that differed from the payload and was
        # therefore preserved (or destroyed) by this install. Empty list =
        # nothing was silently overwritten.
        "drifted": drifted,
        "drift_detected": bool(drifted),
        "plugins_dir": str(plugins_dir),
    }


# ── target-profile selection (t_32f97d20, operator decision option 3) ────────
# install_payload_profiles used to write into every profile home that ALREADY
# had a plugins/ dir — on this fleet that is exactly 2 of 49 (measurement doc
# docs/t_16eb73ab-profile-target-measurement.md), so 47 homes silently never
# received the payload and the t_5720ef53 doctor budget-hook row reads red for
# them. The operator decision (comment 1642 on t_16eb73ab) is an explicit
# allowlist seeded narrowest, NOT a write-to-everything default:
#
#   ~/.hscc/target_profiles.json -> {"target_profiles": ["backend-engineer", ...]}
#
# A DEDICATED file, deliberately not a key inside cluster.json:
# hscc-cluster/discovery.py::_write_cache rewrites cluster.json from a freshly
# built dict (to_cluster_json: name/gateway/workers/nasDevices only) on every
# live discovery, so any hand-added top-level key there silently evaporates —
# the operator's widening edit must not be one `hscc cluster` run away from
# vanishing (same silent-signal-loss class as the node_recovery opt-out drops,
# filed as t_7c3ec484).
#
# Semantics (pinned by tests named for each rule):
#   file ABSENT / key ABSENT -> DEFAULT_TARGET_PROFILES (the hscc-board seed)
#   ["*", ...]  -> EVERY profile home under profiles/ (option 1; the key must
#                  keep this meaning so widening stays a config edit, no code)
#   [...]       -> exactly those profile names; missing plugins/ is CREATED
#   []          -> legacy behaviour: only homes that already have plugins/
# The seed is the DEFAULT, never a wall: an operator adds or drops names by
# editing the config file (same reader convention as install_notifier_mask:
# one key, one reader, hscc_dir= seam so tests can seal ~/.hscc).
DEFAULT_TARGET_PROFILES = (
    # Derived 2026-11-03 from the hscc BOARD's own task history via the
    # sanctioned CLI — see docs/t_32f97d20-target-profiles-seed.md for the
    # exact commands and the caveat that made the board-scoped reading work:
    #   hermes kanban --board hscc stats --json   (board roster, non-archived)
    #   hermes kanban --board hscc assignees      (on_disk filter, drops ghosts)
    "architect", "backend-engineer", "coder", "default", "devops-engineer",
    "ecofire-bc-engineer", "ecofire-bc-orch", "hscc-orch", "ios-engineer",
    "qa", "reviewer", "technical-writer", "worker",
)

#: The literal that widens the allowlist to every profile home (option 1).
TARGET_ALL = "*"

#: The config key inside target_profiles.json that carries the allowlist.
TARGET_PROFILES_KEY = "target_profiles"


def hscc_dir() -> str:
    """HSCC state dir, env-resolved (never a baked ``~/.hscc``). Same shape as
    ``install_notifier_mask.hscc_dir`` / ``install_gpu_clock_cap.hscc_dir``."""
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def target_profiles_config(hscc_dir=None):
    """Read ``target_profiles`` from ``<hscc_dir>/target_profiles.json``.

    Returns ``{"target_profiles": <resolved>, "config_error": str}`` where the
    resolved value is one of:

    * ``None``            — legacy selection (only homes that already have a
                            ``plugins/`` dir). Returned for an explicit ``[]``.
    * ``TARGET_ALL``      — every profile home (the ``["*", ...]`` spelling).
    * ``tuple[str, ...]`` — the allowlist of profile names.

    Missing file / missing key -> ``DEFAULT_TARGET_PROFILES``. A value that is
    not a list of strings keeps the default and is NAMED in ``config_error``:
    a silently-ignored allowlist looks exactly like a working one (same rule
    install_notifier_mask applies to its own subkey). This reader owns only its
    own file — it cannot flip a sibling key in cluster.json and no sibling
    reader can flip it.
    """
    base = hscc_dir or globals()["hscc_dir"]()
    target = os.path.join(base, "target_profiles.json")
    try:
        with open(target) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return {"target_profiles": tuple(DEFAULT_TARGET_PROFILES),
                "config_error": ""}
    except (json.JSONDecodeError, OSError, IsADirectoryError) as e:
        return {"target_profiles": tuple(DEFAULT_TARGET_PROFILES),
                "config_error": (f"could not read {target} "
                                 f"({e.__class__.__name__}: {e}) — default "
                                 f"target_profiles seed applied")}
    if not isinstance(cfg, dict) or TARGET_PROFILES_KEY not in cfg:
        return {"target_profiles": tuple(DEFAULT_TARGET_PROFILES),
                "config_error": ""}
    raw = cfg[TARGET_PROFILES_KEY]
    if raw == []:
        return {"target_profiles": None, "config_error": ""}
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        return {"target_profiles": tuple(DEFAULT_TARGET_PROFILES),
                "config_error": (f"{target}:{TARGET_PROFILES_KEY} must be a "
                                 f"list of profile-name strings (or "
                                 f'["*"] for all); got {raw!r} — default '
                                 f"target_profiles seed applied")}
    if TARGET_ALL in raw:
        return {"target_profiles": TARGET_ALL, "config_error": ""}
    return {"target_profiles": tuple(sorted(set(raw))), "config_error": ""}


def _selected_profile_plugin_dirs(plugins_dir, targets):
    """Plugin dirs of the profile homes *targets* selects, with creation flags.

    ``targets``: ``None`` (legacy: only homes that already have ``plugins/``),
    ``TARGET_ALL`` (every profile home; ``plugins/`` is created when absent),
    or an iterable of profile names (only those homes; ``plugins/`` created
    when absent). Returns a list of ``{"profile", "plugins_dir", "created"}``
    dicts sorted by profile name — ``created`` records whether this call is
    the one that first materialised the home's ``plugins/`` dir, which is
    exactly the "flipped from payload-less to payload" census the bootstrap
    diff needs (t_16eb73ab acceptance criterion).

    A profile NAME that has no home dir on disk is never fabricated here:
    selection iterates the homes that exist (ghosts have no home to write to —
    phase-1 measurement, set B minus A).
    """
    plugins_dir = Path(plugins_dir).expanduser().resolve()
    if plugins_dir.name != "plugins":
        return []  # not a root layout — cannot derive a profiles sibling
    profiles_dir = plugins_dir.parent / "profiles"
    if not profiles_dir.is_dir():
        return []
    allow_all = targets == TARGET_ALL
    allow = None
    if targets is not None and not allow_all:
        allow = set(targets)
    out = []
    try:
        for child in sorted(profiles_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if allow is not None and not allow_all and child.name not in allow:
                continue
            pdir = child / "plugins"
            existed = pdir.is_dir()
            if not existed and targets is None:
                continue  # legacy gate: unlisted homes never materialise
            out.append({"profile": child.name,
                        "plugins_dir": pdir,
                        "created": not existed})
    except OSError:
        return []
    return out


def _named_profile_plugins_dirs(plugins_dir, target_profiles=None):
    """Plugin dirs of named profiles under the same home as ``plugins_dir``
    (``<root>/profiles/<name>/plugins``), legacy selection only.

    Each Hermes profile loads its plugins and memory provider from its OWN
    ``$HERMES_HOME/plugins`` (``get_hermes_home()/plugins``), NOT the root
    ``~/.hermes/plugins``. So a payload installed only into the root home never
    reaches the running profile sessions — the exact failure that kept the
    memori_byodb fix (t_57e5b3f7) from ever taking effect for the orchestrator
    profile, which still ran the OLD buggy ``local_augmentation.py``.

    Returns [] when ``plugins_dir`` is itself profile-scoped (already inside a
    ``<root>/profiles/<name>``) or when no profiles exist, so the call is a
    no-op in those cases.

    ``target_profiles`` (t_32f97d20) is the allowlist resolution
    (``None`` legacy / ``TARGET_ALL`` / names); the default ``None`` keeps the
    historical behaviour byte-for-byte for external callers — only
    ``install_payload_profiles`` passes a resolved selection.
    """
    return [e["plugins_dir"] for e in
            _selected_profile_plugin_dirs(plugins_dir, target_profiles)]


#: Sentinel: "no explicit target_profiles passed — resolve from config".
#: A plain object() so type checkers don't read ``...`` as EllipsisType.
_UNSET = object()


def install_payload_profiles(
    repo_root, plugins_dir, payload, *, backup=True, ts=None, force=False,
    target_profiles=_UNSET, hscc_dir=None,
):
    """Install ``payload`` into the root home AND the selected named profiles'
    plugin dirs under the same home, so the runtime code is current for every
    profile that loads it. Returns the primary summary with a ``profiles``
    child list (one summary per profile plugin dir; a failing profile is
    recorded, not fatal). Each profile summary carries ``profile`` (name) and
    ``created`` (True when this install materialised that home's ``plugins/``
    dir for the first time — the payload-less-to-payload flip).

    ``target_profiles`` (t_32f97d20): left unset resolves the allowlist from
    ``<hscc_dir>/target_profiles.json`` ``target_profiles`` — see
    :func:`target_profiles_config`; an explicit value (``None`` or ``[]`` for
    legacy, ``"*"`` / ``["*"]`` for all homes, or a collection of names)
    overrides the config.
    A malformed config keeps the default seed and is reported under the
    primary summary's ``target_profiles_error`` instead of silently ignored.
    A selected profile whose ``plugins/`` dir is missing gets it created
    (``install_payload`` mkdirs at the copy stage); an unselected home is not
    written at all.

    The GAP-E-1 drift guard runs per destination: every profile plugin dir gets
    its own drift detection + preserve-to-backup (an operator patch inside
    ``profiles/<p>/plugins`` was previously overwritten with zero signal,
    GAP-E-4's second half). ``force`` applies to every destination.
    ``drift_detected`` on the returned primary summary is True when ANY
    destination (primary or profile) flagged drift, so a caller that only
    reads the primary summary still sees profile drift.
    """
    target_error = ""
    if target_profiles is _UNSET:
        resolved = target_profiles_config(hscc_dir=hscc_dir)
        targets = resolved["target_profiles"]
        target_error = resolved["config_error"]
    elif target_profiles is None:
        targets = None
    elif isinstance(target_profiles, str):
        # "*" (or a lone name) as a bare string.
        targets = TARGET_ALL if target_profiles == TARGET_ALL else (target_profiles,)
    elif isinstance(target_profiles, (list, tuple, set, frozenset)):
        if len(target_profiles) == 0:
            # [] == legacy, same as the config spelling for it (empty allowlist).
            targets = None
        elif TARGET_ALL in target_profiles:
            targets = TARGET_ALL
        else:
            targets = tuple(target_profiles)
    else:
        raise TypeError(
            "target_profiles must be None, '*', or a collection of profile-"
            f"name strings; got {type(target_profiles).__name__}")
    primary = install_payload(repo_root, plugins_dir, payload,
                              backup=backup, ts=ts, force=force)
    profile_results = []
    for sel in _selected_profile_plugin_dirs(plugins_dir, targets):
        p_dir = sel["plugins_dir"]
        try:
            summary = install_payload(repo_root, p_dir, payload,
                                      backup=backup, ts=ts, force=force)
        except Exception as exc:  # a bad profile must never sink the whole deploy
            summary = {"plugins_dir": str(p_dir),
                       "error": f"{type(exc).__name__}: {exc}"}
        summary["profile"] = sel["profile"]
        summary["created"] = sel["created"]
        profile_results.append(summary)
    primary["profiles"] = profile_results
    if target_error:
        primary["target_profiles_error"] = target_error
    if any(r.get("drift_detected") for r in profile_results):
        primary["drift_detected"] = True
    return primary


# Default payload: everything the runtime needs (scope B — full sibling layout
# so install/ walk-ups and SCRIPT_DIR resolution keep working).
DEFAULT_PAYLOAD = [
    "hscc-cluster", "hscc-commands", "hscc-roles", "hscc-skills",
    "hscc_daemon", "hscc-bootstrap", "sparkrun-hermes",
    # hscc-project is the relocated flightdeck; `hscc project ...` (in
    # hscc_daemon/hscc.py:_handle_project) imports it as a SIBLING of
    # hscc_daemon, so it must deploy alongside it or the verb breaks.
    "hscc-project",
    # hscc-api ships the `hscc api ...` HTTP server (hscc_daemon/hscc.py:
    # _handle_api). Its server imports the cluster/project plugins as siblings
    # at runtime, so it must deploy alongside them or the installed `hscc api`
    # verb fails with 'package not found' — the same gap that bit hscc-project.
    "hscc-api",
    "memori", "memori_byodb",
    "install", "docs", "assets", "configs",
    # Hermes provider plugins the fleet ships (media enablement, t_ec488ce3).
    # Staged under install/ alongside the bundled skills; PAYLOAD_EXTRA_DESTS
    # additionally deploys it at the category level
    # <plugins>/video_gen/openai_compat/ so Hermes' one-category-level scan
    # discovers it under key "video_gen/openai_compat" — the key
    # plugins.enabled must carry.
    "install/hscc-plugins/video_gen/openai_compat",
    "README.md", "CHANGELOG.md", "LICENSE", "requirements.txt", "VERSION",
]


if __name__ == "__main__":
    import json
    import sys

    boot_dir = Path(__file__).resolve().parent
    repo_root = os.environ.get("REPO_ROOT") or str(boot_dir.parent)
    plugins_dir = os.environ.get("PLUGINS") or os.path.expanduser("~/.hermes/plugins")
    no_backup = "--no-backup" in sys.argv
    # Operator opt-in: overwrite drifted deployed files without the
    # preserve-and-flag ceremony (mirrors bootstrap.sh --force).
    force = "--force" in sys.argv

    result = install_payload_profiles(repo_root, plugins_dir, DEFAULT_PAYLOAD,
                                      backup=not no_backup, force=force)
    print(json.dumps(result, indent=2))
    # Non-zero exit when drift was flagged and not forced: bootstrap turns this
    # into a loud operator warning without failing the install (the copy and
    # the fresh payload already succeeded).
    if result.get("drift_detected"):
        sys.exit(3)
