"""F13 guard: reap_orphans must not act on a serving.json it cannot trust.

Audit docs/audits/2026-11-23-agent-surface.md §4 + §9 F13 — "reap_orphans decides
'orphan' from serving.json, which has >=3 writers; a stale serving.json makes a
live workload reapable". Before this card the reaper read the file through the
tolerant reader, which answers ``[]`` for absent AND corrupt AND unit-less, so a
host with no serving.json marked every sparkrun container on every worker as an
orphan and ``confirm=true`` stopped them all.

Hermetic throughout: TEST-NET-1 fakes (192.0.2.0/24) from conftest's
_pin_topology, serving.json planted as a real fake file under tmp_path, and
`sparkrun stop` intercepted at the clusterlib seam — no fleet, no ssh, no
subprocess (the suite's sparkrun fence reports anything that tried).
"""
import json
import os

import heal


class _StopRecorder:
    """A `sparkrun stop` recorder: `.calls` holds the argv that reached execution."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}


def _workers():
    return sorted(heal.cl.NODES)


# ── the file cannot be evaluated: refuse, and stop nothing ─────────────────

def test_absent_serving_json_refuses_instead_of_reaping_the_fleet(monkeypatch, tmp_path):
    """THE F13 case. No serving.json at all: every container reads as an orphan.

    Pre-guard this stopped all three workers; it is the one tool in the agent
    surface where a stale local copy deletes work, and it needed no operator
    mistake — just a host where the file had never been written.
    """
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(tmp_path / "absent.json"))
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": n, "recipe": f"sparkrun-{n[-2:]}-vllm"} for n in _workers()
    ])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["executed"] is False
    assert out["refused"] is True
    assert out["guard"] == heal.SERVING_NOT_EVALUABLE
    assert out["guard_code"] == "file_absent"
    assert run.calls == [], "a refusal must not reach `sparkrun stop`"
    assert out["reaped"] == []
    assert "nothing was stopped" in out["reason"]
    # the operator still sees what it WOULD have killed, so the refusal is checkable
    assert sorted(n for n, _ in out["would_have_reaped"]) == _workers()


def test_unparseable_serving_json_refuses(monkeypatch, fake_serving):
    fake_serving(raw='{"units": [{"name": "a", ')   # truncated mid-document
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["refused"] is True and out["guard_code"] == "file_unparseable"
    assert run.calls == []


def test_unit_less_serving_json_refuses(monkeypatch, fake_serving):
    """`{"units": []}` parses cleanly and is still not evidence to stop on: 'the
    fleet serves nothing' and 'the file is behind' are the same answer here."""
    fake_serving([])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["refused"] is True and out["guard_code"] == "no_units"
    assert run.calls == []


def test_unreadable_serving_json_refuses(monkeypatch, tmp_path):
    """A path that exists but cannot be opened (here: a directory) is NOT 'no
    units' — the refusal names the difference so the operator fixes the right
    thing."""
    blocked = tmp_path / "serving.json"
    blocked.mkdir()
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(blocked))
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["refused"] is True and out["guard_code"] == "file_unreadable"
    assert run.calls == []


# ── the file evaluates to the wrong SHAPE: refuse ─────────────────────────

def test_orphans_on_every_worker_refuses(monkeypatch, fake_serving):
    """A file that parses beautifully but marks the WHOLE fleet is the same
    hazard as a missing one (regenerated from another topology, or written before
    the fleet grew). A leftover is local; a wrong file is global."""
    fake_serving([{"name": "ghost", "nodes": ["192.0.2.99"]}])  # not in this fleet
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": n, "recipe": f"sparkrun-{n[-2:]}-vllm"} for n in _workers()
    ])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["refused"] is True
    assert out["guard"] == heal.SERVING_STALE and out["guard_code"] == "whole_fleet"
    assert sorted(out["orphan_nodes"]) == _workers()
    assert out["worker_nodes"] == _workers()
    assert run.calls == []


