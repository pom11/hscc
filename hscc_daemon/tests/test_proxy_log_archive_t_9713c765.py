"""Pre-restart preservation of the outgoing litellm proxy log (t_9713c765).

The audit of the 2026-11-22 :4000 outage (§3.4) established that the
``--restart`` which fixed it destroyed the only record of it: sparkrun opens
``litellm.log`` in mode ``"w"`` on every launch. These tests pin the hscc-side
fallback — that a restart issued by hscc's own repair paths cannot take the
outgoing log with it.

Fakes only, by rule: a tmp dir with a fake log, no live proxy, no sparkrun
call. The conftest hermetic floor pins ``proxy_log_archive.SRC_OVERRIDE`` to a
nonexistent path, so a test that does not point it at its own file cannot read
the operator's live proxy log even by accident.
"""

import os
import re

import pytest

from hscc_daemon import health, proxy_log_archive as pla

#: The DECISION's name shape, allowing only the same-second ``-N`` disambiguator.
ARCHIVE_NAME_RE = re.compile(
    r"^litellm\.log\.\d{8}-\d{6}(-\d+)?\.pre-restart\.log$")

OUTAGE_BYTES = (b"INFO: Started server process [70001]\n"
                b"InternalServerError: OpenAIException - Connection error. "
                b"Received Model Group=orchestrator-model\n"
                b"INFO: 127.0.0.1 - \"POST /v1/chat/completions\" 500 Internal Server Error\n")


@pytest.fixture
def fake_proxy(tmp_path):
    """A tmp sparkrun-style proxy dir with one log carrying the outage lines."""
    proxy_dir = tmp_path / "sparkrun-proxy"
    proxy_dir.mkdir()
    log = proxy_dir / "litellm.log"
    log.write_bytes(OUTAGE_BYTES)
    dest = tmp_path / "proxy-logs"
    return {"dir": proxy_dir, "log": log, "dest": dest}


def _make_archives(dest, name, stamps, size=10):
    """Plant archives with strictly increasing mtimes, oldest stamp first."""
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for i, stamp in enumerate(stamps):
        p = dest / f"{name}.{stamp}.pre-restart.log"
        p.write_bytes(b"x" * size)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        made.append(p)
    return made


# --------------------------------------------------------------------------
# (a) the copy exists and holds the pre-restart bytes
# --------------------------------------------------------------------------

def test_copy_holds_the_pre_restart_bytes(fake_proxy):
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(fake_proxy["dest"]))
    assert res["ok"] is True and res["action"] == "archived"
    archived = res["archived"]
    assert os.path.isfile(archived)
    # The whole point: the outage lines survived.
    assert open(archived, "rb").read() == OUTAGE_BYTES
    assert ARCHIVE_NAME_RE.match(os.path.basename(archived)), archived


def test_live_log_is_never_moved_truncated_or_unlinked(fake_proxy):
    """Contract: the live file is only ever opened for READ."""
    log = fake_proxy["log"]
    before = log.read_bytes()
    pla.archive_before_restart(src=str(log), dest_dir=str(fake_proxy["dest"]))
    assert log.is_file(), "the live log must still exist"
    assert log.read_bytes() == before, "the live log must be byte-identical"
    assert os.listdir(fake_proxy["dir"]) == ["litellm.log"], \
        "nothing may be written into sparkrun's dir"


def test_archive_lands_in_hsccs_own_dir_not_sparkruns(fake_proxy):
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(fake_proxy["dest"]))
    assert os.path.dirname(res["archived"]) == str(fake_proxy["dest"])


def test_partial_copy_is_never_left_visible_as_an_archive(fake_proxy, monkeypatch):
    """A name that exists means a copy that completed — so a failed copy must
    leave neither an archive nor its .part sibling behind."""
    def boom(src, dst):
        with open(dst, "wb") as fh:
            fh.write(b"half a log")           # exactly what a crash mid-copy leaves
        raise OSError("disk I/O error")
    monkeypatch.setattr(pla.shutil, "copyfile", boom)
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(fake_proxy["dest"]))
    assert res["ok"] is False and "disk I/O error" in res["error"]
    assert res["archived"] is None
    left = sorted(os.listdir(fake_proxy["dest"]))
    assert left == [], f"copy debris left behind: {left}"


def test_missing_log_is_a_no_op_not_a_failure(fake_proxy):
    res = pla.archive_before_restart(src=str(fake_proxy["dir"] / "gone.log"),
                                     dest_dir=str(fake_proxy["dest"]))
    assert res["ok"] is True and res["action"] == "absent"
    assert res["archived"] is None
    assert not fake_proxy["dest"].exists(), "no work -> no directory"


