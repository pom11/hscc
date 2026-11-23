"""Copy the outgoing litellm proxy log aside BEFORE hscc asks for a restart.

WHY THIS EXISTS (t_9713c765; audit ``2026-11-22-proxy-connection-error.md`` §3.4)
---------------------------------------------------------------------------------
The ``--restart`` that fixed the 2026-11-22 :4000 outage **destroyed the only
record of it**. sparkrun opens the gateway log in TRUNCATE mode on every launch
— ``sparkrun/proxy/_supervisor.py::GatewaySupervisor._launch_background`` does
``open(self.log_path, "w")``, and ``log_path`` is ``state_dir / log_name``, with
``log_name = "litellm.log"`` on the litellm gateway. Both launch paths in the
engine (``start()`` and the config-update restart) reach that one function, so
every restart — including the ones hscc issues — truncates the file.

After that restart the live log began at ``INFO: Started server process
[<new pid>]``, ``grep -cE 'Connection error|InternalServerError'`` returned 0,
and no rotated copy existed anywhere under the sparkrun cache or home. The
audit therefore had to state its §3.2 mechanism as a hypothesis: the evidence
was destroyed by the act of remediating, and the repair that destroyed it was
hscc's own.

THE RIGHT FIX IS UPSTREAM, AND THIS IS NOT IT
---------------------------------------------
sparkrun owns ``<sparkrun cache>/proxy/`` and its log lifecycle, so the small
correct change is a rotate inside ``_launch_background`` (one helper, one call
site) — it covers every gateway and every launcher at once. This module is a
**fallback**, kept because hscc cannot gate the preservation of its own
incident evidence on a third-party release. It is deliberately written so that
retiring it is trivial: when sparkrun rotates on its own side, the outgoing log
is already renamed before hscc copies it, this finds no file and no-ops. See
the upstream ask recorded on card t_9713c765.

CONTRACT — the three rules that make a fallback safe
----------------------------------------------------
* **copy-only, and hscc-owned.** The live log is opened for READ and copied
  into hscc's own ``~/.hscc/proxy-logs/``; nothing is ever written into
  sparkrun's cache dir, and the live file is never moved, truncated or
  unlinked. Archives land as ``<name>.<YYYYmmdd-HHMMSS>.pre-restart.log`` and
  are moved into place from a ``.part`` sibling, so a name that exists means a
  copy that completed.
* **never raises.** A lost log must not block recovery. Every failure comes
  back as ``ok=False`` plus an ``error`` string for the caller to WARN about;
  the restart proceeds regardless.
* **bounded.** Retention keeps the newest ``HSCC_PROXY_LOG_KEEP`` archives
  (default 5) *and* trims to ``HSCC_PROXY_LOG_CAP_BYTES`` total (default
  64 MiB), whichever bites first. The newest archive is never pruned, so a tiny
  cap cannot delete the evidence that was just copied. ``keep=0`` disables
  archiving; ``cap_bytes<=0`` disables only the size bound.

Why this is not "hscc reaching into sparkrun's internals": hscc already
resolves this directory — ``proxy_router.LITELLM_CONFIG_PATH`` is the config
file sparkrun writes beside this log, and the dep-bump drift check reads it
every tick. We copy a file at a path hscc already knows. We do not call,
import or imitate any sparkrun code.
"""

from __future__ import annotations

import glob
import os
import shutil
import time

#: Filename sparkrun truncates on launch (sparkrun
#: ``plugins/litellm/engine.py::log_name`` for the litellm gateway).
PROXY_LOG_NAME = "litellm.log"

#: Archive shape, fixed by the card DECISION: ``<name>.<stamp>.pre-restart.log``.
ARCHIVE_SUFFIX = "pre-restart.log"

#: Where archives live — hscc's own tree, deliberately NOT sparkrun's cache dir.
PROXY_LOG_DIR = os.path.expanduser("~/.hscc/proxy-logs")

#: How many newest archives to keep. ``0`` disables archiving entirely.
PROXY_LOG_KEEP = int(os.environ.get("HSCC_PROXY_LOG_KEEP", "5"))

#: Ceiling over ALL archives' sizes. The newest archive is never deleted by it.
PROXY_LOG_CAP_BYTES = int(os.environ.get("HSCC_PROXY_LOG_CAP_BYTES",
                                         str(64 * 1024 * 1024)))

#: Test seam. When set it *is* the source path, so a test (or the conftest
#: hermetic floor) can point this module at a tmp file — or, by pointing it at
#: a path that does not exist, guarantee that no test ever reads the operator's
#: live proxy log. Production leaves it None and derives the real path.
SRC_OVERRIDE = None


def _stamp(now=None):
    """Local-time ``YYYYmmdd-HHMMSS`` — local because the log's own timestamps
    are local and an operator comparing them must not have to convert."""
    return time.strftime("%Y%m%d-%H%M%S",
                         time.localtime(time.time() if now is None else now))


