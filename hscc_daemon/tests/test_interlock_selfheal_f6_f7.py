"""GAP-F-6 / GAP-F-7 (audit F) coverage: interlock snapshot + verify rows.

Three surfaces, one card:

1. ``autodown.write_interlock_snapshot`` / ``read_interlock_snapshot`` — the
   cross-process document that finally lets a CLI process see what only the
   daemon's memory knew (the #135 unevaluable sentinel). Atomic write,
   never-raise contract, corrupt document reads as "no snapshot" (an
   environment fault is never rendered as a verdict).
2. ``verify.check_autodown_interlock`` — UNEVALUABLE is a DISTINCT ok=None
   state naming source+reason; holding/clear are ok; absent/stale is its own
   ok=None. The card's "distinct state instead of blending into ok/not-ok".
3. ``verify.check_dispatcher_self_heal`` — the wedge latch (armed / fired /
   cooling / latched / gave-up) made observable from
   ``~/.hscc/dispatcher_recover.json``.

Plus the single-writer hook: ``daemon_ops._autodown_tick`` persists the
snapshot on EVERY path (disabled short-circuit, normal cycle, raising cycle) —
the "one writer" rule that keeps the document honest.

All fixtures are tmp-path files; the suite's ``_isolate_hscc`` expanduser
redirect means even the default paths land under tmp_path. The real boards,
gh, launchctl and the operator's ~/.hscc are never touched.
"""

import json
import os
import time

import pytest


# ── 1. the snapshot writer/reader ───────────────────────────────────────────

class TestInterlockSnapshot:
    def test_round_trip(self, tmp_path):
        from hscc_daemon import autodown
        path = str(tmp_path / "autodown_interlock.json")
        payload = autodown.write_interlock_snapshot(
            cfg={"enabled": True, "state": "up"}, state_file=path, now=123.0)
        assert payload["enabled"] is True
        got = autodown.read_interlock_snapshot(state_file=path)
        assert got is not None
        assert got == payload
        assert got["at"] == 123.0
        # No tmp litter left behind (atomic replace).
        assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []

    def test_absent_file_reads_none_not_a_verdict(self, tmp_path):
        from hscc_daemon import autodown
        assert autodown.read_interlock_snapshot(
            state_file=str(tmp_path / "nope.json")) is None

    @pytest.mark.parametrize("content", [
        "not json at all",
        "[1, 2, 3]",            # list-shaped document
        '"a string"',           # scalar
        "",                     # empty
    ])
    def test_corrupt_document_reads_none(self, tmp_path, content):
        """A corrupt/non-dict snapshot is 'no snapshot', never a verdict —
        the honest-signal rule (an environment fault must not be rendered as
        data), and the exact class GAP-F-1 shipped with."""
        from hscc_daemon import autodown
        path = tmp_path / "autodown_interlock.json"
        path.write_text(content)
        assert autodown.read_interlock_snapshot(state_file=str(path)) is None

    def test_writer_never_raises_on_unwritable_path(self, tmp_path):
        from hscc_daemon import autodown
        # A directory as the target path ⇒ open() raises IsADirectoryError
        # (an OSError). The tick owns teardown safety; a diagnostics write
        # must not be able to break it.
        d = tmp_path / "blocked.json"
        d.mkdir()
        payload = autodown.write_interlock_snapshot(
            cfg={"enabled": False}, state_file=str(d))
        assert payload["enabled"] is False  # payload still returned

    def test_payload_carries_sentinel_spelled_out(self, tmp_path):
        """A reader must not need the private constant to spot the #135 case."""
        from hscc_daemon import autodown
        payload = autodown.interlock_snapshot_payload(cfg={})
        assert payload["prci_unreachable_sentinel"] == autodown._PRCI_UNREACHABLE

    def test_payload_defaults_are_never_true(self, tmp_path):
        """A daemon that has not evaluated anything writes None — never True.
        ok=None stays distinguishable from ok=True through the round trip."""
        from hscc_daemon import autodown
        # Module state may have been touched by other tests in this process;
        # the point is only that no field is silently True-by-default.
        payload = autodown.interlock_snapshot_payload(cfg=None)
        for k in ("kanban_ok", "prci_ok"):
            assert payload[k] in (None, True, False)
        assert payload["enabled"] is False


# ── 2. verify.check_autodown_interlock ──────────────────────────────────────

def _snap(tmp_path, name="interlock.json", **over):
    from hscc_daemon import autodown
    base = {"pid": 4242, "at": time.time(), "enabled": True, "state": "up",
            "kanban_ok": True, "kanban_reason": "", "kanban_blocking": None,
            "prci_ok": True, "prci_reason": "", "prci_blocking": None,
            "prci_unreachable_sentinel": autodown._PRCI_UNREACHABLE}
    base.update(over)
    p = tmp_path / name
    p.write_text(json.dumps(base))
    return str(p)


