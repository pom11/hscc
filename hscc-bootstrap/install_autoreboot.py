"""Install the panic-to-reboot sysctl drop-in on every cluster node (t_1fc3fe83).

Node-recovery batch part 2. Context: a node hard-froze with the journal dead
mid-sentence and NO panic line at all — and Sparks have no BMC, so the only
recovery path was a human at the power button. With these knobs ON, a
kernel-level wedge (panic, oops, soft lockup, hung task, hard lockup where the
kernel exposes it) self-resets in ~10 seconds instead of waiting for a human.

The managed state is ONE file per node::

    /etc/sysctl.d/90-hscc-autoreboot.conf

written atomically (tmp file + root ``mv``, same-filesystem rename) and
applied with ``sysctl --system``, then READ BACK and verified key by key — a
read-back mismatch fails that node loudly, it never reports success.

Transport: the existing sparkrun ssh path, nothing else. The default
``_ssh`` resolves ``hscc-cluster/clusterlib.ssh_cmd`` (which delegates to
``sparkrun_bridge.remote_cmd`` → sparkrun's own ``run_remote_script`` with
sparkrun's ssh kwargs). This module never builds an ``ssh`` command line and
never shells out to the fleet itself; every remote byte goes through that one
bridge, and tests inject a fake at the ``_ssh`` seam.

Root: privileged steps use ``sudo -n`` (never a password prompt — sudo without
``-n`` would hang the ssh session). A refused ``sudo -n`` fails THAT NODE with
a clear message; the other nodes proceed.

Per-node kernel variance: ``kernel.hardlockup_panic`` is written ONLY when the
knob exists on that node (GB10 kernels vary). If any other managed knob turns
out to be absent, it is skipped with a note rather than failing the node — the
existence probe is ``/proc/sys/<path>``, which is exactly what sysctl(8) reads,
so "file absent" == "sysctl -a would not list it" without depending on
sysctl --version-specific output formatting.

Opt-out: ``~/.hscc/cluster.json`` -> ``{"node_recovery": {"autoreboot": false}}``
(default ON when the block or subkey is absent; unknown keys stay ignorable by
older consumers). The sibling watchdog card owns the ``watchdog`` subkey;
``node_recovery`` is the shared block, one subkey per layer, so the two cards
never edit the same reader. Doctor coverage is the sibling doctor card (child
of this one): it must reuse ``MANAGED_KEYS``/``HARDLOCKUP_KEY``/``build_dropin``
and the probe command exported here rather than re-deriving the expected set.

THE hung_task DECISION (review gate — the reasoning lives here and in the PR
body; the doctor card asserts against this exact value)::

    kernel.hung_task_timeout_secs is pinned to 600 IN THE DROP-IN together
    with kernel.hung_task_panic=1 — never left at the 120s distro default.

  hung_task_panic turns any task stuck in D-state longer than the timeout
  into a panic — with panic=10, a reboot. This fleet legitimately produces
  long D-state: NFS hard mounts put every task touching the mount in D until
  the RPC returns (a 1-2 minute NAS failover is an everyday event here), and
  serving paths mmap model weights off storage. At the 120s default, an
  ordinary NFS failover would reboot a node mid-serve: the false-positive
  cost is a full service outage plus a cold model reload of tens of minutes,
  fired precisely during an infrastructure hiccup the fleet should ride out.
  The false-negative cost of 600s is that a genuinely stuck storage backend
  leaks blocked tasks for 8 more minutes before the reset — and a true
  hung-task condition leaks indefinitely anyway, so 8 extra minutes of
  detection latency is not a real loss. 600 = ~5x the default: comfortably
  beyond any observed NFS failover or checkpoint sync on this NAS, and still
  short enough that a node with tasks wedged 10 minutes gets reset rather
  than limping — at that point it cannot serve anything anyway. Pinning the
  value IN the file (not relying on the distro default) is deliberate: the
  doctor card asserts one known-good state per key, and a value that differs
  between Ubuntu point releases would make "green" meaningless. If a shared
  storage fault ever produces a reboot loop (visible as consecutive short
  uptimes), the correct fix is the mount options (soft/timeo), not raising
  this timeout further.
"""

