"""Tests for the durable node-pause overlay (hscc_daemon/node_pauses.py).

Every case passes an explicit ``directory=`` temp-dir seam and uses
documentation-range fake hosts (192.0.2.0/24) — nothing here resolves a real
host, runs sparkrun, or reads/writes the operator's ``~/.hscc``.
"""

import json
import os
import subprocess
import sys

import pytest

from hscc_daemon import node_pauses

#: fixture-clock: per-case literals (2026-11-20/21), never a file-wide anchor —
#: each case names its own instant and passes it to the call under test
#: (``pause(..., now=...)``, ``pending_resume(now, ...)``), so ``node_pauses``'
#: wall-clock fallback (``_utc(None)``) is never what an assertion answers to.
#: The expiry cases that DO run on the wall clock build both ends from it
#: (``_hours_ago()``), which is the same rule seen from the other side: one
#: clock per case. Single-clock rule, card t_ed416771; enforced by
#: test_single_clock_rule.py.

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

HOST = "192.0.2.10"
PEER = "192.0.2.11"
OTHER = "192.0.2.12"
UNRELATED = "192.0.2.99"


@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root, returned as the ``directory=`` value."""
    root = tmp_path / "hscc"
    root.mkdir()
    return str(root)


@pytest.fixture
def warned(monkeypatch):
    """Collect every _warn() message so 'fail-loud' is an assertion."""
    messages = []
    monkeypatch.setattr(node_pauses, "_warn", lambda msg: messages.append(msg))
    return messages


def _raw(overlay):
    with open(node_pauses.overlay_path(overlay)) as f:
        return json.load(f)


def _plant(overlay, name, text):
    path = os.path.join(overlay, name)
    with open(path, "w") as f:
        f.write(text)
    return path


# ── Absence and corruption: never raise ────────────────────────────────────

class TestFailOpenOnAbsenceFailLoudOnCorruption:

    def test_missing_file_is_no_pauses_and_creates_nothing(self, overlay):
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert node_pauses.paused(HOST, overlay) is None
        assert node_pauses.list_pauses(overlay) == {}
        assert node_pauses.pending_resume(directory=overlay) == []
        assert os.listdir(overlay) == []  # a read must not leave a file behind

    def test_unreadable_file_is_no_pauses_with_a_warning(self, overlay, warned):
        # A directory where the overlay should be forces a real OSError on open
        # without needing a root-sensitive chmod.
        os.mkdir(node_pauses.overlay_path(overlay))
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert any("unreadable" in m for m in warned)

    def test_truncated_json_is_no_pauses_with_a_warning(self, overlay, warned):
        _plant(overlay, "node_pauses.json", '{"pauses": {"192.0.2.10": ')
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert node_pauses.paused(HOST, overlay) is None
        assert warned and any("unreadable" in m for m in warned)

    def test_non_object_document_is_no_pauses_with_a_warning(self, overlay,
                                                             warned):
        _plant(overlay, "node_pauses.json", '["not", "an", "object"]')
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert any("expected a JSON object" in m for m in warned)

    def test_non_object_pauses_block_is_no_pauses_with_a_warning(self, overlay,
                                                                 warned):
        _plant(overlay, "node_pauses.json", '{"pauses": [1, 2, 3]}')
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert any("'pauses'" in m for m in warned)

    def test_a_bad_entry_drops_only_that_host(self, overlay, warned):
        _plant(overlay, "node_pauses.json", json.dumps({
            "pauses": {HOST: "i am not an entry",
                       PEER: {"reason": "training", "until": None,
                              "units": ["u1"], "span_nodes": [],
                              "stopped": True}}}))
        pauses = node_pauses.list_pauses(overlay)
        assert HOST not in pauses
        assert pauses[PEER]["units"] == ["u1"]
        assert any(HOST in m for m in warned)

    def test_bad_host_key_is_dropped_not_used_as_a_real_host(self, overlay,
                                                             warned):
        _plant(overlay, "node_pauses.json", '{"pauses": {"": {"reason": "x"}}}')
        assert node_pauses.list_pauses(overlay) == {}
        assert any("non-string host key" in m or "host key" in m for m in warned)

    def test_empty_file_is_no_pauses_with_a_warning(self, overlay, warned):
        _plant(overlay, "node_pauses.json", "")
        assert node_pauses.load(overlay) == {"pauses": {}}
        assert warned


# ── The documented schema on disk ──────────────────────────────────────────

class TestSchemaOnDisk:

    def test_pause_writes_the_overlay_at_the_documented_path(self, overlay):
        node_pauses.pause(HOST, reason="training", units=["vllm-a"],
                          span_nodes=[HOST, PEER], stopped=True,
                          directory=overlay)
        path = node_pauses.overlay_path(overlay)
        assert path == os.path.join(overlay, "node_pauses.json")
        assert os.path.exists(path)
        doc = _raw(overlay)
        assert list(doc) == ["pauses"]
        assert list(doc["pauses"]) == [HOST]
        entry = doc["pauses"][HOST]
        assert sorted(entry) == sorted(node_pauses.ENTRY_KEYS)
        assert entry["reason"] == "training"
        assert entry["units"] == ["vllm-a"]
        assert entry["span_nodes"] == [HOST, PEER]
        assert entry["stopped"] is True
        assert entry["since"]  # ISO, stamped by the module

    def test_defaults_for_an_entry_written_with_nothing(self, overlay):
        entry = node_pauses.pause(HOST, directory=overlay)
        assert entry["reason"] == ""
        assert entry["units"] == []
        assert entry["span_nodes"] == []
        assert entry["stopped"] is False
        assert entry["until"] is None
        assert entry["since"]

    def test_non_string_list_items_are_dropped_with_a_warning(self, overlay,
                                                              warned):
        entry = node_pauses.pause(HOST, units=["vllm-a", 7, "", None],
                                  span_nodes=[HOST, {"node": PEER}],
                                  directory=overlay)
        assert entry["units"] == ["vllm-a"]
        assert entry["span_nodes"] == [HOST]
        assert any("units" in m for m in warned)
        assert any("span_nodes" in m for m in warned)

    def test_unknown_extra_keys_survive_a_round_trip(self, overlay):
        # A newer writer's field must not be silently eaten by an older reader.
        entry = node_pauses.pause(HOST, directory=overlay)
        entry["operator"] = "operator-1"
        node_pauses.save({"pauses": {HOST: entry}}, overlay)
        assert node_pauses.paused(HOST, overlay)["operator"] == "operator-1"

    def test_save_sorts_and_is_byte_stable(self, overlay):
        node_pauses.pause(PEER, directory=overlay)
        node_pauses.pause(HOST, directory=overlay)
        first = open(node_pauses.overlay_path(overlay)).read()
        node_pauses.save(node_pauses.load(overlay), overlay)
        assert open(node_pauses.overlay_path(overlay)).read() == first


# ── until / duration grammar ───────────────────────────────────────────────

class TestUntilGrammar:

    def test_no_until_is_open_ended(self, overlay):
        assert node_pauses.pause(HOST, directory=overlay)["until"] is None

    @pytest.mark.parametrize("text,seconds", [
        ("45", 45), ("45s", 45), ("90m", 5400), ("2h", 7200),
        ("1d", 86400), ("1w", 604800), ("1d3h", 97200), ("2h 30m", 9000),
    ])
    def test_duration_forms(self, text, seconds):
        delta = node_pauses.parse_duration(text)
        assert delta is not None and delta.total_seconds() == seconds

    @pytest.mark.parametrize("text", ["", "  ", "h", "2x", "two hours", None,
                                      "1.5.2h", object()])
    def test_non_durations_parse_to_none(self, text):
        assert node_pauses.parse_duration(text) is None

    def test_until_duration_is_relative_to_now(self, overlay):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        entry = node_pauses.pause(HOST, until="2h", directory=overlay, now=now)
        assert entry["until"] == "2026-11-21T14:00:00+00:00"
        assert datetime.fromisoformat(entry["until"]) == now + timedelta(hours=2)

    def test_until_iso_is_stored_canonical(self, overlay):
        entry = node_pauses.pause(
            HOST, until="2026-11-22T00:00:00Z", directory=overlay)
        assert entry["until"] == "2026-11-22T00:00:00+00:00"

    def test_until_timedelta_and_number(self, overlay):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        assert node_pauses.pause(HOST, until=timedelta(minutes=30),
                                 directory=overlay,
                                 now=now)["until"] == "2026-11-21T12:30:00+00:00"
        assert node_pauses.pause(PEER, until=120, directory=overlay,
                                 now=now)["until"] == "2026-11-21T12:02:00+00:00"

    def test_unparseable_until_becomes_open_ended_never_expired(self, overlay,
                                                                 warned):
        entry = node_pauses.pause(HOST, until="sometime next week",
                                  directory=overlay)
        assert entry["until"] is None
        assert node_pauses.pending_resume(directory=overlay) == []
        assert any("open-ended" in m for m in warned)

    def test_a_corrupt_until_on_disk_is_open_ended_not_auto_resume(self,
                                                                   overlay,
                                                                   warned):
        node_pauses.pause(HOST, until="2h", directory=overlay)
        doc = _raw(overlay)
        doc["pauses"][HOST]["until"] = "not-a-timestamp"
        node_pauses.save(doc, overlay)
        assert node_pauses.paused(HOST, overlay) is not None
        assert node_pauses.pending_resume(directory=overlay) == []
        assert node_pauses.expiry_pass(directory=overlay) == []
        assert any("open-ended" in m for m in warned)

    def test_repause_keeps_the_original_since(self, overlay):
        from datetime import datetime, timezone
        first = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
        node_pauses.pause(HOST, reason="training", directory=overlay, now=first)
        again = node_pauses.pause(
            HOST, reason="still training", until="1h", directory=overlay,
            now=first.replace(hour=20))
        assert again["since"] == "2026-11-20T08:00:00+00:00"
        assert again["reason"] == "still training"


# ── Durability: the flaw this module exists to remove ─────────────────────

class TestDurabilityAcrossProcesses:

    def test_every_api_read_goes_to_the_disk(self, overlay):
        node_pauses.pause(HOST, reason="training", directory=overlay)
        # Rewrite behind the module's back: a cached read would disagree.
        doc = _raw(overlay)
        doc["pauses"][HOST]["reason"] = "changed on disk"
        node_pauses.save(doc, overlay)
        assert node_pauses.paused(HOST, overlay)["reason"] == "changed on disk"

    def test_a_pause_written_by_one_process_is_seen_by_a_fresh_interpreter(
            self, overlay):
        """The daemon-restart / CLI-writes-daemon-reads case, for real."""
        node_pauses.pause(HOST, reason="sphoin training", units=["vllm-a"],
                          span_nodes=[HOST], stopped=True, directory=overlay)
        code = (
            "import json, sys\n"
            "from hscc_daemon import node_pauses\n"
            "print(json.dumps([node_pauses.paused(sys.argv[1], "
            "directory=sys.argv[2]), sorted(node_pauses.list_pauses("
            "directory=sys.argv[2]))]))\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code, HOST, overlay], env=env,
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        entry, hosts = json.loads(proc.stdout.strip().splitlines()[-1])
        assert hosts == [HOST]
        assert entry["reason"] == "sphoin training"
        assert entry["units"] == ["vllm-a"]

    def test_reload_does_not_reset_a_pause(self, overlay):
        import importlib
        node_pauses.pause(HOST, reason="training", directory=overlay)
        fresh = importlib.reload(node_pauses)
        try:
            assert fresh.paused(HOST, overlay) is not None
            assert fresh.paused(HOST, overlay)["reason"] == "training"
        finally:
            importlib.reload(node_pauses)


# ── Atomic writing ─────────────────────────────────────────────────────────

class TestAtomicSave:

    def test_only_the_overlay_is_left_behind(self, overlay):
        node_pauses.pause(HOST, directory=overlay)
        node_pauses.pause(PEER, directory=overlay)
        node_pauses.clear(HOST, directory=overlay)
        assert os.listdir(overlay) == ["node_pauses.json"]

    def test_a_failed_replace_leaves_no_tmp_and_keeps_the_old_file(self,
                                                                   overlay,
                                                                   monkeypatch):
        node_pauses.pause(HOST, reason="training", directory=overlay)
        before = open(node_pauses.overlay_path(overlay)).read()
        monkeypatch.setattr(node_pauses.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            node_pauses.pause(PEER, directory=overlay)
        assert os.listdir(overlay) == ["node_pauses.json"]  # no stray .tmp
        assert open(node_pauses.overlay_path(overlay)).read() == before

    def test_write_failure_propagates_instead_of_losing_a_pause(self, overlay,
                                                                monkeypatch):
        # A silently-swallowed write is a node that walks back into service.
        real = node_pauses.os.replace

        def flaky(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(node_pauses.os, "replace", flaky)
        with pytest.raises(OSError):
            node_pauses.pause(HOST, directory=overlay)
        monkeypatch.setattr(node_pauses.os, "replace", real)
        assert node_pauses.paused(HOST, overlay) is None

    def test_save_creates_a_missing_directory(self, tmp_path):
        root = str(tmp_path / "deep" / "nested")
        node_pauses.pause(HOST, directory=root)
        assert os.path.exists(os.path.join(root, "node_pauses.json"))

    def test_the_pause_file_never_touchs_its_neighbours(self, overlay):
        # The feature promise: the overlay is an overlay. Nothing the cluster is
        # built from is edited, even in the same directory.
        neighbours = {
            "serving.json": '{"units": [{"id": "a", "nodes": ["%s"]}]}' % HOST,
            "applied-template.json": '{"template": "spark-4x"}',
            "autodown.json": '{"enabled": false}',
        }
        for name, body in neighbours.items():
            _plant(overlay, name, body)
        node_pauses.pause(HOST, reason="training", units=["a"],
                          span_nodes=[HOST, PEER], stopped=True,
                          directory=overlay)
        node_pauses.pause(PEER, until="1h", directory=overlay)
        node_pauses.expiry_pass(directory=overlay)
        node_pauses.clear(HOST, directory=overlay)
        for name, body in neighbours.items():
            with open(os.path.join(overlay, name)) as f:
                assert f.read() == body, name


# ── paused(): truth-accurate, expiry is explicit ───────────────────────────

class TestPausedIsTruthAccurate:

    def test_a_pause_past_its_until_is_still_a_pause(self, overlay):
        node_pauses.pause(HOST, until="1h", directory=overlay,
                          now=_hours_ago(3))
        assert node_pauses.paused(HOST, overlay) is not None
        assert node_pauses.covering(HOST, overlay) is not None

    def test_a_pause_with_no_until_is_still_a_pause_years_later(self, overlay):
        node_pauses.pause(HOST, directory=overlay, now=_hours_ago(24 * 400))
        assert node_pauses.paused(HOST, overlay) is not None
        assert node_pauses.is_expired(node_pauses.paused(HOST, overlay)) is False

    def test_blank_or_bad_host_never_matches(self, overlay):
        node_pauses.pause(HOST, directory=overlay)
        assert node_pauses.paused("", overlay) is None
        assert node_pauses.paused(None, overlay) is None
        assert node_pauses.paused("   ", overlay) is None
        assert node_pauses.covering("", overlay) is None

    def test_paused_does_not_follow_span_nodes(self, overlay):
        # Documented split: paused() is host-keyed; covering() is the span-aware
        # question. Pinning it stops a future refactor from quietly merging them.
        node_pauses.pause(HOST, span_nodes=[HOST, PEER], directory=overlay)
        assert node_pauses.paused(PEER, overlay) is None
        assert node_pauses.covering(PEER, overlay) is not None

    def test_clear_ends_the_pause(self, overlay):
        node_pauses.pause(HOST, reason="training", directory=overlay)
        removed = node_pauses.clear(HOST, directory=overlay)
        assert removed["reason"] == "training"
        assert node_pauses.paused(HOST, overlay) is None
        assert "192.0.2.10" not in _raw(overlay)["pauses"]

    def test_clear_of_an_unknown_host_is_none_and_harmless(self, overlay):
        node_pauses.pause(HOST, reason="training", directory=overlay)
        path = node_pauses.overlay_path(overlay)
        before = (os.stat(path).st_mtime_ns, open(path).read())
        assert node_pauses.clear(UNRELATED, directory=overlay) is None
        # A resume retry must not churn the overlay another node depends on.
        assert (os.stat(path).st_mtime_ns, open(path).read()) == before
        assert node_pauses.paused(HOST, directory=overlay) is not None

    def test_pause_of_a_blank_host_is_a_programming_error(self, overlay):
        with pytest.raises(ValueError):
            node_pauses.pause("  ", directory=overlay)
        with pytest.raises(ValueError):
            node_pauses.clear("", directory=overlay)


# ── Span awareness (what a skip seam asks) ─────────────────────────────────

class TestSpanAwareness:

    def test_covering_reports_the_keyed_host_and_every_span_member(self,
                                                                   overlay):
        node_pauses.pause(HOST, units=["tp-8"], span_nodes=[HOST, PEER, OTHER],
                          stopped=True, directory=overlay)
        for node in (HOST, PEER, OTHER):
            entry = node_pauses.covering(node, overlay)
            assert entry is not None and entry["units"] == ["tp-8"], node
        assert node_pauses.covering(UNRELATED, overlay) is None

    def test_covering_prefers_the_entry_keyed_on_the_host(self, overlay):
        node_pauses.pause(HOST, reason="keyed", directory=overlay)
        node_pauses.pause(PEER, reason="spans-over",
                          span_nodes=[PEER, HOST], directory=overlay)
        assert node_pauses.covering(HOST, overlay)["reason"] == "keyed"

    def test_paused_nodes_is_keys_union_spans(self, overlay):
        node_pauses.pause(HOST, span_nodes=[HOST, PEER], directory=overlay)
        node_pauses.pause(OTHER, directory=overlay)
        assert node_pauses.paused_nodes(overlay) == {HOST, PEER, OTHER}
        assert UNRELATED not in node_pauses.paused_nodes(overlay)

    def test_a_cleared_pause_frees_the_whole_span(self, overlay):
        node_pauses.pause(HOST, span_nodes=[HOST, PEER], directory=overlay)
        node_pauses.clear(HOST, overlay)
        assert node_pauses.paused_nodes(overlay) == set()


# ── Expiry: pending_resume / expiry_pass ───────────────────────────────────

def _hours_ago(hours):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(hours=hours)


class TestExpiry:

    def test_pending_resume_lists_only_expired(self, overlay):
        node_pauses.pause(HOST, until="1h", directory=overlay,
                          now=_hours_ago(2))          # expired
        node_pauses.pause(PEER, until="24h", directory=overlay,
                          now=_hours_ago(1))          # still parked
        node_pauses.pause(OTHER, directory=overlay)   # open-ended
        found = node_pauses.pending_resume(directory=overlay)
        assert [host for host, _entry in found] == [HOST]

    def test_pending_resume_is_sorted_by_deadline_then_host(self, overlay):
        node_pauses.pause(HOST, until="1h", directory=overlay,
                          now=_hours_ago(2))            # due 1h ago
        node_pauses.pause(PEER, until="1h", directory=overlay,
                          now=_hours_ago(5))            # due 4h ago
        node_pauses.pause(OTHER, until="1h", directory=overlay,
                          now=_hours_ago(3))            # due 2h ago
        assert [h for h, _e in node_pauses.pending_resume(
            directory=overlay)] == [PEER, OTHER, HOST]

    def test_pending_resume_reports_without_changing_anything(self, overlay):
        node_pauses.pause(HOST, until="1h", directory=overlay,
                          now=_hours_ago(2))
        before = open(node_pauses.overlay_path(overlay)).read()
        assert len(node_pauses.pending_resume(directory=overlay)) == 1
        assert open(node_pauses.overlay_path(overlay)).read() == before
        assert node_pauses.paused(HOST, overlay) is not None

    def test_boundary_until_exactly_now_is_pending(self, overlay):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        node_pauses.pause(HOST, until=now, directory=overlay, now=now)
        assert [h for h, _e in node_pauses.pending_resume(now, overlay)] == [HOST]
        assert [h for h, _e in node_pauses.pending_resume(
            "2026-11-21T11:59:59+00:00", overlay)] == []

    def test_expiry_pass_drops_expired_and_keeps_the_rest(self, overlay):
        node_pauses.pause(HOST, until="1h", directory=overlay,
                          now=_hours_ago(2))
        node_pauses.pause(PEER, until="24h", directory=overlay,
                          now=_hours_ago(1))
        node_pauses.pause(OTHER, directory=overlay)
        passed = node_pauses.expiry_pass(directory=overlay)
        assert [h for h, _e in passed] == [HOST]
        assert passed[0][1]["reason"] == ""
        assert node_pauses.paused(HOST, overlay) is None
        assert node_pauses.paused(PEER, overlay) is not None
        assert node_pauses.paused(OTHER, overlay) is not None

    def test_expiry_pass_saves_only_when_something_expired(self, overlay):
        node_pauses.pause(PEER, until="24h", directory=overlay,
                          now=_hours_ago(1))
        path = node_pauses.overlay_path(overlay)
        before = (os.stat(path).st_mtime_ns, open(path).read())
        assert node_pauses.expiry_pass(directory=overlay) == []
        after = os.stat(path)
        assert (after.st_mtime_ns, open(path).read()) == before

    def test_expiry_pass_on_an_empty_overlay_is_quiet(self, overlay):
        assert node_pauses.expiry_pass(directory=overlay) == []
        assert os.listdir(overlay) == []

    def test_expiry_pass_accepts_an_explicit_now(self, overlay):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        node_pauses.pause(HOST, until="1h", directory=overlay, now=now)
        assert node_pauses.expiry_pass("2026-11-21T12:30:00+00:00",
                                       overlay) == []
        passed = node_pauses.expiry_pass("2026-11-21T13:30:00+00:00", overlay)
        assert [h for h, _e in passed] == [HOST]

    def test_a_tick_that_resumes_then_clears_leaves_an_empty_overlay(self,
                                                                     overlay):
        # The daemon's own order of operations, end to end on disk.
        node_pauses.pause(HOST, reason="training", units=["vllm-a"],
                          span_nodes=[HOST], stopped=True, until="1h",
                          directory=overlay, now=_hours_ago(2))
        pending = node_pauses.pending_resume(directory=overlay)
        assert [h for h, _e in pending] == [HOST]
        assert node_pauses.paused(HOST, overlay) is not None  # until resumed
        assert node_pauses.expiry_pass(directory=overlay)
        assert node_pauses.paused_nodes(overlay) == set()
        assert _raw(overlay) == {"pauses": {}}


# ── Concurrency-shaped reads and the root seam ─────────────────────────────

class TestReadModifyWriteAndRoot:

    def test_a_pause_added_elsewhere_is_not_clobbered(self, overlay):
        node_pauses.pause(HOST, reason="training", directory=overlay)
        # Another process adds a host through the file, then we pause ours.
        doc = _raw(overlay)
        doc["pauses"][PEER] = {"reason": "written elsewhere", "since": None,
                               "until": None, "units": [], "span_nodes": [],
                               "stopped": True}
        node_pauses.save(doc, overlay)
        node_pauses.pause(OTHER, reason="mine", directory=overlay)
        assert sorted(node_pauses.list_pauses(overlay)) == [HOST, PEER, OTHER]

    def test_overlay_path_defaults_to_hscct_dir_and_ignores_the_environment(
            self, overlay, monkeypatch):
        # cron_watchers' lesson: an env-var root is per-process, so it is not a
        # root another process can have written. No env knob exists here.
        monkeypatch.setattr(node_pauses, "HSCC_DIR", overlay)
        assert node_pauses.overlay_path() == os.path.join(
            overlay, "node_pauses.json")
        monkeypatch.setenv("HSCC_DIR", "/nonexistent/root")
        monkeypatch.setenv("HSCC_NODE_PAUSES", "/nonexistent/file.json")
        assert node_pauses.overlay_path() == os.path.join(
            overlay, "node_pauses.json")
        assert node_pauses.overlay_path(overlay) == os.path.join(
            overlay, "node_pauses.json")

    def test_the_seam_expands_a_tilde_and_never_writes_outside_it(self,
                                                                  overlay,
                                                                  monkeypatch):
        home = os.path.expanduser("~")
        monkeypatch.setattr(os.path, "expanduser",
                            lambda p: p.replace("~", home) if p.startswith("~")
                            else os.path.join(home, p.lstrip("/")))
        # directory= is the seam; a relative root resolves under HOME, not the
        # overlay's own default root.
        assert node_pauses.overlay_path("relative-root").startswith(home)


# ── The WARN actually gets out (fail-loud, not fail-silent) ───────────────

class TestWarningIsObservable:

    def test_warn_reaches_stderr_when_the_daemon_logger_is_unavailable(
            self, tmp_path, monkeypatch, capsys):
        # A corrupt overlay with no daemon log present must still be LOUD.
        monkeypatch.setitem(sys.modules, "hscc_daemon.daemon_ops", None)
        node_pauses._warn("overlay unreadable")
        err = capsys.readouterr().err
        assert "WARN" in err and "overlay unreadable" in err

    def test_warn_never_raises_even_when_nothing_works(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hscc_daemon.daemon_ops", None)

        def explode(*a, **k):
            raise RuntimeError("no stderr either")

        monkeypatch.setattr(sys, "stderr", explode)
        node_pauses._warn("still just a warning")  # must not raise