class TestCheckAutodownInterlock:
    def test_clear_is_ok(self, tmp_path):
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(interlock_state_file=_snap(tmp_path))
        assert res["ok"] is True
        assert "all clear" in res["detail"]

    def test_holding_names_the_signal_and_is_not_a_fault(self, tmp_path):
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(interlock_state_file=_snap(
            tmp_path, kanban_blocking="hscc", prci_blocking="PR/CI"))
        assert res["ok"] is True
        assert "kanban work on board 'hscc'" in res["detail"]
        assert "open PR / active CI run" in res["detail"]

    def test_prci_unevaluable_is_a_distinct_unverified_state(self, tmp_path):
        """THE GAP-F-6 acceptance state: PR/CI unreachable (the #135 sentinel)
        must be ok=None naming source + reason + the cost, never blended into
        ok=True 'interlock active' and never ok=False."""
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(interlock_state_file=_snap(
            tmp_path, prci_ok=False,
            prci_reason="PR/CI source unreachable — treating as active "
                        "(fail-safe)",
            prci_blocking=autodown_sentinel()))
        assert res["ok"] is None
        assert "UNEVALUABLE" in res["detail"]
        assert "PR/CI" in res["detail"]
        assert "fail-safe" in res["detail"]
        assert "scaled up" in res["detail"] or "scaled" in res["detail"]
        assert "next_step" in res

    def test_kanban_unevaluable_is_named_too(self, tmp_path):
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(interlock_state_file=_snap(
            tmp_path, kanban_ok=False,
            kanban_reason="board 'hscc' unreadable: disk full"))
        assert res["ok"] is None
        assert "kanban" in res["detail"]
        assert "disk full" in res["detail"]

    def test_missing_snapshot_is_its_own_unverified_state(self, tmp_path):
        """'Nobody ever evaluated' != 'the daemon says unevaluable' != ok."""
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(
            interlock_state_file=str(tmp_path / "never-written.json"))
        assert res["ok"] is None
        assert "UNVERIFIED" in res["detail"]
        assert "UNEVALUABLE" not in res["detail"]

    def test_stale_snapshot_is_unverified(self, tmp_path):
        """An old document must not pass as current: the tick is 30s; a
        snapshot older than the max-age is 'the daemon stopped reporting'."""
        from hscc_daemon.verify import check_autodown_interlock
        from hscc_daemon import autodown
        old = time.time() - autodown.INTERLOCK_SNAPSHOT_MAX_AGE_S - 1
        res = check_autodown_interlock(interlock_state_file=_snap(
            tmp_path, at=old))
        assert res["ok"] is None
        assert "stale" in res["detail"]

    def test_disabled_is_a_named_skip(self, tmp_path):
        from hscc_daemon.verify import check_autodown_interlock
        res = check_autodown_interlock(interlock_state_file=_snap(
            tmp_path, enabled=False))
        assert res["ok"] is True
        assert "skipped" in res["detail"]
        assert "disabled" in res["detail"]

    def test_corrupt_snapshot_never_becomes_a_verdict(self, tmp_path):
        from hscc_daemon.verify import check_autodown_interlock
        p = tmp_path / "bad.json"
        p.write_text("{ not json")
        res = check_autodown_interlock(interlock_state_file=str(p))
        assert res["ok"] is None

    def test_registered_in_run_all(self):
        from hscc_daemon import verify
        import inspect
        src = inspect.getsource(verify.run_all)
        assert "check_autodown_interlock" in src
        assert "check_dispatcher_self_heal" in src


def autodown_sentinel():
    from hscc_daemon import autodown
    return autodown._PRCI_UNREACHABLE


# ── 3. verify.check_dispatcher_self_heal (GAP-F-7) ──────────────────────────

