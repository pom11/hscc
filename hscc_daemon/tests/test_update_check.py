"""`hscc update --check` (card t_a4055572) — version check + notices.

ZERO network calls: every fetch goes through an injected stub
(``run_check(fetch=...)`` / ``daemon_tick(fetch=...)``) or a monkeypatched
transport. If a real socket ever opens here, the assertion in
``test_fetch_never_touches_the_network_under_test`` fails first.

The loud/quiet split the card demands, documented per surface (the body
explicitly asks for this table in tests):

  SURFACE                       ON NEWER           ON FETCH FAILURE
  hscc update --check (verb)    stdout + rc=0      stderr line, rc=1 (LOUD)
  hscc status (notice)          one INFO line      nothing (QUIET)
  daemon tick (log line)        WARN once/day      nothing at ERROR (QUIET)

Cache rules pinned here: newer/older/equal tags; brew/dev/unknown upgrade
strings; cache expiry at the TTL boundary; malformed JSON; unwritable cache;
failed check does NOT poison the cache; status with a >24h cache prints
nothing.
"""
import datetime
import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import hscc_daemon.hscc as h
import hscc_daemon.update_check as uc

UTC = datetime.timezone.utc


def _ts(hours_ago=0.0):
    return (datetime.datetime.now(UTC)
            - datetime.timedelta(hours=hours_ago)).isoformat()


def _stub_fetch(tag):
    def fetch():
        return tag
    return fetch


def _boom(exc=None):
    def fetch():
        raise (exc or uc.UpdateCheckError("offline (test)"))
    return fetch


# ── network fence ──────────────────────────────────────────────────────────

def test_fetch_never_touches_the_network_under_test(monkeypatch):
    """The whole suite is fabricated: urllib and subprocess must be inert."""
    def _no_socket(*a, **k):
        raise AssertionError("a test opened a real network path")

    monkeypatch.setattr(uc.urllib.request, "urlopen", _no_socket)
    monkeypatch.setattr(uc.shutil, "which", lambda name: None)  # no gh
    res = uc.run_check(fetch=_stub_fetch("v9.9.9"))
    assert res["ok"] and res["available"]


# ── version comparison ─────────────────────────────────────────────────────

class TestVersionCompare:
    @pytest.mark.parametrize("installed,latest,want", [
        ("2.12.0", "v2.13.0", -1),   # newer published
        ("2.13.0", "v2.13.0", 0),    # equal, v-prefix differences aside
        ("2.13.0", "v2.12.0", 1),    # dev checkout ahead of the last release
        ("2.9.0", "v2.10.0", -1),    # numeric, NOT lexical (t_bc5a3fee trap)
        ("2.13", "v2.13.0", 0),      # padding: a tag cannot beat its own release
    ])
    def test_orders(self, installed, latest, want):
        assert uc.version_compare(installed, latest) == want

    def test_unparseable_raises_unknown_not_error(self):
        with pytest.raises(uc.UnknownVersion):
            uc.version_compare("nightly-abc", "v2.13.0")

    def test_display_tag_single_v(self):
        assert uc.display_tag("v2.13.0") == "v2.13.0"
        assert uc.display_tag("2.13.0") == "v2.13.0"


# ── install mode + upgrade command ─────────────────────────────────────────