def test_local_orphan_still_reaps(monkeypatch, fake_serving):
    """CONTROL half: the guard must not disable the tool. One leftover on one
    node of a three-worker fleet — the case the tool exists for — still reaps."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.11", "recipe": "sparkrun-11-vllm"},
        {"node": "192.0.2.13", "recipe": "sparkrun-13-stale"},
    ])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert out["executed"] is True and out["ok"] is True
    assert out["guard"] == heal.SERVING_TRUSTWORTHY
    assert out["reaped"] == [{"node": "192.0.2.13", "recipe": "sparkrun-13-stale"}]
    assert len(run.calls) == 1
    assert run.calls[0][1] == "stop" and run.calls[0][2] == "sparkrun-13-stale"


def test_no_containers_is_not_refused(monkeypatch, tmp_path):
    """Nothing running means nothing to protect: an unreadable file must not turn
    a no-op into a refusal, or the guard cries wolf on every idle sweep."""
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(tmp_path / "absent.json"))
    monkeypatch.setattr(heal, "_running_containers", lambda: [])

    out = heal.reap_orphans({})

    assert out.get("refused") is None and out["preview"] is True


# ── the confirmation text now says what the stop is based on ───────────────

def test_preview_quotes_the_serving_file_behind_the_stop(monkeypatch, fake_serving):
    path = fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-stale"}])

    out = heal.reap_orphans({})

    would = out["would_do"]
    assert out["preview"] is True
    assert path in would
    assert "definition of orphan" in would and "guard: trustworthy" in would
    assert "live worker(s)" in would


# ── force overrides the verdict, nothing else ──────────────────────────────

def test_force_overrides_guard_but_never_head(monkeypatch, tmp_path):
    """force=true means 'I checked by hand'. It lifts the guard's two refusals —
    and cannot lift the head exemption, which is not the guard's to give."""
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(tmp_path / "absent.json"))
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": heal.cl.HEAD, "recipe": "orch-27b"},
        {"node": "192.0.2.13", "recipe": "sparkrun-13-stale"},
    ])
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True, "force": True})

    assert out["executed"] is True
    assert out["guard_overridden"] is True
    assert [c[2] for c in run.calls] == ["sparkrun-13-stale"], \
        "force must not put the head/orchestrator container in the stop list"
    assert {"node": heal.cl.HEAD, "recipe": "orch-27b"} not in out["reaped"]


def test_preview_says_loudly_when_the_guard_is_beaten(monkeypatch, tmp_path):
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(tmp_path / "absent.json"))
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-stale"}])

    out = heal.reap_orphans({"force": True})

    assert out["preview"] is True
    assert "GUARD OVERRIDDEN" in out["would_do"]
    assert "not_evaluable" in out["would_do"]


# ── the action-time re-read: the >=3-writer race, at the moment of harm ────

def _scripted_snapshots(*snaps):
    """A serving_snapshot() that answers from a script and then repeats the last."""
    state = {"i": 0}

    def _snap():
        s = snaps[min(state["i"], len(snaps) - 1)]
        state["i"] += 1
        return s
    return _snap


def _ok(units, age_s=5.0):
    return {"path": "/fake/serving.json", "state": heal.cl.SERVING_OK,
            "units": list(units), "age_s": age_s}


def test_node_claimed_while_deciding_is_not_stopped(monkeypatch, fake_serving):
    """A writer that claims the node between plan and stop is exactly the F13
    race (serving.json has >=3 writers and none lock against this reaper). The
    stop for that node is dropped and the call reports ok=False, not a clean reap."""
    fake_serving([{"name": "ghost", "nodes": ["192.0.2.99"]}])  # plan: .13 is orphan
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    monkeypatch.setattr(heal.cl, "serving_snapshot", _scripted_snapshots(
        _ok([{"name": "ghost", "nodes": ["192.0.2.99"]}]),
        _ok([{"name": "worker-c", "nodes": ["192.0.2.13"]}], age_s=0.1),
    ))
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert run.calls == [], "a node the file now claims must not be stopped"
    assert out["reaped"] == []
    assert out["ok"] is False
    assert len(out["skipped"]) == 1 and out["skipped"][0]["node"] == "192.0.2.13"
    assert "now names" in out["skipped"][0]["reason"]