def test_empty_log_is_skipped(fake_proxy):
    log = fake_proxy["log"]
    log.write_bytes(b"")
    res = pla.archive_before_restart(src=str(log),
                                     dest_dir=str(fake_proxy["dest"]))
    assert res["ok"] is True and res["action"] == "empty"
    assert res["archived"] is None


def test_two_restarts_in_the_same_second_do_not_clobber_each_other(fake_proxy):
    """os.replace overwrites silently; the second archive must get its own name."""
    pla.archive_before_restart(src=str(fake_proxy["log"]),
                               dest_dir=str(fake_proxy["dest"]), keep=5)
    fake_proxy["log"].write_bytes(b"second outage\n")
    res2 = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                      dest_dir=str(fake_proxy["dest"]), keep=5)
    names = sorted(os.listdir(fake_proxy["dest"]))
    assert len(names) == 2, names
    assert all(ARCHIVE_NAME_RE.match(n) for n in names), names
    assert open(os.path.join(str(fake_proxy["dest"]),
                             os.path.basename(res2["archived"])), "rb").read() \
        == b"second outage\n"


def test_source_path_is_derived_from_the_config_path_hscc_already_resolves(tmp_path, monkeypatch):
    """No duplicated sparkrun path: the log is the sibling of the litellm config
    the dep-bump drift check already reads every tick."""
    monkeypatch.setattr(pla, "SRC_OVERRIDE", None)
    from hscc_daemon import proxy_router
    monkeypatch.setattr(proxy_router, "LITELLM_CONFIG_PATH",
                        str(tmp_path / "sparkrun-cache" / "proxy" / "litellm_config.yaml"))
    assert pla.proxy_log_path() == str(tmp_path / "sparkrun-cache" / "proxy" / "litellm.log")


# --------------------------------------------------------------------------
# (b) retention trims to N
# --------------------------------------------------------------------------

@pytest.mark.parametrize("keep,total", [(1, 6), (2, 6), (5, 6)])
def test_retention_trims_to_the_newest_n(fake_proxy, keep, total):
    dest = fake_proxy["dest"]
    stamps = [f"2026{11:02d}{11+i:02d}-00000{i}" for i in range(total)]
    _make_archives(dest, pla.PROXY_LOG_NAME, stamps)
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(dest), keep=keep)
    assert res["ok"] is True
    left = sorted(os.listdir(dest))
    assert len(left) == keep, left
    # The archive just copied is always among the survivors, and the survivors
    # are the NEWEST plants (last `keep-1` stamps), never the oldest.
    assert os.path.basename(res["archived"]) in left
    survivors_from_plants = [] if keep <= 1 else stamps[-(keep - 1):]
    expected = {f"{pla.PROXY_LOG_NAME}.{s}.pre-restart.log"
                for s in survivors_from_plants}
    assert set(left) - {os.path.basename(res["archived"])} == expected
    assert res["pruned"], "pruning must be reported"


def test_keep_zero_disables_archiving(fake_proxy):
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(fake_proxy["dest"]), keep=0)
    assert res["ok"] is True and res["action"] == "disabled"
    assert res["archived"] is None
    assert not fake_proxy["dest"].exists()


def test_size_cap_trims_from_the_oldest(fake_proxy):
    dest = fake_proxy["dest"]
    _make_archives(dest, pla.PROXY_LOG_NAME,
                   [f"2026111{i}-000000" for i in range(1, 4)], size=100)
    removed = pla.prune(str(dest), keep=99, cap_bytes=250)   # 3x100 + new 60 > 250
    assert len(removed) >= 1
    left = sorted(p.name for p in dest.iterdir())
    assert "litellm.log.20261111-000000.pre-restart.log" not in left


def test_size_cap_never_deletes_the_archive_just_made(fake_proxy):
    """A cap smaller than one log must not destroy the evidence it just copied."""
    res = pla.archive_before_restart(src=str(fake_proxy["log"]),
                                     dest_dir=str(fake_proxy["dest"]),
                                     keep=5, cap_bytes=1)
    assert res["ok"] is True and res["archived"]
    assert os.path.isfile(res["archived"]), "the newest archive survived the cap"