class TestInstallMode:
    def test_brew_marker_primary(self, tmp_path):
        root = tmp_path / "prefix" / "libexec" / "hscc"
        root.mkdir(parents=True)
        (root / ".hscc-brew-libexec").write_text("brew\n")
        assert uc.detect_install_mode(root) == uc.MODE_BREW
        assert uc.upgrade_command(uc.MODE_BREW) == "brew upgrade hscc"

    def test_cellar_path_fallback_without_marker(self, tmp_path):
        root = tmp_path / "Cellar" / "hscc" / "2.12.0" / "libexec" / "hscc"
        root.mkdir(parents=True)
        assert uc.detect_install_mode(root) == uc.MODE_BREW

    def test_libexec_hscdash_path_fallback(self, tmp_path):
        root = tmp_path / "opt" / "homebrew-else" / "libexec" / "hscc-2.12.0"
        # "homebrew-else" must NOT match the bare-component test; the
        # libexec/hscc-* shape is what makes this brew.
        root.mkdir(parents=True)
        assert uc.detect_install_mode(root) == uc.MODE_BREW

    def test_dev_is_git_checkout(self, tmp_path):
        root = tmp_path / "checkout"
        (root / ".git").mkdir(parents=True)
        assert uc.detect_install_mode(root) == uc.MODE_DEV
        cmd = uc.upgrade_command(uc.MODE_DEV, source_root=root)
        assert cmd.startswith(f"git -C {root} pull")
        # t_bc5a3fee coordination: prefer the verb, keep the script fallback.
        assert "hscc bootstrap --yes" in cmd
        assert str(Path(root) / "hscc-bootstrap" / "bootstrap.sh") in cmd

    def test_unknown_prints_both_commands(self, tmp_path):
        root = tmp_path / "mystery"
        root.mkdir()
        assert uc.detect_install_mode(root) == uc.MODE_UNKNOWN
        cmd = uc.upgrade_command(uc.MODE_UNKNOWN)
        assert "brew upgrade hscc" in cmd and "git -C <repo> pull" in cmd

    def test_brew_tree_with_no_git_is_brew_not_dev(self, tmp_path):
        """Order pin: a brew tree looks like a release checkout with no .git.

        If dev were tested first, a brew install would be told to run
        `git pull` in a directory that is not a repo.
        """
        root = tmp_path / "libexec" / "hscc"
        root.mkdir(parents=True)
        (root / "libexec").mkdir()
        assert uc.detect_install_mode(root) == uc.MODE_BREW
        assert "git -C" not in uc.upgrade_command(uc.MODE_BREW)

    def test_brew_marker_beats_a_git_directory(self, tmp_path):
        """The ORDER pin with teeth: a brew tree that ALSO carries a .git
        (a tap staged from a local clone, a stray vendored checkout) must
        still read as brew. If dev were consulted first, this install would
        be told to `git pull` a tree brew owns — and the brew upgrade
        command would never be printed."""
        root = tmp_path / "opt" / "custom-prefix" / "libexec" / "hscc"
        (root / ".git").mkdir(parents=True)
        (root / ".hscc-brew-libexec").write_text("brew\n")
        assert uc.detect_install_mode(root) == uc.MODE_BREW
        assert uc.upgrade_command(uc.MODE_BREW) == "brew upgrade hscc"


# ── run_check: newer / older / equal + cache writes ───────────────────────

