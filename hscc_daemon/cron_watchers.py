"""Liveness stamps + staleness verdicts for the Hermes-cron watchers.

Audit B / GAP-B-4: the watchers that live OUTSIDE the daemon --
``scripts/dep_pr_watcher.py`` (Hermes cron job ``hscc-dep-watcher``, daily),
``scripts/escalate_watcher_run.py`` (Hermes cron job ``hscc-escalate-watcher``,
*/15) and ``scripts/alone_sweep_gate.sh`` (Hermes cron job
``hscc-alone-sweep-gate``, nightly) -- leave no trace when they run and no gap
at all when they stop running. All are ``--no-agent`` jobs whose only record is
Hermes' own run history (plus files in a report dir nobody reads by default), so
a job that stops firing is discovered only when something downstream goes
unverified for days. That exact failure already happened once: the dep-watcher
failed 17 consecutive daily runs before anyone looked (t_3aadd2c0, recorded in
``scripts/install_dep_watcher.sh``).

Two meanings, kept apart on purpose (t_13a70290): every row is a **liveness**
row -- "did this job fire?" -- and the verdict is only what the stamp *carries*.
A watcher that produces a verdict (the alone-sweep gate prints GREEN / RED / NOT
MEASURED) stamps through :func:`stamp_failure` when its pass was not clean, so
the row goes ``failing`` (warn) instead of ``ok``: a red sweep can never read as
"watcher alive and well". ``stale`` / ``never-ran`` still mean "the job is not
firing", which is a different sentence and prints differently.

Fix shape deliberately copied from the daemon streams, not invented:

  * Every pass calls :func:`stamp`, which stamps through the daemon's OWN
    ``state.write_state()`` -- same files under ``~/.hscc/state``, same
    ``timestamp`` field, same atomic tmp+replace, same ``persist_disabled()``
    honour. There is exactly ONE liveness stamp format on this host.
  * The stamp moves on EVERY pass -- idle passes included. "Ran, nothing to do"
    must advance it, or a healthy idle watcher is indistinguishable from a dead
    one. A pass whose upstream probe failed still stamps (ok=True) with the
    failure in the payload: the pass itself happened.
  * Verdicts are WARN-shaped, mirroring the repo's verify warn convention
    (verify.py ``check_proxy_router_settings``: ``ok: True`` + a ``WARN:``-
    prefixed detail). A dead watcher is a loud yellow line, not a RED check.

The watchers are NOT moved into the daemon here -- that is a separate decision
(audit B left it open). This module only makes their silence observable.

Cadence source of truth lives HERE (``WATCHERS``), mirroring the installer
defaults. A watcher whose schedule is customised via env
(``HSCC_DEP_WATCHER_SCHEDULE`` / ``HSCC_ESCALATE_SCHEDULE`` /
``HSCC_ALONE_SWEEP_SCHEDULE``) should point the staleness window at the real
cadence with ``HSCC_DEP_WATCHER_CADENCE_S`` / ``HSCC_ESCALATE_CADENCE_S`` /
``HSCC_ALONE_SWEEP_CADENCE_S`` (seconds) so a custom schedule is not judged
against the installer default.

Stdlib-only (same rule as the daemon's ``state.py``, which this module loads
standalone by path when the package import fails -- ``scripts/`` is not a
deployed plugin package, and dep_pr_watcher.py deliberately imports nothing
from ``hscc_daemon`` at module scope).

Test seams: every read accepts an explicit ``directory`` (the stamp dir; the
install marker lives one level above it), threaded through as a parameter
rather than resolved from a mutated environment -- see :func:`state_dir` and
:func:`doctor_check`.
"""

import datetime
import hashlib
import importlib.util
import json
import os

# Staleness = this many missed passes before a watcher is declared stale.
# >= 3x cadence per GAP-B-4: one missed pass is normal (a slow tick, a laptop
# asleep across the 08:00 mark), so the window has to absorb it; two misses
# already read stale at 3x.
MISS_FACTOR = 3