from __future__ import annotations

import base64
import json
import os
import sys

DROPIN_PATH = "/etc/sysctl.d/90-hscc-autoreboot.conf"

#: Always-managed knobs (ordered — this order IS the drop-in file order).
#: kernel.hardlockup_panic is deliberately NOT here: it is conditional.
MANAGED_KEYS: dict[str, str] = {
    "kernel.panic": "10",                    # reboot 10s after any panic
    "kernel.panic_on_oops": "1",             # an oops is a panic, not a limp
    "kernel.softlockup_panic": "1",          # stuck CPU loop -> panic
    "kernel.hung_task_panic": "1",           # D-state past the timeout -> panic
    "kernel.hung_task_timeout_secs": "600",  # pinned with it — see docstring
}

#: Written ONLY when the knob exists on the node (GB10 kernels vary).
HARDLOCKUP_KEY = "kernel.hardlockup_panic"
HARDLOCKUP_VALUE = "1"

#: All keys this module may assert about (probe set; membership decides the
#: per-node file content and the doctor card's expected set).
ALL_KNOBS: tuple[str, ...] = tuple(MANAGED_KEYS) + (HARDLOCKUP_KEY,)

_HEADER = (
    "# hscc node-recovery: panic-to-reboot (managed by hscc bootstrap).\n"
    "# Editing this file makes `hscc bootstrap` report DRIFT and leave it\n"
    "# alone — it never clobbers operator edits. See hscc-bootstrap/\n"
    "# install_autoreboot.py for the why of every value.\n"
)


def build_dropin(has_hardlockup: bool, skip: tuple = ()) -> str:
    """Exact managed-file content.

    ``has_hardlockup`` toggles the conditional hardlockup key; ``skip`` names
    any other knob the node's kernel does not expose (omitted rather than
    written — sysctl would only warn on it and the read-back could never make
    it stick).
    """
    lines = [_HEADER]
    for key, value in MANAGED_KEYS.items():
        if key in skip:
            continue
        lines.append(f"{key} = {value}\n")
    if has_hardlockup:
        lines.append(f"{HARDLOCKUP_KEY} = {HARDLOCKUP_VALUE}\n")
    return "".join(lines)


def _proc_path(key: str) -> str:
    """kernel.foo_bar -> /proc/sys/kernel/foo_bar (what sysctl(8) itself reads)."""
    return "/proc/sys/" + key.replace(".", "/")


def probe_command() -> str:
    """One bash script: knob existence + current values + current drop-in.

    Emits ``KNOB <key>`` lines (only for knobs that EXIST), ``VAL <key> <value>``
    lines, then a ``---FILE---`` sentinel followed by the drop-in content (or
    nothing when absent). ``/proc/sys`` existence is the kernel's own surface
    — it is exactly what ``sysctl -a`` lists — so GB10 kernel variance shows up
    here without parsing sysctl-version-specific output formats.
    """
    parts = ['for k in %s; do' % " ".join(ALL_KNOBS)]
    parts.append('f=$(echo "$k" | tr "." "/"); p="/proc/sys/$f"; '
                 '[ -e "$p" ] || continue; echo "KNOB $k"; '
                 'echo "VAL $k $(cat "$p" 2>/dev/null | head -1 | tr -d " \\r")"; done')
    # `if...fi`, NOT `[ -f ] && cat` — the latter exits 1 when the file is
    # absent (the normal fresh-node state) and would render the probe failed.
    parts.append(f'echo "---FILE---"; if [ -f "{DROPIN_PATH}" ]; then '
                 f'cat "{DROPIN_PATH}"; fi; exit 0')
    return "\n".join(parts)


def parse_probe(stdout: str) -> dict:
    """Split probe output into {knobs: {key: value}, file: str|None}.

    The sentinel line is always emitted by echo, so an ABSENT drop-in leaves
    stdout ending at the sentinel: a blank/whitespace body is therefore
    indistinguishable from "no file" and normalizes to None (an empty file is
    a no-op for sysctl anyway).
    """
    head, sep, body = stdout.partition("\n---FILE---\n")
    if not sep:
        head, sep, body = stdout.partition("---FILE---")
    knobs: dict[str, str] = {}
    for line in head.splitlines():
        if line.startswith("VAL "):
            key, _, value = line[4:].partition(" ")
            knobs[key] = value.strip()
    return {"knobs": knobs, "file": body if body.strip() else None}