class TestRunCheck:
    def cache(self, tmp_path):
        return tmp_path / "hscc" / "update-check.json"

    def test_newer_tag_caches_and_announces(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = self.cache(tmp_path)
        res = uc.run_check(cache_file=cache, fetch=_stub_fetch("v2.13.0"))
        assert res["ok"] and res["available"] and res["cmp"] == -1
        entry = json.loads(cache.read_text())
        assert entry["latest_tag"] == "v2.13.0"
        assert entry["available"] is True
        assert entry["upgrade_cmd"]  # actionable string stored for status
        lines = uc.format_check_result(res)
        assert any("v2.13.0" in ln for ln in lines)
        assert any("installed: v2.12.0" == ln.split("  ")[-1].strip()
                   or "v2.12.0" in ln for ln in lines)

    def test_equal_tag_is_quiet_one_liner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        res = uc.run_check(cache_file=self.cache(tmp_path),
                           fetch=_stub_fetch("v2.12.0"))
        assert res["ok"] and not res["available"] and res["cmp"] == 0
        lines = uc.format_check_result(res)
        # Spec: "Up to date: quiet one-liner rc=0" — exactly one line.
        assert len(lines) == 1 and lines[0].startswith("up to date")

    def test_older_tag_reports_dev_newer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.13.0")
        res = uc.run_check(cache_file=self.cache(tmp_path),
                           fetch=_stub_fetch("v2.12.0"))
        assert res["ok"] and not res["available"] and res["cmp"] == 1
        assert "NEWER" in uc.format_check_result(res)[-1]

    def test_only_write_is_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        before = sorted(p.name for p in tmp_path.iterdir())
        uc.run_check(cache_file=self.cache(tmp_path),
                     fetch=_stub_fetch("v2.13.0"))
        after = sorted(p.name for p in tmp_path.iterdir())
        # Exactly one new top-level entry: the hscc dir (created as needed),
        # holding exactly one file — the cache. No state/, no log, no pid.
        assert after == before + ["hscc"]
        assert [p.name for p in (tmp_path / "hscc").iterdir()] == \
            ["update-check.json"]

    def test_failed_check_does_not_poison_cache(self, tmp_path, monkeypatch):
        """Spec: a failed check must not overwrite the good answer. The
        un-poisoned fresh cache is exactly what keeps the status notice
        alive; once it ages out, the next run retries the fetch."""
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = self.cache(tmp_path)
        uc.run_check(cache_file=cache, fetch=_stub_fetch("v2.13.0"))
        good = json.loads(cache.read_text())

        res = uc.run_check(cache_file=cache, fetch=_boom())
        assert not res["ok"]
        assert json.loads(cache.read_text()) == good      # byte-identical
        assert res["cached"]["latest_tag"] == "v2.13.0"   # in-memory salvage
        # The good answer is still inside its TTL, so no refetch is due yet
        # (that is the point of not poisoning) — and once it ages out, it is.
        assert uc.check_due(cache_file=cache) is False
        later = datetime.datetime.now(UTC) + datetime.timedelta(hours=25)
        assert uc.check_due(cache_file=cache, now=later) is True

    def test_failed_check_with_no_cache_leaves_nothing_and_retries(
            self, tmp_path, monkeypatch):
        """No cache + offline: no file appears, and the next run retries."""
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = self.cache(tmp_path)
        res = uc.run_check(cache_file=cache, fetch=_boom())
        assert not res["ok"] and res["cached"] is None
        assert not cache.exists()
        assert uc.check_due(cache_file=cache) is True

    def test_up_to_date_success_throttles_retry(self, tmp_path, monkeypatch):
        """A successful check caches 24h: check_due flips False until expiry."""
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = self.cache(tmp_path)
        uc.run_check(cache_file=cache, fetch=_stub_fetch("v2.12.0"))
        assert uc.check_due(cache_file=cache) is False
        later = datetime.datetime.now(UTC) + datetime.timedelta(hours=25)
        assert uc.check_due(cache_file=cache, now=later) is True

    def test_malformed_cache_json_reads_as_no_cache(self, tmp_path):
        cache = self.cache(tmp_path)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{not json at all")
        assert uc.load_cache(cache) is None
        assert uc.check_due(cache_file=cache) is True

    def test_unwritable_cache_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        # Parent is a FILE: the cache path can never be created.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file")
        res = uc.run_check(cache_file=blocker / "update-check.json",
                           fetch=_stub_fetch("v2.13.0"))
        assert res["ok"] and res["available"]      # answer still returned
        assert res["cache_written"] is False       # failure reported, not raised


# ── cache freshness boundary ───────────────────────────────────────────────

class TestCacheBoundary:
    def entry(self, dt):
        return {"checked_at": dt.isoformat(), "latest_tag": "v9.9.9",
                "installed": "2.12.0", "available": True,
                "upgrade_cmd": "brew upgrade hscc"}

    # The boundary is pinned against a FIXED `now` and a FIXED stamp, so the
    # test cannot drift by microseconds between building the entry and the
    # assertion (an `hours_ago`-computed stamp plus a later `now()` reads
    # 24.0000001h and fails for arithmetic reasons, not behavioural ones).
    NOW = datetime.datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)

    def _entry_at(self, **delta):
        return self.entry(self.NOW - datetime.timedelta(**delta))

    def test_fresh_inside_ttl(self):
        assert uc.cache_is_fresh(self._entry_at(hours=23), now=self.NOW) is True

    def test_boundary_exactly_ttl_counts_fresh(self):
        """<= 24h is 'fresh' per the spec's own wording ('cache age <= 24h')."""
        assert uc.cache_is_fresh(self._entry_at(hours=24), now=self.NOW) is True

    def test_stale_past_ttl(self):
        assert uc.cache_is_fresh(self._entry_at(hours=24, seconds=1),
                                 now=self.NOW) is False

    def test_future_stamp_never_usable(self):
        """A clock jump (or a writable cache) cannot mint freshness with a
        future timestamp — negative ages report 'not usable'."""
        assert uc.cache_is_fresh(self._entry_at(hours=-1), now=self.NOW) is False

    def test_missing_or_bad_stamp_is_not_fresh(self):
        assert uc.cache_is_fresh({"latest_tag": "v9"}) is False
        assert uc.cache_is_fresh({"checked_at": "yesterday",
                                  "latest_tag": "v9"}) is False


# ── the status notice (QUIET surface) ──────────────────────────────────────

class TestStatusNotice:
    """cmd_status() prints ONE line when the cache says newer and is fresh,
    nothing otherwise, and can never fail because of this feature.
    """

    def _render(self, monkeypatch, tmp_path, cache_entry, install="2.12.0"):
        from hscc_daemon import cli, daemon_ops, state as state_mod
        monkeypatch.setattr(uc, "installed_version", lambda: install)
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_path / "pid"))
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE",
                            str(tmp_path / "heartbeat"))
        cache = tmp_path / "hscc" / "update-check.json"
        if cache_entry is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(cache_entry))
        monkeypatch.setattr(uc, "CACHE_FILE", str(cache))
        # Any network path reachable from cmd_status would be a spec breach:
        monkeypatch.setattr(uc.urllib.request, "urlopen", _raise_socket)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_status()
        return out.getvalue()

    def test_fresh_newer_cache_adds_one_notice_line(self, monkeypatch, tmp_path):
        out = self._render(monkeypatch, tmp_path, {
            "checked_at": _ts(1), "latest_tag": "v2.13.0",
            "installed": "2.12.0", "available": True,
            "upgrade_cmd": "brew upgrade hscc"})
        assert "v2.13.0 available" in out
        assert "brew upgrade hscc" in out
        assert out.count("available") == 1
        assert "HSCC Daemon Status" in out

    def test_stale_cache_prints_nothing(self, monkeypatch, tmp_path):
        """Spec: status with a >24h cache prints nothing."""
        out = self._render(monkeypatch, tmp_path, {
            "checked_at": _ts(25), "latest_tag": "v2.13.0",
            "installed": "2.12.0", "available": True,
            "upgrade_cmd": "brew upgrade hscc"})
        assert "v2.13.0" not in out
        assert "update:" not in out
        assert "HSCC Daemon Status" in out   # status itself unaffected

    def test_no_cache_prints_nothing(self, monkeypatch, tmp_path):
        out = self._render(monkeypatch, tmp_path, None)
        assert "update:" not in out

    def test_cached_up_to_date_prints_nothing(self, monkeypatch, tmp_path):
        out = self._render(monkeypatch, tmp_path, {
            "checked_at": _ts(1), "latest_tag": "v2.12.0",
            "installed": "2.12.0", "available": False, "upgrade_cmd": ""})
        assert "update:" not in out

    def test_corrupt_cache_cannot_break_status(self, monkeypatch, tmp_path):
        """Malformed JSON must not print, not raise, not change status."""
        from hscc_daemon import cli
        cache = tmp_path / "hscc" / "update-check.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("]]not json[[")
        monkeypatch.setattr(uc, "CACHE_FILE", str(cache))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_status()
        text = out.getvalue()
        assert "HSCC Daemon Status" in text
        assert "not json" not in text
        assert "update:" not in text

    def test_unwritable_cache_status_unchanged(self, monkeypatch, tmp_path):
        from hscc_daemon import cli
        blocker = tmp_path / "blocker"
        blocker.write_text("file, not dir")
        monkeypatch.setattr(uc, "CACHE_FILE", str(blocker / "update-check.json"))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_status()
        assert "HSCC Daemon Status" in out.getvalue()

    def test_raising_notice_helper_cannot_take_status_down(
            self, monkeypatch, tmp_path):
        """The hard guarantee behind the try/except: even if check_from_cache
        itself explodes (a future refactor, a corrupted cache entry the
        defensive paths miss), `hscc status` still prints and still exits
        normally. Mutation-probed: removing the swallow fails this test."""
        from hscc_daemon import cli

        def detonate(*a, **k):
            raise RuntimeError("update_check exploded")
        monkeypatch.setattr(uc, "check_from_cache", detonate)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_status()          # must NOT raise
        assert "HSCC Daemon Status" in out.getvalue()
        assert "update:" not in out.getvalue()

    def test_notice_appears_even_with_no_stream_state(self, monkeypatch, tmp_path):
        """The early `return` for 'no state data yet' must not swallow the
        notice (a fresh install is exactly when an update notice matters)."""
        out = self._render(monkeypatch, tmp_path, {
            "checked_at": _ts(0), "latest_tag": "v2.13.0",
            "installed": "2.12.0", "available": True,
            "upgrade_cmd": "brew upgrade hscc"})
        assert "No state data yet" in out
        assert "v2.13.0 available" in out

    def test_status_never_fetches(self, monkeypatch, tmp_path):
        """Quiet surface: cmd_status makes ZERO network calls even when the
        cache is empty (the fetch is the verb's / daemon's job)."""
        out = self._render(monkeypatch, tmp_path, None)
        assert "HSCC Daemon Status" in out          # _render fenced urlopen


def _raise_socket(*a, **k):
    raise AssertionError("status/daemon made a network call")


# ── the daemon tick (QUIET surface, once/day) ──────────────────────────────

class TestDaemonTick:
    def logs(self):
        seen = []
        def log(msg, level="INFO"):
            seen.append((level, msg))
        return seen, log

    def test_newer_writes_warn_line_once_then_cache_guards(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        cache = tmp_path / "hscc" / "update-check.json"
        seen, log = self.logs()
        n = uc.daemon_tick(cache_file=cache, fetch=_stub_fetch("v2.13.0"), log_fn=log)
        assert n is not None
        assert ("WARN", n) in seen
        assert "v2.13.0 available" in n
        assert "installed v2.12.0" in n
        assert "upgrade: brew upgrade hscc" in n
        # Second tick: the cache says "checked recently" -> no fetch, no log.
        calls = []
        def counting_fetch():
            calls.append(1)
            return "v2.13.0"
        assert uc.daemon_tick(cache_file=cache, fetch=counting_fetch,
                              log_fn=log) is None
        assert calls == []
        assert len([s for s in seen if "available" in s[1]]) == 1

    def test_offline_is_quiet_and_retryable(self, tmp_path, monkeypatch):
        """Spec: offline -> status unchanged + NO daemon error-level line."""
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = tmp_path / "hscc" / "update-check.json"
        seen, log = self.logs()
        assert uc.daemon_tick(cache_file=cache, fetch=_boom(), log_fn=log) is None
        assert [lvl for lvl, _ in seen] == []          # nothing logged at all
        # And the failed attempt left no cache, so tomorrow's tick retries.
        assert not cache.exists()
        assert uc.check_due(cache_file=cache) is True

    def test_raising_tick_never_propagates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        def detonate():
            raise RuntimeError("injected transport bug")
        # No cache file, no log_fn patch: must return None, not raise, and
        # must not reach the daemon's real log.
        monkeypatch.setattr("hscc_daemon.daemon_ops.log", lambda *a, **k: None)
        assert uc.daemon_tick(cache_file=tmp_path / "nope.json",
                              fetch=detonate) is None

    def test_up_to_date_logs_info_never_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        cache = tmp_path / "hscc" / "update-check.json"
        seen, log = self.logs()
        uc.daemon_tick(cache_file=cache, fetch=_stub_fetch("v2.12.0"), log_fn=log)
        assert ("INFO", "HSCC is up to date (installed v2.12.0, latest v2.12.0)") in seen
        assert all(lvl != "WARN" for lvl, _ in seen)

    def test_tick_never_raises_outward_via_loop_guard(self, tmp_path, monkeypatch):
        """The loop-level guard: run_heartbeat_loop must survive a tick that
        raises, keep touching the heartbeat, and stop ticking on shutdown."""
        from hscc_daemon import daemon_ops
        hb = tmp_path / "heartbeat"
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(hb))
        monkeypatch.setattr(daemon_ops, "UPDATE_CHECK_STARTUP_DELAY", 0)
        ticks = []
        def exploding_tick():
            ticks.append(1)
            raise RuntimeError("tick exploded")
        stop = threading.Event()
        t = threading.Thread(target=daemon_ops.run_heartbeat_loop,
                             args=(stop,),
                             kwargs={"interval": 0.01, "update_interval": 0.01,
                                     "update_check_fn": exploding_tick},
                             daemon=True)
        t.start()
        time.sleep(0.15)
        stop.set()
        t.join(timeout=5)
        assert not t.is_alive()
        assert hb.exists(), "heartbeat stopped while ticks were exploding"
        assert ticks, "update tick never ran"


# ── the verb (LOUD surface) ────────────────────────────────────────────────

class TestUpdateVerb:
    def _run(self, monkeypatch, tmp_path, argv, tag="v2.13.0", install="2.12.0",
             fetch=None, mode=uc.MODE_BREW, source_root=None):
        monkeypatch.setattr(uc, "installed_version", lambda: install)
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: mode)
        if source_root is not None:
            monkeypatch.setattr(uc, "_source_root", lambda: source_root)
        monkeypatch.setattr(uc, "CACHE_FILE",
                            str(tmp_path / "hscc" / "update-check.json"))
        monkeypatch.setattr(sys, "argv", ["hscc", "update"] + argv)
        out, err = io.StringIO(), io.StringIO()
        rc = None
        if fetch is None:
            fetch = _stub_fetch(tag)
        monkeypatch.setattr(uc, "fetch_latest_release", fetch)
        with redirect_stdout(out), redirect_stderr(err):
            rc = h._handle_update()
        return rc, out.getvalue(), err.getvalue()

    def test_newer_prints_actionable_brew_command(self, monkeypatch, tmp_path):
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check"])
        assert rc == 0 and err == ""
        assert "installed: v2.12.0" in out
        assert "latest:    v2.13.0" in out
        assert "brew upgrade hscc" in out
        # NO AUTO-UPGRADE: nothing but the printed string may have happened.
        assert "Running" not in out

    def test_up_to_date_is_quiet_one_liner_rc0(self, monkeypatch, tmp_path):
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check"],
                                 tag="v2.12.0")
        assert rc == 0 and err == ""
        assert out.strip().startswith("up to date")
        # exactly ONE stdout line — no comparison block for a non-event
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 1

    def test_dev_mode_prints_git_pull_and_bootstrap(self, monkeypatch, tmp_path):
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check"],
                                 mode=uc.MODE_DEV, source_root=root)
        assert rc == 0
        assert f"git -C {root} pull" in out
        assert "hscc bootstrap --yes" in out

    def test_unknown_mode_prints_both(self, monkeypatch, tmp_path):
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check"],
                                 mode=uc.MODE_UNKNOWN)
        assert rc == 0
        assert "brew upgrade hscc" in out and "git -C <repo> pull" in out

    def test_offline_is_loud_stderr_rc1(self, monkeypatch, tmp_path):
        """The verb is the ONLY loud surface (card's ruling: rc=1 allowed)."""
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check"],
                                 fetch=_boom())
        assert rc == 1
        assert "update check failed" in err
        assert out == ""                    # failure belongs on stderr only

    def test_offline_still_shows_last_good_check(self, monkeypatch, tmp_path):
        cache = tmp_path / "hscc" / "update-check.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({
            "checked_at": _ts(1), "latest_tag": "v2.13.0",
            "installed": "2.12.0", "available": True,
            "upgrade_cmd": "brew upgrade hscc"}))
        monkeypatch.setattr(uc, "installed_version", lambda: "2.12.0")
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        monkeypatch.setattr(uc, "CACHE_FILE", str(cache))
        monkeypatch.setattr(sys, "argv", ["hscc", "update", "--check"])
        monkeypatch.setattr(uc, "fetch_latest_release", _boom())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = h._handle_update()
        assert rc == 1
        assert "last good check" in err.getvalue()
        assert "v2.13.0" in err.getvalue()
        assert cache.exists()               # good cache survived the failure

    def test_json_machine_shape(self, monkeypatch, tmp_path):
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check", "--json"])
        assert rc == 0
        payload = json.loads(out)
        assert payload["available"] is True
        assert payload["latest_tag"] == "v2.13.0"
        assert payload["ok"] is True

    def test_missing_check_flag_is_usage_error(self, monkeypatch, tmp_path):
        rc, out, err = self._run(monkeypatch, tmp_path, [])
        assert rc == 1 and "needs --check" in err

    def test_unknown_flag_refused_not_ignored(self, monkeypatch, tmp_path):
        """F6: a token the verb does not understand is a refusal (an argument
        typo must not silently run a different check)."""
        rc, out, err = self._run(monkeypatch, tmp_path, ["--check", "--force"])
        assert rc == 1 and "unknown argument" in err

    def test_help_token_never_reaches_verb(self, monkeypatch):
        """`hscc update --help` prints COMMAND_HELP and makes no network call
        (the DAEMON_COMMANDS intercept runs before dispatch)."""
        monkeypatch.setattr(sys, "argv", ["hscc", "update", "--help"])
        monkeypatch.setattr(uc.urllib.request, "urlopen", _raise_socket)
        monkeypatch.setattr(h, "_handle_update", lambda: pytest.fail(
            "dispatch reached the verb for a --help invocation"))
        with pytest.raises(SystemExit) as exc:
            h.main()
        assert exc.value.code == 0

    def test_help_wording_single_source(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "update"])
        assert h._handle_help() == 0
        printed = capsys.readouterr().out
        assert "hscc update --check" in printed
        assert h.COMMAND_HELP["update"] in printed