# Each watcher's SOT row. ``stream`` is the stamp file name under
# ~/.hscc/state (and the verify-stream exclusion key), ``cron_job`` the Hermes
# cron job name the installer registers, ``cadence_s`` the installer default
# schedule in seconds, ``cadence_env`` the env override for the cadence (see
# the module docstring), ``install_script`` how to (re)install it.
#
# WHAT EACH ROW MEANS, per watcher, because the third one is different from the
# first two:
#   dep_pr_watcher / escalate_watcher -- pure liveness. Their verdict is their
#   own work product (cards created, escalations acted on) and lives elsewhere;
#   the row only says the job fired.
#   alone_sweep_gate -- liveness, WITH the gate's verdict riding in the stamp.
#   The gate's whole point is a verdict (GREEN / RED / NOT MEASURED) that
#   otherwise lives in a report dir nothing reads, so a pass that was not clean
#   stamps ok=False (:func:`stamp_failure`) and the row reads ``failing``, not
#   ``ok``. Reading the row: ``stale`` / ``never-ran`` = the nightly has stopped
#   firing; ``failing`` = it fired and the sweep said RED (or could not
#   measure). Both warn; neither is fatal -- a red sweep is a finding about the
#   test suite, not a broken host.
#
# ``deploy`` says how the code reaches <bound home>/scripts (t_95eb9f15), and as
# of t_accbc5da all three streams use the second shape:
#   ``wrapper`` the installer generates a stub that runs the repo's file
#               (install_alone_sweep_gate.sh's shape; install_dep_watcher.sh and
#               install_escalate_watcher.sh now generate the same kind of stub).
#               There is no second copy, so there is nothing to drift: the code
#               the cron job runs is always the code in git.
#   ``copy``    LEGACY. The installer ``cp``s the repo file next to the cron
#               binding, which leaves a SECOND copy of the code on the host --
#               the shape that let a live, healthy ``hscc-dep-watcher`` report
#               ``not configured`` (t_95eb9f15). No shipping installer does this
#               any more, and this module must keep handling it, because
#               "the stream is a wrapper in git" says nothing about a host that
#               has not re-run its installer: the stale file is still sitting
#               under <bound home>/scripts and its cron job still runs it. That
#               is why :func:`deployed_copy_drift` compares a leftover copy for
#               a ``wrapper`` stream instead of shrugging.
DEPLOY_COPY = "copy"
DEPLOY_WRAPPER = "wrapper"

WATCHERS = {
    "dep_pr_watcher": {
        "cron_job": "hscc-dep-watcher",
        # ``script`` is the repo's file AND (since t_accbc5da) the path the
        # RETIRED copy shape deployed to. deployed_copy_drift looks at exactly
        # this path for the leftover copy, so it must keep naming the copy-shape
        # leaf rather than the generated wrapper (hscc-dep-watcher.py).
        "script": "dep_pr_watcher.py",
        "deploy": DEPLOY_WRAPPER,
        "cadence_s": 86400,           # daily (`0 8 * * *`)
        "cadence_env": "HSCC_DEP_WATCHER_CADENCE_S",
        "install_script": "scripts/install_dep_watcher.sh",
    },
    "escalate_watcher": {
        "cron_job": "hscc-escalate-watcher",
        "script": "escalate_watcher_run.py",
        "deploy": DEPLOY_WRAPPER,
        "cadence_s": 900,             # every 15 min (`*/15 * * * *`)
        "cadence_env": "HSCC_ESCALATE_CADENCE_S",
        "install_script": "scripts/install_escalate_watcher.sh",
    },
    "alone_sweep_gate": {
        "cron_job": "hscc-alone-sweep-gate",
        "script": "alone_sweep_gate.sh",
        # NOT a copy: the job is a generated wrapper (hscc-alone-sweep-gate.sh)
        # that execs the repo's gate, so comparing a deployed copy makes no
        # sense here and a "missing copy" must never be inferred.
        "deploy": DEPLOY_WRAPPER,
        "cadence_s": 86400,           # nightly (`30 4 * * *`)
        "cadence_env": "HSCC_ALONE_SWEEP_CADENCE_S",
        "install_script": "scripts/install_alone_sweep_gate.sh",
    },
}

# Names in stable rendering order.
STREAMS = tuple(WATCHERS)

# Statuses that mean "nothing to warn about".
HEALTHY_STATUSES = ("ok", "not-configured")