def proxy_log_path(name=PROXY_LOG_NAME):
    """The live gateway log, derived from a path hscc already resolves.

    Sibling of ``proxy_router.LITELLM_CONFIG_PATH``: sparkrun writes the config
    and the log into the same state dir. Derived rather than duplicated so the
    two can never drift apart. Never raises — resolution failures surface to
    the caller as a missing file, which is a no-op, not a repair failure.
    """
    if SRC_OVERRIDE:
        return SRC_OVERRIDE
    from . import proxy_router  # lazy: keeps health<->module import order simple
    return os.path.join(os.path.dirname(
        os.path.expanduser(proxy_router.LITELLM_CONFIG_PATH)), name)


def archives(dest_dir=None, name=PROXY_LOG_NAME):
    """Completed archives in *dest_dir*, newest first (mtime, then name)."""
    dest_dir = PROXY_LOG_DIR if dest_dir is None else dest_dir
    found = []
    for path in glob.glob(os.path.join(dest_dir, f"{name}.*.{ARCHIVE_SUFFIX}")):
        try:
            found.append((os.path.getmtime(path), os.path.basename(path), path))
        except OSError:
            continue  # vanished between glob and stat: not ours to worry about
    found.sort(reverse=True)
    return [p for _mtime, _name, p in found]


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _free_dest(dest_dir, name, stamp=None):
    """An unused archive path for this second.

    Two restarts inside one second would otherwise collide, and ``os.replace``
    silently overwrites — so the second gets ``-2``, ``-3`` … appended. Still
    matches the ``*.pre-restart.log`` retention glob.
    """
    stamp = _stamp() if stamp is None else stamp
    dest = os.path.join(dest_dir, f"{name}.{stamp}.{ARCHIVE_SUFFIX}")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_dir, f"{name}.{stamp}-{n}.{ARCHIVE_SUFFIX}")
    return dest


def prune(dest_dir=None, keep=None, cap_bytes=None, name=PROXY_LOG_NAME):
    """Delete oldest archives until both bounds hold. Returns removed paths.

    ``keep<=0`` drops every archive (only reachable directly — the archiving
    path returns before that when archiving is disabled). The newest archive is
    exempt from the SIZE bound: deleting the copy that was just made to satisfy
    a small cap would destroy exactly what this module exists to preserve.
    """
    dest_dir = PROXY_LOG_DIR if dest_dir is None else dest_dir
    keep = PROXY_LOG_KEEP if keep is None else int(keep)
    cap_bytes = PROXY_LOG_CAP_BYTES if cap_bytes is None else int(cap_bytes)

    files = archives(dest_dir, name)
    if not files:
        return []
    doomed = list(files) if keep <= 0 else files[keep:]
    total = sum(_size(p) for p in files)
    if cap_bytes > 0:
        newest = files[0]
        for path in reversed(files):     # oldest first
            if total <= cap_bytes or path is newest:
                break
            if path not in doomed:
                doomed.append(path)
            total -= _size(path)
    removed = []
    for path in sorted(set(doomed)):
        try:
            os.unlink(path)
        except OSError:
            continue                      # a prune failure is never an outage
        removed.append(path)
    return removed


def archive_before_restart(src=None, dest_dir=None, keep=None, cap_bytes=None,
                          name=PROXY_LOG_NAME):
    """Copy the outgoing proxy log aside. Never raises; returns a result dict.

    Keys: ``ok`` (bool — False only when there WAS a log to save and the copy
    failed), ``action`` (``archived`` | ``absent`` | ``empty`` | ``disabled``),
    ``archived`` (path or None), ``error`` (str or None), ``pruned`` (list).

    Call this BEFORE asking sparkrun for a restart, while the proxy is still
    serving: the only thing done to the live file here is an open-for-read.
    """
    keep = PROXY_LOG_KEEP if keep is None else int(keep)
    out = {"ok": True, "action": "absent", "archived": None, "error": None,
           "pruned": []}
    if keep <= 0:
        out["action"] = "disabled"
        return out
    try:
        src = proxy_log_path(name) if src is None else src
        if not os.path.isfile(src):
            return out                     # nothing to preserve: not a failure
        if _size(src) <= 0:
            out["action"] = "empty"
            return out                     # an empty log is already nothing
        dest_dir = PROXY_LOG_DIR if dest_dir is None else dest_dir
        os.makedirs(dest_dir, exist_ok=True)
        dest = _free_dest(dest_dir, name)
        partial = dest + ".part"
        try:
            # Copy to a temp name, then move: a visible archive is a complete
            # archive, even if the daemon dies half-way through the copy.
            shutil.copyfile(src, partial)
            os.replace(partial, dest)
        except BaseException:
            try:
                os.unlink(partial)
            except OSError:
                pass
            raise
        out["archived"] = dest
        out["action"] = "archived"
    except BaseException as exc:           # noqa: BLE001 - must never block repair
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        out["pruned"] = prune(dest_dir, keep, cap_bytes, name)
    except BaseException as exc:           # noqa: BLE001 - retention is cosmetic
        out["error"] = f"prune failed: {type(exc).__name__}: {exc}"
    return out