# ── the transport (fabricated; no live calls) ──────────────────────────────

class TestTransports:
    """gh-first, urllib-fallback: both fabricated. `_tag_from_http` is fed a
    canned payload; a 403 becomes the rate-limit hint; gh requires auth."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def test_http_path_parses_tag_name(self, monkeypatch):
        payload = json.dumps({"tag_name": "v2.13.0"}).encode()
        monkeypatch.setattr(uc.urllib.request, "urlopen",
                            lambda req, timeout=None: self._Resp(payload))
        assert uc._tag_from_http() == "v2.13.0"

    def test_http_403_becomes_rate_limit_hint(self, monkeypatch):
        import urllib.error
        def _403(req, timeout=None):
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        monkeypatch.setattr(uc.urllib.request, "urlopen", _403)
        with pytest.raises(uc.UpdateCheckError) as exc:
            uc._tag_from_http()
        assert "rate limited" in str(exc.value)
        assert "gh auth login" in str(exc.value)

    def test_http_garbage_json_is_update_check_error(self, monkeypatch):
        monkeypatch.setattr(uc.urllib.request, "urlopen",
                            lambda req, timeout=None: self._Resp(b"<html>"))
        with pytest.raises(uc.UpdateCheckError) as exc:
            uc._tag_from_http()
        assert "malformed" in str(exc.value).lower()

    def test_no_tag_name_field_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            uc.urllib.request, "urlopen",
            lambda req, timeout=None: self._Resp(b'{"message": "nope"}'))
        with pytest.raises(uc.UpdateCheckError):
            uc._tag_from_http()

    def test_gh_requires_auth_else_falls_back(self, monkeypatch):
        monkeypatch.setattr(uc.shutil, "which", lambda name: "/usr/bin/gh")
        calls = []
        monkeypatch.setattr(uc.subprocess, "run", lambda *a, **k:
                            calls.append(a[0]) or
                            subprocess.CompletedProcess(a[0], 1, "", "not login"))
        assert uc._gh_argv() is None          # unauthenticated gh is not used

    def test_fetch_prefers_authed_gh(self, monkeypatch):
        monkeypatch.setattr(uc, "_gh_argv",
                            lambda: ["gh", "api", "x", "--jq", ".tag_name"])
        monkeypatch.setattr(uc, "_tag_from_gh", lambda: "v9.0.0")
        monkeypatch.setattr(uc.urllib.request, "urlopen", _raise_socket)
        assert uc.fetch_latest_release() == "v9.0.0"   # urllib never touched

    def test_fetch_without_gh_uses_plain_https(self, monkeypatch):
        monkeypatch.setattr(uc.shutil, "which", lambda name: None)
        payload = json.dumps({"tag_name": "v2.13.0"}).encode()
        monkeypatch.setattr(uc.urllib.request, "urlopen",
                            lambda req, timeout=None: self._Resp(payload))
        assert uc.fetch_latest_release() == "v2.13.0"

    def test_timeout_constant_is_five_seconds(self):
        assert uc.FETCH_TIMEOUT_SECONDS == 5


# ── help surfaces inside the 78-column budget ──────────────────────────────

class TestHelpBudget:
    """The (B)-gate convention: every authored help line stays inside 78."""

    def test_command_help_update_lines_fit(self):
        for line in h.COMMAND_HELP["update"].splitlines():
            assert len(line) <= 76, f"{len(line)} > 76: {line!r}"

    def test_main_help_entry_line_fits(self):
        # _entry() puts usage at col 2 and the description at col _TOP_HELP_COL.
        usage = "update --check"
        desc = "Check GitHub releases for a newer version (read-only)"
        assert len(f"  {usage:<21}{desc}") <= 78


# ── module-level contract pins ─────────────────────────────────────────────

def test_cache_path_lives_under_hscss_dir():
    """The state-dir convention: the ONE file this feature writes sits in
    ~/.hscc. Checked against the SOURCE literal, because under the suite's
    autouse isolation fixture ``uc.CACHE_FILE`` is legitimately rebound to a
    per-test tmp dir (that redirect is what the leak guard relies on) — the
    shipped default is what this pins."""
    src = Path(uc.__file__).read_text()
    assert 'CACHE_FILE = "~/.hscc/update-check.json"' in src
    # and the code reads the constant, never a hardcoded absolute path
    assert 'open(os.path.expanduser("/' not in src


def test_no_auto_upgrade_surface_exists():
    """Static guard for the NO AUTO-UPGRADE rule (card body: "NO
    auto-upgrade"; "NEVER writes outside ~/.hscc").

    This is string-inspection, so it is deliberately narrow: it forbids the
    argv shapes a package manager would need, and pins the subprocess
    budget to the two gh-API probe sites. Prose that *mentions* brew or pip
    is not a call site, so the patterns here are argv-list shapes only.
    """
    src = Path(uc.__file__).read_text()
    for banned in ('"brew", "install"', '"brew", "upgrade"', '"pip", "install"',
                   '["brew"', '["pip"', "os.exec", "os.system", "os.spawn",
                   "shutil.rmtree"):
        assert banned not in src, f"auto-upgrade-ish call site: {banned}"
    # The only subprocess use allowed is the read-only gh release probe
    # (argv built in _gh_argv) + its `gh auth status` gate.
    assert src.count("subprocess.run") == 2, \
        "update_check may only shell out for the gh API probe (2 call sites)"
    # No import of any installer/service manager.
    for banned in ("import subprocess.run", "from shutil import move",
                   "launchd", "subprocess.Popen"):
        assert banned not in src, f"unexpected capability: {banned}"


def test_api_endpoint_is_the_releases_latest_route():
    assert uc.LATEST_RELEASE_API == (
        "https://api.github.com/repos/pom11/hscc/releases/latest")