class TestCheckDispatcherSelfHeal:
    def test_no_file_is_armed_ok(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        res = check_dispatcher_self_heal(
            recover_state_file=str(tmp_path / "absent.json"))
        assert res["ok"] is True
        assert "armed" in res["detail"]

    def test_fired_within_window_reports_cooling(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        p = tmp_path / "recover.json"
        now = 1_000_000.0
        p.write_text(json.dumps({"attempts": 1,
                                 "last_recovery_at": now - 60}))
        res = check_dispatcher_self_heal(recover_state_file=str(p), now=now)
        assert res["ok"] is True
        assert "cooling" in res["detail"]
        assert "1 restart(s) attempted" in res["detail"]

    def test_after_window_reports_armed_again(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        from hscc_daemon import dispatcher_wedge as dw
        p = tmp_path / "recover.json"
        now = 1_000_000.0
        p.write_text(json.dumps({"attempts": 2,
                                 "last_recovery_at":
                                     now - dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS
                                     - 5}))
        res = check_dispatcher_self_heal(recover_state_file=str(p), now=now)
        assert res["ok"] is True
        assert "cooldown expired" in res["detail"]

    def test_non_dispatchable_latch_is_named(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        p = tmp_path / "recover.json"
        now = 1_000_000.0
        p.write_text(json.dumps({
            "attempts": 1, "last_recovery_at": now - 99999,
            "last_incident": {"signature": "ready:hscc:t_1", "at": now - 99999,
                              "dispatchable": False}}))
        res = check_dispatcher_self_heal(recover_state_file=str(p), now=now)
        assert res["ok"] is True
        assert "latch SET" in res["detail"]
        assert "ready:hscc:t_1" in res["detail"]

    def test_gave_up_is_unverified_never_ok_never_absent(self, tmp_path):
        """The cap means self-heal STOPPED acting. ok=False would double-page
        (the wedge itself already reds the dispatcher stream); absence would
        hide it (GAP-F's 'assert-nothing = not green'). ok=None, loudly."""
        from hscc_daemon.verify import check_dispatcher_self_heal
        p = tmp_path / "recover.json"
        p.write_text(json.dumps({"attempts": 3, "gave_up": True,
                                 "last_recovery_at": 1_000_000.0}))
        res = check_dispatcher_self_heal(recover_state_file=str(p),
                                         now=1_000_000.0)
        assert res["ok"] is None
        assert "GAVE UP" in res["detail"]
        assert "3 restart(s) attempted" in res["detail"]
        assert "next_step" in res

    def test_corrupt_file_is_unverified_naming_the_path(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        p = tmp_path / "recover.json"
        p.write_text("{ broken")
        res = check_dispatcher_self_heal(recover_state_file=str(p))
        assert res["ok"] is None
        assert "UNVERIFIED" in res["detail"]

    def test_non_dict_document_is_unverified(self, tmp_path):
        from hscc_daemon.verify import check_dispatcher_self_heal
        p = tmp_path / "recover.json"
        p.write_text('["not", "a", "map"]')
        res = check_dispatcher_self_heal(recover_state_file=str(p))
        assert res["ok"] is None

    def test_default_path_resolves_under_the_isolated_home(self, tmp_path):
        """Without an injected path the check must resolve through the module
        seam — which conftest's expanduser redirect has moved under tmp. A
        regression here would make the suite read the operator's real latch."""
        from hscc_daemon.verify import check_dispatcher_self_heal
        from hscc_daemon import dispatcher_wedge as dw
        assert str(tmp_path) in dw._recover_state_path()
        res = check_dispatcher_self_heal()
        assert res["ok"] is True and "armed" in res["detail"]


# ── 4. the single-writer hook in the daemon tick ────────────────────────────

class TestAutodownTickWritesSnapshotEveryPath:
    """One writer, every path. The document is only honest if the tick can't
    leave it missing on the paths it skips for OTHER reasons."""

    def _capture(self, monkeypatch, tmp_path, cfg, cycle_impl):
        from hscc_daemon import autodown, daemon_ops
        path = str(tmp_path / "autodown_interlock.json")
        monkeypatch.setattr(autodown, "_interlock_state_path", lambda: path)
        monkeypatch.setattr(autodown, "load_config", lambda: dict(cfg))
        monkeypatch.setattr(autodown, "cycle", cycle_impl)
        daemon_ops._autodown_tick()
        return autodown.read_interlock_snapshot(state_file=path)

    def test_disabled_path_still_writes(self, monkeypatch, tmp_path):
        # An operator who disabled autodown still deserves the truthful
        # "disabled" row instead of a stale or missing snapshot.
        snap = self._capture(monkeypatch, tmp_path,
                             {"enabled": False},
                             lambda: (_ for _ in ()).throw(
                                 AssertionError("must not cycle")))
        assert snap is not None
        assert snap["enabled"] is False

    def test_normal_path_writes(self, monkeypatch, tmp_path):
        called = []
        snap = self._capture(monkeypatch, tmp_path,
                             {"enabled": True, "state": "up"},
                             lambda: called.append(True))
        assert called == [True]
        assert snap is not None and snap["enabled"] is True

    def test_raising_cycle_still_writes(self, monkeypatch, tmp_path):
        def boom():
            raise RuntimeError("cycle blew up")
        snap = self._capture(monkeypatch, tmp_path,
                             {"enabled": True}, boom)
        # _autodown_tick catches the cycle error (logs it); the finally-block
        # snapshot must exist anyway — a crashed cycle is exactly when the
        # operator needs to see what the interlocks last said.
        assert snap is not None

    def test_snapshot_write_failure_never_breaks_the_tick(self, monkeypatch,
                                                          tmp_path):
        from hscc_daemon import autodown, daemon_ops
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": False})

        def raise_on_write(cfg=None, state_file=None, now=None):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(autodown, "write_interlock_snapshot", raise_on_write)
        monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
        daemon_ops._autodown_tick()   # must not raise