#: Name of the doctor row this module owns, enumerating every stream in
#: ``WATCHERS`` -- an operator scanning doctor output has to be able to see that
#: the sweep nightly is in here (t_13a70290 was filed because it was not). A
#: fallback for doctor.py lives next to its other _*_CHECK_NAME constants so the
#: row still prints when this module cannot be imported at all (same honesty
#: rule as the notifier mask); test_doctor_row_registry.py pins the name list.
CHECK_NAME = "cron watchers (dep_pr/escalate/alone_sweep)"

#: Install marker file name, kept ONE LEVEL ABOVE the stamp dir (see
#: :func:`install_marker_path`).
MARKER_FILENAME = "watcher-installs.json"


def _state_writer():
    """Return the canonical state module (write_state / read_state / STATE_DIR).

    Prefers the real ``hscc_daemon.state`` package module -- the deployed
    runtime, and any test that patched its ``STATE_DIR``. Falls back to loading
    the sibling ``state.py`` standalone by path, for a ``scripts/``-only
    checkout where ``hscc_daemon`` is not importable. Raises ImportError if
    neither is reachable; callers decide how loud to be.
    """
    try:
        from hscc_daemon import state as _s
        return _s
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "hscc_daemon", "state.py")
        spec = importlib.util.spec_from_file_location(
            "hscc_cron_watcher_state", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def state_dir():
    """Default stamp dir: the canonical state module's STATE_DIR.

    Reads go exactly where ``write_state`` writes -- the ``hscc_daemon.state``
    package module when importable (deployed runtime, and tests that patch its
    STATE_DIR), else the sibling ``state.py`` loaded standalone. Callers that
    must read a DIFFERENT root (the doctor's ``hscc_dir`` seam: bootstrap
    sandboxes keep HOME real and relocate only HSCC_DIR) thread an explicit
    ``directory`` through :func:`read_stamp` / :func:`verdict` instead of
    mutating the environment, so a redirected read can never split from where
    writes landed -- and can never accidentally read the operator's live tree.
    """
    return _state_writer().STATE_DIR


def _dirs(directory=None):
    """(stamp_dir, marker_dir) -- honours an explicit stamp-dir seam."""
    if directory:
        directory = os.path.expanduser(directory)
        return directory, os.path.dirname(directory)
    d = state_dir()
    return d, os.path.dirname(d)


def cadence_s(stream, env=None):
    """Watcher cadence in seconds: env override, else the installer default."""
    row = WATCHERS[stream]
    raw = (os.environ if env is None else env).get(row["cadence_env"])
    if raw:
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            return val
    return row["cadence_s"]


def stale_window_s(stream, env=None):
    """Seconds after which a stamp is stale: MISS_FACTOR x cadence."""
    return MISS_FACTOR * cadence_s(stream, env=env)


def _parse_ts(value):
    """Parse a stamp timestamp; None when absent or unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def stamp(stream, **fields):
    """Record one watcher pass through the canonical state writer.

    Never raises: a stamp failure must never break -- or silently un-stamp --
    the watcher's real work, and silent no-op is exactly the GAP-B-4 failure
    mode this closes. Returns True when the stamp landed, False otherwise
    (callers print the difference; they never act on it).

    Honours ``state.persist_disabled_active()``: inside an ad-hoc ``hscc
    check`` run there is no real pass to record, so nothing is written.
    """
    row = WATCHERS.get(stream)
    if row is None:
        return False
    try:
        s = _state_writer()
        if getattr(s, "persist_disabled_active", lambda: False)():
            return False
        s.write_state(stream, {
            "ok": True,
            "watcher": stream,
            "cron_job": row["cron_job"],
            **fields,
        })
        return True
    except Exception:
        return False


def stamp_failure(stream, error, **fields):
    """Record a pass that raised: the stamp MOVES, ok=False carries the fault.

    A watcher that crashes mid-pass is the single most important thing to
    notice, and "crashed" must not look like "never ran" -- so this writes too,
    with ok=False and the error text. Returns True when the stamp landed.

    ``**fields`` ride along like they do on :func:`stamp` (t_13a70290): a
    verdict-producing watcher wants the failed pass to carry the same shape as a
    clean one, so the gate stamps ``verdict="red"`` plus the head it measured and
    the report dir, and the doctor's ``failing`` line names what the operator has
    to go read.
    """
    row = WATCHERS.get(stream)
    if row is None:
        return False
    try:
        s = _state_writer()
        s.write_state(stream, {
            "ok": False,
            "watcher": stream,
            "cron_job": row["cron_job"],
            "error": str(error)[:400],
            **fields,
        })
        return True
    except Exception:
        return False


def read_stamp(stream, directory=None):
    """Read a watcher's stamp dict; None when absent or unreadable-as-dict."""
    stamp_dir, _ = _dirs(directory)
    path = os.path.join(stamp_dir, stream + ".json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def install_marker_path(directory=None):
    """Path of the install marker file (ONE LEVEL ABOVE the stamp dir).

    Deliberately OUTSIDE the stamp dir: everything under ``~/.hscc/state`` is
    globbed by ``verify.check_daemon_streams`` as a daemon stream, and the
    marker is not a stream -- it is the record that an installer registered the
    cron job.
    """
    _, marker_dir = _dirs(directory)
    return os.path.join(marker_dir, MARKER_FILENAME)


def mark_installed(stream, schedule=None, directory=None):
    """Record that install_<...>_watcher.sh registered this cron job.

    Called by the installer, not by the watcher. The marker lets the doctor
    tell "installed, but the job never completed a pass" (the
    17-consecutive-failures incident class, t_3aadd2c0) from "never
    configured". Best-effort; returns True when the marker was written.
    """
    if stream not in WATCHERS:
        return False
    path = install_marker_path(directory)
    data = {}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[stream] = {
        "cron_job": WATCHERS[stream]["cron_job"],
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "schedule": schedule or "",
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def mark_removed(stream, directory=None):
    """Drop a stream's install marker (installer --uninstall). Best-effort."""
    path = install_marker_path(directory)
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or stream not in data:
            return True
        data.pop(stream, None)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def remove_stamp(stream, directory=None):
    """Drop a stream's stamp file (installer --uninstall). Best-effort.

    A deliberately-removed watcher must go back to ``not-configured``, not age
    into a permanent ``stale`` warn for a job nobody wants any more: the install
    marker alone cannot say "this operator uninstalled it on purpose". Returns
    True when there is no stamp left (absent counts as done), False when one
    could not be removed.
    """
    if stream not in WATCHERS:
        return False
    stamp_dir, _ = _dirs(directory)
    path = os.path.join(stamp_dir, stream + ".json")
    if not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def read_install_marker(stream, directory=None):
    """Install marker row for a stream, or None."""
    try:
        with open(install_marker_path(directory)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    row = data.get(stream)
    return row if isinstance(row, dict) else None


# ── deployed-copy drift (t_95eb9f15) ────────────────────────────────────────

def bound_hermes_home(env=None):
    """The Hermes home a watcher cron job is bound to.

    The installer's own rule, in one place: ``$HERMES_HOME`` when set, else
    ``~/.hermes`` (``install_dep_watcher.sh`` / ``install_escalate_watcher.sh``
    / ``install_alone_sweep_gate.sh`` all derive both the copy destination and
    the cron binding from it -- the t_3aadd2c0 drift rule). Read through an
    ``env`` seam rather than a mutated environment, like every other read here.
    """
    e = os.environ if env is None else env
    raw = e.get("HERMES_HOME")
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(os.path.expanduser("~"), ".hermes")


def hermes_home_candidates(env=None):
    """Every Hermes home a deployed watcher copy could legitimately live under.

    ``bound_hermes_home()`` first, then the shared ``~/.hermes``. A LIST, not a
    single home, because the home a *doctor* run sees is not necessarily the
    home the *installer* ran under: installing from a profile shell writes
    ``$HERMES_HOME/scripts`` (== that profile's home), installing from a plain
    shell writes ``~/.hermes/scripts``, and the t_3aadd2c0 incident was exactly
    this split. A check that consults only the caller's own home reports
    ``missing-deployed`` for a copy that is sitting one directory away — a false
    clean, which is worse than no check. (Measured on the live host: with
    ``HERMES_HOME`` pointing at a profile home, the drifted Sep-29 copy is in the
    shared ``~/.hermes/scripts``.)
    """
    out = [bound_hermes_home(env)]
    shared = os.path.join(os.path.expanduser("~"), ".hermes")
    if shared not in out:
        out.append(shared)
    return out


def checkout_root():
    """Repo root when this module runs from a checkout, else None.

    The deployed plugin payload (``~/.hermes/plugins/hscc_daemon/``) is NOT a
    checkout, and its parent has no ``scripts/`` to compare against -- so a
    watcher stamping through the deployed copy gets ``None`` here and the drift
    check stays out of the way instead of inventing a repo next to the plugins.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if os.path.isfile(os.path.join(root, "hscc-bootstrap", "doctor.py")):
        return root
    return None


def _digest(path):
    """(sha256-hex, size) for a file, or (None, None) when unreadable."""
    h = hashlib.sha256()
    try:
        size = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
    except OSError:
        return None, None
    return h.hexdigest(), size


def deployed_copy_drift(stream, *, hermes_home=None, repo_dir=None, env=None):
    """Compare a watcher's DEPLOYED copy against the REPO copy it is installed from.

    The ``copy`` installers (``deploy`` == :data:`DEPLOY_COPY`) ``cp`` the repo
    file into ``<bound home>/scripts/``, which leaves a second copy of the code
    on the host that nothing ever re-checked. That gap is how a healthy
    ``hscc-dep-watcher`` read as ``not configured``: the deployed copy predated
    GAP-B-4 stamping, so the job ran, did its work, left no stamp, and the
    doctor blamed an operator who had in fact installed it (t_95eb9f15).

    Returns ``{stream, status, deployed, repo, candidates, detail}`` where
    ``status`` is:

      ``in-sync``          byte-identical -- nothing to say. For a ``wrapper``
                           stream this means a leftover copy from the retired
                           copy shape is present and still matches git.
      ``drifted``          both exist and differ -- the deployed copy is stale
                           code. This is the case that must be said out loud.
      ``missing-deployed`` nothing under any candidate ``scripts`` dir, and the
                           stream is (or was installed as) a ``copy`` stream --
                           genuinely not installed, which is what
                           ``not-configured`` means.
      ``missing-repo``     no repo copy to compare against (this module is not
                           running from a checkout) -- UNKNOWN, never a warning.
      ``unreadable``       a copy exists but could not be hashed -- unknown.
      ``not-a-copy``       the stream deploys as a generated wrapper
                           (:data:`DEPLOY_WRAPPER`) AND nothing sits at the path
                           the retired copy shape used -- no second copy exists,
                           so there is nothing to compare (t_accbc5da).

    A ``wrapper`` stream is NOT exempt from the comparison. "git says wrapper" is
    a statement about the installer, not about the host: an operator who has not
    re-run it still has the old ``cp``-ed file under ``<bound home>/scripts`` and
    its cron job still runs it. So a wrapper stream answers ``not-a-copy`` only
    when that path is empty, and compares the leftover otherwise -- which is what
    keeps the ``copy-drifted`` warn alive across the transition (t_95eb9f15).

    ``hermes_home`` narrows the search to ONE home (the test seam, and what an
    installer knows its own binding to); without it every home in
    :func:`hermes_home_candidates` is consulted and ``deployed`` names the copy
    that mismatched (or the first one found when none does). ``repo_dir`` pins
    the other side (default: this module's own checkout).

    Never raises, and never warns on its own: :func:`verdict` decides which
    statuses deserve a WARN (only ``drifted`` does), so an unreadable or
    absent copy cannot nag an operator about a watcher they never installed.
    """
    row = WATCHERS.get(stream)
    if row is None:
        raise KeyError(stream)
    out = {"stream": stream, "status": "unknown", "deployed": None,
           "repo": None, "candidates": [], "detail": ""}

    is_copy = row.get("deploy") == DEPLOY_COPY
    homes = ([os.path.expanduser(hermes_home)] if hermes_home
             else hermes_home_candidates(env))
    repo_root = os.path.expanduser(repo_dir) if repo_dir else checkout_root()
    copies = [os.path.join(h, "scripts", row["script"]) for h in homes]
    out["candidates"] = copies
    out["deployed"] = copies[0]
    existing = [p for p in copies if os.path.isfile(p)]

    if not existing:
        if is_copy:
            out["status"] = "missing-deployed"
            out["detail"] = f"nothing deployed at {' or '.join(copies)}"
        else:
            out["status"] = "not-a-copy"
            out["detail"] = (f"{stream}'s cron job is a generated wrapper that runs "
                             f"the repo's {row['script']} -- nothing was copied to "
                             f"{' or '.join(copies)}, so there is no deployed copy "
                             f"that could go stale")
        return out

    if not repo_root:
        out["status"] = "missing-repo"
        out["detail"] = (f"no checkout to compare {' or '.join(existing)} against "
                         f"(this cron_watchers.py is not running from a repo)")
        return out

    repo = os.path.join(repo_root, "scripts", row["script"])
    out["repo"] = repo
    if not os.path.isfile(repo):
        out["status"] = "missing-repo"
        out["detail"] = f"no repo copy at {repo} to compare against"
        return out

    r_hash, r_size = _digest(repo)
    if r_hash is None:
        out["status"] = "unreadable"
        out["detail"] = f"the repo copy {repo} could not be read to be hashed"
        return out

    stale = None
    for path in existing:
        p_hash, p_size = _digest(path)
        if p_hash is None:
            # Cannot prove it, so cannot call it stale: fail open.
            out["status"] = "unreadable"
            out["detail"] = (f"the deployed copy {path} could not be read to be "
                             f"hashed")
            return out
        if p_hash != r_hash and stale is None:
            stale = (path, p_hash, p_size)
    if stale is None:
        out["deployed"] = existing[0]
        out["status"] = "in-sync"
        out["detail"] = (f"deployed copy matches the repo copy ({r_size} B, "
                         f"{r_hash[:8]})"
                         + (f" in all {len(existing)} candidate homes"
                            if len(existing) > 1 else "")
                         + ("" if is_copy else
                            f" -- but a copy is still installed, and the "
                            f"installer does not copy any more: re-run "
                            f"{row['install_script']} to drop it and let the "
                            f"generated wrapper run the repo's file"))
        return out
    deployed, d_hash, d_size = stale
    out["deployed"] = deployed
    out["status"] = "drifted"
    out["detail"] = (f"the deployed copy {deployed} ({d_size} B, {d_hash[:8]}) is "
                     f"NOT the repo copy {repo} ({r_size} B, {r_hash[:8]}) -- the "
                     f"job runs stale code; re-run {row['install_script']}"
                     + ("" if is_copy else
                        f" (that installer now generates a wrapper instead of a "
                        f"copy, which removes this leftover)"))
    return out


def verdict(stream, *, now=None, state=None, env=None, directory=None, copy=None):
    """Liveness verdict for one watcher -- RAW status, no surface-specific ok.

    Returns {stream, status, warn, detail, age_s, window_s, copy} where
    ``status`` is:

      ``ok``               stamp fresh -- healthy.
      ``not-configured``   no stamp file and no install marker -- the watcher
                           is off. Clean, NOT a warning: an operator who never
                           installed it must not be nagged every doctor run.
      ``copy-drifted``     no stamp, no marker, but the deployed copy under
                           ``<bound home>/scripts`` is NOT the repo copy it was
                           installed from -- the job runs code older than the
                           stamping it is missing. Warns (t_95eb9f15).
      ``never-ran``        the installer registered the cron job (install
                           marker present) but no pass ever completed -- the
                           17-consecutive-failures incident class. Warns.
      ``stale``            stamp older than :func:`stale_window_s` -- the cron
                           job stopped firing. The GAP-B-4 case.
      ``failing``          stamp fresh but the last pass recorded ok=False.
      ``bad-stamp``        stamp exists, timestamp unparseable.

    ``warn`` is True for every non-healthy status and ``detail`` carries the
    repo's ``WARN:`` prefix there, so any surface printing the row reads it as
    a warning.

    ``copy`` is the deployed-copy status (:func:`deployed_copy_drift`) that
    produced this verdict, or None when no comparison was done. Pass a
    pre-resolved drift dict in ``copy`` to ask for one; leaving it None keeps
    this a PURE STAMP READ -- a caller that has not said which Hermes home to
    read does not get a host filesystem read done on its behalf, which is the
    same reason ``directory`` is a parameter and not an environment mutation.

    Consumers map status onto their own ok convention on purpose: the doctor
    row renders non-healthy as a NON-FATAL failure (ok=False, fatal=False --
    doctor's warning shape) while ``hscc verify`` keeps ok=True with the WARN
    detail, so a dead watcher never turns the cluster smoke test RED.
    """
    if stream not in WATCHERS:
        raise KeyError(stream)
    row = WATCHERS[stream]
    window = stale_window_s(stream, env=env)
    if state is None:
        state = read_stamp(stream, directory=directory)
    copy_status = copy.get("status") if isinstance(copy, dict) else None
    copy_detail = copy.get("detail") if isinstance(copy, dict) else None
    # Only a `drifted` copy is ever a warning; an absent, unreadable or
    # not-a-copy result must never change a verdict (see deployed_copy_drift).
    drifted = bool(copy_detail) if copy_status == "drifted" else False

    base = {"stream": stream, "age_s": None, "window_s": window,
            "copy": copy_status}

    if state is None:
        # No stamp. Two very different reasons, and conflating them is how the
        # 17-consecutive-failures incident stayed invisible: an installer
        # marker means the job WAS registered, so silence means it never
        # completed a pass (a warning); no marker means it was never installed
        # (clean, no warning). A third reason exists and used to be invisible:
        # the job IS installed and IS firing, but the deployed copy predates
        # stamping, so it cannot report itself (t_95eb9f15) -- that reads
        # `copy-drifted`, which blames the copy and not the operator.
        if read_install_marker(stream, directory=directory):
            detail = (f"WARN: {stream} ({row['cron_job']}) was "
                      f"installed ({row['install_script']}) but has "
                      f"never completed a pass -- check "
                      f"`hermes cron list` for failing runs")
            if drifted:
                detail += f"; and {copy_detail}"
            return {**base, "status": "never-ran", "warn": True, "detail": detail}
        if drifted:
            return {**base, "status": "copy-drifted", "warn": True,
                    "detail": (f"WARN: {stream} ({row['cron_job']}) has no "
                               f"liveness stamp and {copy_detail}")}
        return {**base, "status": "not-configured", "warn": False,
                "detail": (f"{stream}: not configured (no stamp; install with "
                           f"{row['install_script']} if it should run)")}

    ts = _parse_ts(state.get("timestamp"))
    if ts is None:
        return {**base, "status": "bad-stamp", "warn": True,
                "detail": (f"WARN: {stream}: stamp carries no parseable "
                           f"timestamp -- liveness cannot be judged")}

    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - ts).total_seconds()
    base["age_s"] = int(age)

    if age > window:
        return {**base, "status": "stale", "warn": True,
                "detail": (f"WARN: {stream} ({row['cron_job']}) last ran "
                           f"{int(age / 60)}m ago -- past the "
                           f"{int(window / 60)}m staleness window; the cron "
                           f"job is not firing (check `hermes cron list`; "
                           f"cadence env {row['cadence_env']})")}
    if state.get("ok") is False:
        err = str(state.get("error") or state.get("message") or "last pass failed")
        return {**base, "status": "failing", "warn": True,
                "detail": (f"WARN: {stream} ({row['cron_job']}) ran recently "
                           f"but its last pass reported a failure: {err[:200]}")}
    return {**base, "status": "ok", "warn": False,
            "detail": (f"{stream} ({row['cron_job']}) fresh "
                       f"({int(age)}s old, window {window}s)")}


def all_verdicts(*, now=None, env=None, directory=None, copies=None):
    """Verdicts for every registered watcher, in stable order.

    ``copies`` is an optional ``{stream: deployed_copy_drift()}`` map threaded
    into each verdict's ``copy`` seam; leave it out (or leave a stream out of
    it) for a pure stamp read.
    """
    return [verdict(s, now=now, env=env, directory=directory,
                    copy=(copies or {}).get(s)) for s in STREAMS]


def doctor_check(hscc_dir=None, *, now=None, env=None,
                 check_deployed_copies=False, hermes_home=None, repo_dir=None):
    """The GAP-B-4 doctor row: are the cron watchers still firing?

    The verdict lives with the feature (this module owns the cadence SOT and
    the stamp format) and the bootstrap doctor only adapts the dict to its
    five Check fields, exactly as it does for ``orch_waker.doctor_check``.

    ``hscc_dir`` is the ~/.hscc ROOT (where cluster.json lives), matching the
    doctor_notifier_mask convention: the stamp dir is ``<hscc_dir>/state`` and
    the install marker sits at ``<hscc_dir>/watcher-installs.json``. Redirected
    by parameter, never by mutating os.environ -- a check whose seam is ignored
    (because the state module resolves its dir from HOME at import) would read
    the operator's live tree from a sandboxed bootstrap, which is exactly the
    bug class that took down the live daemon-pid verdict before. Passing the
    directory down keeps read and marker on ONE root, always.

    Warn-only: ``ok`` is False for any stale/failing/never-ran/bad-stamp/
    copy-drifted watcher, but ``fatal`` is ALWAYS False -- an out-of-band Hermes
    cron job must never hard-stop a bootstrap or flip the doctor's overall
    verdict. "not configured" is clean ok=True with a plain detail, never a
    warning.

    ``check_deployed_copies`` is the deployed-copy opt-in (t_95eb9f15): say True
    and every row also reports whether the copy installed under the bound Hermes
    home is still the repo copy — the difference between "nobody installed this"
    and "the install is stale", and the invisible one of the two (its watcher
    keeps running and looks unconfigured). The default (False) reads NO host
    scripts dir: the same doctrine as ``hscc_dir`` above, so a caller that has
    not asked for a host read never gets one done on its behalf.
    ``hermes_home`` narrows the comparison to one home (and implies True);
    ``repo_dir`` pins what to compare against (default: this module's own
    checkout, via :func:`checkout_root`).
    """
    directory = (os.path.join(os.path.expanduser(hscc_dir), "state")
                 if hscc_dir else None)
    copies = None
    if check_deployed_copies or hermes_home:
        copies = {s: deployed_copy_drift(s, hermes_home=hermes_home,
                                         repo_dir=repo_dir, env=env)
                  for s in STREAMS}
    rows = all_verdicts(now=now, env=env, directory=directory, copies=copies)

    problems = [r["detail"] for r in rows if r["warn"]
                or r["status"] not in HEALTHY_STATUSES]
    notes = [r["detail"] for r in rows if not r["warn"]
             and r["status"] in HEALTHY_STATUSES]
    if problems:
        # Cheap-first: the sweep pass is ~25 min per pair, so the fix that fits
        # in a terminal session is the preflight + --status, not a re-run
        # (t_13a70290; the cadence is the gate header's own routing decision).
        sweep_hint = ""
        if any(r["stream"] == "alone_sweep_gate" for r in rows if r["warn"]):
            sweep_hint = (" -- for alone_sweep_gate, start cheap: "
                          "`scripts/alone_sweep_gate.sh --preflight` checks the "
                          "wiring in seconds and "
                          "`scripts/install_alone_sweep_gate.sh --status` prints "
                          "the last verdict on record; the sweep pass itself is "
                          "minutes per interpreter")
        # A drifted deployed copy has a different (and much cheaper) fix than a
        # dead cron job: re-run the installer. Say it per stream, or the
        # operator chases `hermes cron list` for a job that is fine (t_95eb9f15).
        drift_hint = ""
        stale = [r["stream"] for r in rows if r.get("copy") == "drifted"]
        if stale:
            drift_hint = " -- " + "; ".join(
                f"{s}'s deployed copy under <bound home>/scripts is STALE, not "
                f"missing: re-run {WATCHERS[s]['install_script']}" for s in stale)
        return {
            "name": CHECK_NAME, "ok": False, "fatal": False, "rows": rows,
            "detail": "; ".join(problems + notes),
            "fix": ("check `hermes cron list` (job paused? ticker down? the "
                    "script moved out of <bound home>/scripts? -- the "
                    "t_3aadd2c0 failure class), then `hermes cron run <job>` "
                    "to prove a pass; reinstall with "
                    + " / ".join(sorted({r['install_script']
                                         for r in WATCHERS.values()}))
                    + " if a job is gone" + drift_hint + sweep_hint),
        }
    return {"name": CHECK_NAME, "ok": True, "fatal": False, "rows": rows,
            "detail": "; ".join(notes) or "no watchers configured",
            "fix": ""}


def fmt_age(seconds):
    """Human age: '38m', '4.2h', '2.1d'."""
    if seconds is None:
        return "never"
    if seconds < 5400:
        return f"{max(0, int(seconds / 60))}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