def write_command(content: str) -> str:
    """One bash script: atomic root install of the drop-in + sysctl --system.

    base64 carries the content through ssh with zero quoting risk. The file
    lands in two hops so the FINAL path is only ever reached by an atomic
    same-filesystem rename: (1) user-writable /tmp (unique pid name, no sudo
    needed to create), (2) ``sudo -n install`` into a HIDDEN non-``*.conf``
    name inside /etc/sysctl.d (so a concurrent ``sysctl --system`` glob can
    never half-load it), then (3) ``sudo -n mv`` = same-dir rename, atomic.
    All privileged steps are ``sudo -n`` — a password-required sudo fails the
    script immediately (``set -e``) instead of hanging the ssh session.
    """
    b64 = base64.b64encode(content.encode()).decode()
    tmp = "/tmp/hscc-autoreboot.$$.conf"
    stage = f"{DROPIN_PATH}.hscc-new.$$"
    return "\n".join([
        "set -euo pipefail",
        "umask 077",
        "base64 -d > '%s' <<'HSCC_B64'" % tmp,
        b64,
        "HSCC_B64",
        "sudo -n install -m 0644 -o root -g root '%s' '%s'" % (tmp, stage),
        "rm -f '%s'" % tmp,
        "sudo -n mv -f '%s' '%s'" % (stage, DROPIN_PATH),
        "sudo -n sysctl --system >/dev/null",
    ])


def reapplied_command() -> str:
    """File already correct but live values drifted: re-apply, don't rewrite."""
    return "sudo -n sysctl --system >/dev/null"


def autoreboot_enabled(cluster_config: object) -> bool:
    """Opt-out reader: node_recovery.autoreboot, default ON when absent.

    Pure function over the parsed cluster.json dict, so the bootstrap stage,
    the doctor card, and tests all answer the question from one implementation.
    Unknown keys in the block are ignored (older consumers stay compatible).
    A non-bool value (string, number, null) is honoured via bool() rather than
    silently defaulting — an operator who wrote something explicit gets it
    treated as explicit.
    """
    if not isinstance(cluster_config, dict):
        return True
    block = cluster_config.get("node_recovery")
    if not isinstance(block, dict) or "autoreboot" not in block:
        return True
    return bool(block["autoreboot"])