def test_action_time_claim_survives_force(monkeypatch, fake_serving):
    """force=true buys a verdict, not a blank cheque: the re-read at the moment of
    the stop cannot be overridden, because by then it is positive evidence."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    monkeypatch.setattr(heal.cl, "serving_snapshot", _scripted_snapshots(
        _ok([{"name": "worker-a", "nodes": ["192.0.2.11"]}]),
        _ok([{"name": "worker-a", "nodes": ["192.0.2.11"]},
             {"name": "worker-c", "nodes": ["192.0.2.13"]}]),
    ))
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True, "force": True})

    assert run.calls == []
    assert len(out["skipped"]) == 1 and out["ok"] is False


def test_unreadable_re_read_does_not_abort_on_its_own(monkeypatch, fake_serving):
    """One-directional: an unreadable re-read is the state the plan-time verdict
    already judged (possibly under force), so it is not evidence of a claim."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}])
    monkeypatch.setattr(heal.cl, "serving_snapshot", _scripted_snapshots(
        _ok([{"name": "worker-a", "nodes": ["192.0.2.11"]}]),
        {"path": "/fake/serving.json", "state": heal.cl.SERVING_ABSENT,
         "units": [], "age_s": None},
    ))
    run = _StopRecorder()
    monkeypatch.setattr(heal.cl, "run_cmd", run)

    out = heal.reap_orphans({"confirm": True})

    assert len(run.calls) == 1 and out["reaped"] == [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-vllm"}]
    assert "skipped" not in out


# ── the guard observes serving.json; it does not own it ────────────────────

def test_guard_never_writes_serving_json(monkeypatch, fake_serving, tmp_path):
    """Card constraint: keep it a guard, do NOT rewrite serving.json ownership.
    Every branch — refusal, preview, override, reap — leaves the file byte-exact
    and adds no sibling file where a writer's backup would land."""
    path = fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    before = open(path, "rb").read()
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "sparkrun-13-stale"}])
    monkeypatch.setattr(heal.cl, "run_cmd", _StopRecorder())

    heal.reap_orphans({})                                  # preview
    heal.reap_orphans({"confirm": True})                   # trustworthy reap
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(tmp_path / "gone.json"))
    heal.reap_orphans({"confirm": True})                   # refusal (absent)
    heal.reap_orphans({"confirm": True, "force": True})    # override + reap

    assert open(path, "rb").read() == before
    assert sorted(os.listdir(tmp_path)) == ["serving.json"]


# ── the clusterlib seam's own contract ────────────────────────────────────

def test_snapshot_reports_state_and_age(monkeypatch, fake_serving):
    path = fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]},
                         {"name": "worker-b", "nodes": ["192.0.2.12"]},
                         "not-a-mapping"])   # a malformed unit must not raise
    snap = heal.cl.serving_snapshot()
    assert snap["state"] == heal.cl.SERVING_OK
    assert snap["path"] == path
    assert snap["age_s"] is not None and 0.0 <= snap["age_s"] < 60.0
    assert heal._serving_nodes(snap) == {"192.0.2.11", "192.0.2.12"}


def test_snapshot_states_are_exhaustive_and_never_raise(monkeypatch, tmp_path):
    """Every way serving.json can be wrong gets its own state, and none of them
    raise — the reaper must be able to ask the question without a try/except."""
    p = tmp_path / "s.json"
    p.write_text("not json {{{")
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(p))
    assert heal.cl.serving_snapshot()["state"] == heal.cl.SERVING_UNPARSEABLE

    p.write_text("[1, 2, 3]")           # valid JSON, wrong top-level shape
    assert heal.cl.serving_snapshot()["state"] == heal.cl.SERVING_UNPARSEABLE

    p.write_text('{"units": "orch"}')   # wrong shape for units, still a readable file
    snap = heal.cl.serving_snapshot()
    assert snap["state"] == heal.cl.SERVING_OK and snap["units"] == []

    p.write_text('{"units": [{"name": "a", "nodes": ["192.0.2.11"]}]}')
    assert heal.cl.serving_snapshot()["units"][0]["name"] == "a"
    # the path argument wins over the module constant
    assert heal.cl.serving_snapshot(str(tmp_path / "nope.json"))["state"] == heal.cl.SERVING_ABSENT


def test_tolerant_reader_is_unchanged_for_read_only_tools(monkeypatch, tmp_path):
    """The tolerant [] answer stays: cluster_status/pick_node/provision_model read
    serving.json for identity and must keep answering instead of raising. Only the
    reaper was moved to the state-aware read."""
    assert heal.cl.read_serving_units() == []
    p = tmp_path / "serving.json"
    p.write_text('{"units": [{"name": "a", "nodes": ["192.0.2.11"]}]}')
    monkeypatch.setattr(heal.cl, "SERVING_JSON", str(p))
    assert heal.cl.read_serving_units()[0]["name"] == "a"