def test_prune_only_touches_matching_archives(fake_proxy):
    """Retention must not become a general-purpose cleaner in a shared dir."""
    dest = fake_proxy["dest"]
    _make_archives(dest, pla.PROXY_LOG_NAME, [f"2026111{i}-000000" for i in range(1, 5)])
    keep_me = dest / "litellm.log"                      # a live-named file
    keep_me.write_bytes(b"do not delete me\n")
    stranger = dest / "litellm.log.20261111-000000.notes.txt"
    stranger.write_bytes(b"not an archive\n")
    pla.prune(str(dest), keep=1, cap_bytes=0)           # cap 0 = size bound off
    assert keep_me.is_file() and stranger.is_file()
    assert len(list(dest.glob(f"*.{pla.ARCHIVE_SUFFIX}"))) == 1


def test_env_vars_drive_the_defaults(tmp_path, monkeypatch):
    """HSCC_PROXY_LOG_KEEP / _CAP_BYTES are read at import, the same convention
    as every other HSCC_* knob. HOME is sandboxed so the reloaded module's own
    PROXY_LOG_DIR lands in the sandbox, never in the operator's ~/.hscc
    (precedent: test_heal_breaker.py's reload test)."""
    import importlib
    home = tmp_path / "fake-home"
    (home / ".hscc").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HSCC_PROXY_LOG_KEEP", "2")
    monkeypatch.setenv("HSCC_PROXY_LOG_CAP_BYTES", "12345")
    import hscc_daemon.proxy_log_archive as mod
    importlib.reload(mod)
    try:
        assert mod.PROXY_LOG_KEEP == 2
        assert mod.PROXY_LOG_CAP_BYTES == 12345
        assert mod.PROXY_LOG_DIR == os.path.join(str(home), ".hscc", "proxy-logs")
    finally:
        monkeypatch.delenv("HSCC_PROXY_LOG_KEEP", raising=False)
        monkeypatch.delenv("HSCC_PROXY_LOG_CAP_BYTES", raising=False)
        importlib.reload(mod)          # restore real constants for other tests


# --------------------------------------------------------------------------
# (c) a failing copy must never block the repair — and must say so
# --------------------------------------------------------------------------

def _stub_repair(monkeypatch, seq):
    """Minimal harness for _proxy_repair_orphan: argv recorded, nothing real run."""
    monkeypatch.setattr(health, "_proxy_tracked_pid",
                        lambda: seq.append("tracked") or 222)
    monkeypatch.setattr(health, "_reap_orphan_proxy_listener",
                        lambda t: seq.append("reap") or True)
    monkeypatch.setattr(health, "_proxy_listener",
                        lambda: seq.append("listen") or (222, "litellm"))
    monkeypatch.setattr(health, "run_cmd",
                        lambda a, **k: seq.append(list(a)) or {"ok": True})
    monkeypatch.setattr(health, "_proxy_launch_env", lambda: {})
    return seq


def _argv_kinds(seq):
    kinds = []
    for entry in seq:
        if isinstance(entry, list) and "proxy" in entry:
            kinds.append("stop" if "stop" in entry else
                         "start" if "start" in entry else None)
        elif isinstance(entry, str):
            kinds.append(entry)
    return [k for k in kinds if k]


def test_repair_archives_before_it_touches_the_proxy(tmp_path, monkeypatch):
    """Order is the feature: an archive taken AFTER the launch is worthless."""
    log = tmp_path / "litellm.log"
    log.write_bytes(OUTAGE_BYTES)
    dest = tmp_path / "proxy-logs"
    monkeypatch.setattr(pla, "SRC_OVERRIDE", str(log))
    monkeypatch.setattr(pla, "PROXY_LOG_DIR", str(dest))
    seq = _stub_repair(monkeypatch, [])
    _install_archiving_probe(monkeypatch, seq)
    calls = []
    monkeypatch.setattr(health, "log",
                        lambda m, lvl="INFO": calls.append((lvl, m)))
    res = health._proxy_repair_orphan(["192.0.2.2"])
    assert res["ok"] is True
    kinds = _argv_kinds(seq)
    assert kinds.index("archive") < kinds.index("stop") < kinds.index("start"), kinds
    assert any(lvl == "INFO" and "archived before orphan repair" in m
               for lvl, m in calls), calls
    assert log.read_bytes() == OUTAGE_BYTES


def _install_archiving_probe(monkeypatch, seq):
    real = pla.archive_before_restart

    def probe(*a, **kw):
        seq.append("archive")
        return real(*a, **kw)

    monkeypatch.setattr(pla, "archive_before_restart", probe)