def read_cluster_config(hscc_dir: str | None = None) -> dict:
    """Load cluster.json from the HSCC dir. Missing/unparseable -> {} (=> default ON).

    ``hscc_dir`` defaults to ``$HSCC_DIR`` then ``~/.hscc`` — never a baked
    home path, so an isolated bootstrap run reads ITS OWN config.
    """
    base = hscc_dir or os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")
    path = os.path.join(base, "cluster.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _default_ssh(host: str, command: str, timeout: int = 60) -> dict:
    """Fleet transport: hscc-cluster.clusterlib.ssh_cmd (sparkrun owns the fleet).

    Resolves the ``hscc-cluster`` dir that sits beside this one — true in both
    the work repo and the deployed plugins layout — and routes through
    clusterlib -> sparkrun_bridge -> sparkrun's own run_remote_script. NO
    hand-rolled ssh anywhere. If the bridge cannot even be imported, that is
    an honest failure for every node, not a fabricated ok.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cluster_dir = os.path.join(os.path.dirname(here), "hscc-cluster")
    if cluster_dir not in sys.path:
        sys.path.insert(0, cluster_dir)
    try:
        import clusterlib  # noqa: PLC0415 - late on purpose (path set above)
    except ImportError as e:  # pragma: no cover - layout must provide it
        return {"ok": False, "stdout": "", "code": 127,
                "stderr": f"clusterlib unavailable ({e}); sparkrun bridge unreachable"}
    return clusterlib.ssh_cmd(host, command, timeout=timeout)


def install_node(host: str, *, _ssh=None, dry_run: bool = False,
                 enabled: bool | None = None,
                 cluster_config: dict | None = None) -> dict:
    """Ensure the drop-in on one node. Never raises; returns a result dict.

    ``action`` is one of:
      installed        — file written, sysctl applied, every key read back OK
      unchanged        — file and live values already exactly as desired
      live-reapplied   — file correct, live values had drifted, re-applied + verified
      dry-run          — what WOULD happen (diff included), nothing touched
      drift            — operator-edited file present: reported, NOT clobbered
      skipped-disabled — node_recovery.autoreboot is false
      failed           — transport/sudo/read-back failure; ``error`` says which
    """
    ssh = _ssh or _default_ssh
    if enabled is None:
        enabled = autoreboot_enabled(
            cluster_config if cluster_config is not None else read_cluster_config())
    if not enabled:
        return {"host": host, "action": "skipped-disabled", "changed_keys": [],
                "skipped_keys": [], "error": None,
                "diff": "", "note": "node_recovery.autoreboot is false"}

    p = ssh(host, probe_command(), timeout=60)
    if not p.get("ok"):
        return {"host": host, "action": "failed", "changed_keys": [],
                "skipped_keys": [], "error": f"probe failed: {p.get('stderr') or p.get('code')}",
                "diff": "", "note": ""}
    state = parse_probe(p.get("stdout", ""))
    knobs = state["knobs"]
    current_file = state["file"]

    if HARDLOCKUP_KEY not in knobs:
        has_hl = False
        skipped = [HARDLOCKUP_KEY]
    else:
        has_hl = True
        skipped = []
    # Anything else that is missing is skipped with a note, never a node failure.
    wanted = {k: v for k, v in MANAGED_KEYS.items() if k in knobs}
    skipped += [k for k in MANAGED_KEYS if k not in knobs]
    if has_hl:
        wanted[HARDLOCKUP_KEY] = HARDLOCKUP_VALUE

    desired = build_dropin(has_hl, skip=tuple(k for k in skipped if k != HARDLOCKUP_KEY))
    file_matches = current_file == desired

    live_mismatch = {k: (v, knobs.get(k)) for k, v in wanted.items()
                     if knobs.get(k) != v}

    # A file byte-identical to one of OUR canonical variants is ours, not an
    # operator edit: e.g. the kernel later exposed hardlockup_panic and the
    # old managed file predates it. That upgrades; anything else foreign is
    # drift. Only the two exact canonical contents qualify — a subset or a
    # tweaked value is operator intent and is never rewritten.
    ours_older_variant = (not file_matches and current_file is not None
                          and current_file in (build_dropin(True), build_dropin(False)))

    if dry_run:
        if file_matches and not live_mismatch:
            diff = "(no change — file and live values already match)"
        else:
            diff = _unified(current_file, desired, live_mismatch, wanted)
        return {"host": host, "action": "dry-run", "changed_keys": sorted(wanted),
                "skipped_keys": skipped, "error": None, "diff": diff, "note": ""}

    if file_matches and not live_mismatch:
        return {"host": host, "action": "unchanged", "changed_keys": [],
                "skipped_keys": skipped, "error": None, "diff": "", "note": ""}

    if current_file is not None and not file_matches and not ours_older_variant:
        # Operator edited the managed file: report drift, NEVER clobber, and do
        # not sysctl --system either (their file, their values — the doctor
        # card surfaces this red until the operator resolves it).
        return {"host": host, "action": "drift", "changed_keys": [],
                "skipped_keys": skipped, "error": None,
                "diff": _unified(current_file, desired, {}, {}),
                "note": f"{DROPIN_PATH} exists with foreign content — left untouched"}

    if file_matches:
        cmd = reapplied_command()
        action = "live-reapplied"
    else:
        cmd = write_command(desired)
        action = "installed"

    w = ssh(host, cmd, timeout=120)
    if not w.get("ok"):
        err = (w.get("stderr") or w.get("stdout") or f"exit {w.get('code')}").strip()
        if "password" in err.lower() or "sudo" in err.lower():
            err += " — sudo -n was refused on this node (grant NOPASSWD or run as root)"
        return {"host": host, "action": "failed", "changed_keys": [],
                "skipped_keys": skipped,
                "error": f"write/apply failed: {err}", "diff": "", "note": ""}

    # READ BACK — the arbiter, on every write path. A mismatch fails the node.
    rb = ssh(host, probe_command(), timeout=60)
    if not rb.get("ok"):
        return {"host": host, "action": "failed", "changed_keys": [],
                "skipped_keys": skipped,
                "error": f"read-back probe failed: {rb.get('stderr') or rb.get('code')}",
                "diff": "", "note": ""}
    live = parse_probe(rb.get("stdout", ""))["knobs"]
    bad = {k: (v, live.get(k)) for k, v in wanted.items() if live.get(k) != v}
    if bad:
        detail = "; ".join(f"{k}: expected {exp!r} got {act!r}"
                           for k, (exp, act) in sorted(bad.items()))
        return {"host": host, "action": "failed", "changed_keys": [],
                "skipped_keys": skipped,
                "error": f"read-back mismatch — {detail}",
                "diff": "", "note": "sysctl --system did not make the value stick"}
    return {"host": host, "action": action, "changed_keys": sorted(wanted),
            "skipped_keys": skipped, "error": None, "diff": "",
            "note": "all keys verified live"}


def _unified(old: str | None, new: str, live_mismatch: dict, wanted: dict) -> str:
    """Human-readable per-node diff for dry-run / drift reports (no difflib noise)."""
    lines = []
    if old is None:
        lines.append(f"(new file) {DROPIN_PATH}")
        lines += [f"+{line}" for line in new.rstrip("\n").splitlines()]
    else:
        import difflib
        lines.extend(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=DROPIN_PATH + " (current)", tofile=DROPIN_PATH + " (desired)",
            lineterm=""))
    for k, (exp, act) in sorted(live_mismatch.items()):
        if k in wanted:
            lines.append(f"live: {k} = {act!r} -> {exp!r}")
    return "\n".join(lines)


def install_fleet(hosts: list[str], *, _ssh=None, dry_run: bool = False,
                  hscc_dir: str | None = None,
                  _config_loader=None) -> dict:
    """Install across *hosts*. Honors the node_recovery.autoreboot opt-out once.

    Returns {results: [per-node dicts], ok: bool, skipped_reason: str|None}.
    One node failing never suppresses the others.
    """
    config = (_config_loader or read_cluster_config)(hscc_dir)
    if not autoreboot_enabled(config):
        return {"results": [], "ok": True,
                "skipped_reason": "node_recovery.autoreboot is false (opt-out in cluster.json)"}
    results = [install_node(h, _ssh=_ssh, dry_run=dry_run, enabled=True)
               for h in hosts]
    return {"results": results, "ok": all(r["action"] != "failed" for r in results),
            "skipped_reason": None}


def _hosts_from_detect() -> list[str]:
    """Cluster hosts via detect.py (sparkrun cluster list) — bootstrap's own path."""
    boot_dir = os.path.dirname(os.path.abspath(__file__))
    if boot_dir not in sys.path:
        sys.path.insert(0, boot_dir)
    try:
        import detect  # noqa: PLC0415
    except ImportError:
        return []
    cluster = detect.detect_cluster()
    return list(cluster.get("hosts") or []) if cluster else []


def main(argv=None) -> int:
    """CLI used by bootstrap.sh. Prints one JSON summary line; exit 0 iff no node failed."""
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    hosts: list[str] = []
    if "--hosts" in argv:
        i = argv.index("--hosts")
        hosts = [h for h in argv[i + 1:] if not h.startswith("--")]
    if not hosts:
        hosts = _hosts_from_detect()
    if not hosts:
        print(json.dumps({"results": [], "ok": False, "skipped_reason": None,
                          "error": "no hosts: pass --hosts or configure a sparkrun cluster"}))
        return 2
    summary = install_fleet(hosts, dry_run=dry_run)
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