def test_simulated_sparkrun_truncation_cannot_erase_the_outage(fake_proxy, monkeypatch):
    """The audit scenario in miniature: repair, then let sparkrun do what it
    always does (open the log in "w") — the outage lines must still be there."""
    seq = _stub_repair(monkeypatch, [])
    monkeypatch.setattr(pla, "SRC_OVERRIDE", str(fake_proxy["log"]))
    monkeypatch.setattr(pla, "PROXY_LOG_DIR", str(fake_proxy["dest"]))
    _install_archiving_probe(monkeypatch, seq)
    monkeypatch.setattr(health, "log", lambda *a, **kw: None)
    res = health._proxy_repair_orphan(["192.0.2.2"])
    assert res["ok"] is True
    with open(fake_proxy["log"], "w"):      # exactly sparkrun's `_launch_background`
        pass
    assert fake_proxy["log"].read_bytes() == b"", "the truncation happened"
    archived = [p for p in fake_proxy["dest"].iterdir()
                if ARCHIVE_NAME_RE.match(p.name)]
    assert len(archived) == 1
    assert archived[0].read_bytes() == OUTAGE_BYTES


def test_failing_copy_still_restarts_and_warns(fake_proxy, monkeypatch):
    """Card rule: a lost log must not block recovery — but it must not be silent."""
    seq = _stub_repair(monkeypatch, [])
    monkeypatch.setattr(pla, "SRC_OVERRIDE", str(fake_proxy["log"]))
    monkeypatch.setattr(pla, "PROXY_LOG_DIR", str(fake_proxy["dest"]))

    def boom(src, dst):
        raise OSError("disk I/O error")

    monkeypatch.setattr(pla.shutil, "copyfile", boom)
    calls = []
    monkeypatch.setattr(health, "log",
                        lambda m, lvl="INFO": calls.append((lvl, m)))
    res = health._proxy_repair_orphan(["192.0.2.2"])
    kinds = _argv_kinds(seq)
    assert "stop" in kinds and "start" in kinds, "repair must still run"
    assert res["ok"] is True and res["verified"] is True
    warns = [m for lvl, m in calls if lvl == "WARN"]
    assert any("proxy log archive FAILED" in m and "restarting anyway" in m
               for m in warns), calls


def test_archiver_raising_still_restarts_and_warns(fake_proxy, monkeypatch):
    """The module promises never to raise; the seam assumes it might anyway."""
    seq = _stub_repair(monkeypatch, [])

    def explode(*a, **kw):
        raise RuntimeError("archiver exploded")

    monkeypatch.setattr(pla, "archive_before_restart", explode)
    calls = []
    monkeypatch.setattr(health, "log",
                        lambda m, lvl="INFO": calls.append((lvl, m)))
    res = health._proxy_repair_orphan(["192.0.2.2"])
    assert _argv_kinds(seq).count("start") == 1
    assert res["ok"] is True
    assert any(lvl == "WARN" and "proxy log archive FAILED" in m
               for lvl, m in calls), calls


def test_adopt_and_relaunch_paths_archive_first(tmp_path, monkeypatch):
    """The other two restart seams: `--restart` re-attach, and the stall relaunch."""
    log = tmp_path / "litellm.log"
    log.write_bytes(OUTAGE_BYTES)
    monkeypatch.setattr(pla, "SRC_OVERRIDE", str(log))
    monkeypatch.setattr(pla, "PROXY_LOG_DIR", str(tmp_path / "proxy-logs"))
    monkeypatch.setattr(health, "_proxy_launch_env", lambda: {})
    monkeypatch.setattr(health, "log", lambda *a, **kw: None)

    seq = []
    monkeypatch.setattr(health, "run_cmd",
                        lambda a, **k: seq.append(list(a)) or {"ok": True})
    _install_archiving_probe(monkeypatch, seq)
    monkeypatch.setattr(health, "_proxy_wait_gateway_listener",
                        lambda: ("gateway", 222, "litellm", 222))
    health._proxy_adopt_backends(["192.0.2.2"])
    assert seq[0] == "archive", "the adopt path must copy before --restart"
    assert any(isinstance(e, list) and "--restart" in e for e in seq)


def test_conftest_floor_keeps_every_other_test_off_the_live_log(monkeypatch):
    """The autouse pin: no test that forgets to fake a log can read the operator's."""
    assert pla.SRC_OVERRIDE and not os.path.exists(pla.SRC_OVERRIDE)
    res = pla.archive_before_restart(dest_dir=str(
        os.path.dirname(os.path.abspath(pla.SRC_OVERRIDE))))
    assert res["ok"] is True and res["action"] == "absent"
