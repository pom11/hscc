"""hscc kanban pause/resume/paused/enforce — fake hermes CLI, tmp state + config."""

import json

import pytest

from hscc_daemon import kanban_pause as kp


class FakeHermes:
    """In-memory boards driven through the same argv the module sends."""

    def __init__(self, boards):
        self.boards = {b: {c: dict(s) for c, s in cards.items()} for b, cards in boards.items()}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        board, verb, rest = args[1], args[2], args[3:]
        cards = self.boards[board]

        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        if verb == "list":
            r.stdout = json.dumps([{"id": i, "status": c["status"]} for i, c in cards.items()])
        elif verb == "reclaim":
            assert cards[rest[0]]["status"] == "running"
            cards[rest[0]]["status"] = "ready"
        elif verb == "block":
            assert rest[0] == "--kind"              # --kind must precede the id (argparse)
            cid = rest[2]
            if cards[cid]["status"] in ("todo", "triage"):
                r.returncode = 1
            else:
                cards[cid]["status"] = "blocked"
        elif verb == "unblock":
            cid = rest[0]
            if cards[cid]["status"] != "blocked":
                r.returncode = 1
            else:
                cards[cid]["status"] = "todo" if cards[cid].get("parents_open") else "ready"
        return r


@pytest.fixture(autouse=True)
def _no_real_inject(monkeypatch):
    """Never type into a real tmux pane from a test."""
    monkeypatch.setattr(kp, "INJECT_BIN", "/nonexistent/hermes-inject")


@pytest.fixture(autouse=True)
def _no_real_cron(monkeypatch):
    """Never touch a real profile's cron jobs from a test."""
    class R:
        returncode, stdout, stderr = 0, "No scheduled jobs.\n", ""
    monkeypatch.setattr(kp, "_run_cron", lambda profile, args: R())


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: x\nkanban:\n  max_in_progress: 10\n  auto_decompose: true\n"
                   "  auto_decompose_per_tick: 3\nother:\n  auto_decompose: true\n")
    return {"state_path": str(tmp_path / "paused.json"), "config_path": str(cfg), "cfg": cfg}


def _boards():
    return FakeHermes({
        "low": {"r1": {"status": "running"}, "q1": {"status": "ready"},
                "t1": {"status": "todo"}, "b1": {"status": "blocked"},
                "x1": {"status": "triage"}, "d1": {"status": "done"}},
        "high": {"r9": {"status": "running"}}})


def test_pause_holds_running_and_ready_only(env):
    h = _boards()
    rep = kp.pause(["low"], "priority", run=h, state_path=env["state_path"],
                   config_path=env["config_path"], now=100)
    assert sorted(rep["low"]["held"]) == ["q1", "r1"] and rep["low"]["triage"] == ["x1"]
    low = h.boards["low"]
    assert low["r1"]["status"] == low["q1"]["status"] == "blocked"
    assert low["t1"]["status"] == "todo" and low["b1"]["status"] == "blocked"
    assert h.boards["high"]["r9"]["status"] == "running"       # other boards untouched
    reclaim_at = next(i for i, c in enumerate(h.calls) if c[2] == "reclaim")
    assert h.calls[reclaim_at + 1][2] == "block"                # reclaim then block, same card
    st = kp.load_state(env["state_path"])
    assert {c["id"]: c["was"] for c in st["low"]["cards"]} == {"r1": "running", "q1": "ready"}
    assert st["_auto_decompose_was"] is True
    text = env["cfg"].read_text()
    assert "  auto_decompose: false\n" in text and "other:\n  auto_decompose: true" in text


def test_resume_releases_exactly_the_paused_cards_and_restores_decompose(env):
    h = _boards()
    kp.pause(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"])
    h.boards["low"]["q1"]["parents_open"] = True
    rep = kp.resume(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"])
    assert sorted(rep["low"]["released"]) == ["q1", "r1"]
    assert h.boards["low"]["r1"]["status"] == "ready" and h.boards["low"]["q1"]["status"] == "todo"
    assert h.boards["low"]["b1"]["status"] == "blocked"         # pre-pause block stays
    assert kp.load_state(env["state_path"]) == {}
    assert "  auto_decompose: true\n" in env["cfg"].read_text()


def test_decompose_stays_off_until_the_last_board_resumes(env):
    h = FakeHermes({"a": {"x": {"status": "ready"}}, "b": {"y": {"status": "ready"}}})
    kw = dict(run=h, state_path=env["state_path"], config_path=env["config_path"])
    kp.pause(["a"], **kw)
    kp.pause(["b"], **kw)
    kp.resume(["a"], **kw)
    assert "  auto_decompose: false\n" in env["cfg"].read_text()
    kp.resume(["b"], **kw)
    assert "  auto_decompose: true\n" in env["cfg"].read_text()


def test_decompose_left_off_if_it_was_off(env):
    env["cfg"].write_text("kanban:\n  auto_decompose: false\n")
    h = FakeHermes({"a": {"x": {"status": "ready"}}})
    kw = dict(run=h, state_path=env["state_path"], config_path=env["config_path"])
    kp.pause(["a"], **kw)
    kp.resume(["a"], **kw)
    assert env["cfg"].read_text() == "kanban:\n  auto_decompose: false\n"


def test_enforce_holds_new_cards_on_paused_boards_only(env):
    h = _boards()
    kw = dict(run=h, state_path=env["state_path"])
    assert kp.enforce(**kw) == {}                                  # nothing paused: no-op
    kp.pause(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"])
    h.boards["low"]["new"] = {"status": "ready"}
    h.boards["high"]["new2"] = {"status": "ready"}
    assert kp.enforce(**kw) == {"low": ["new"]}
    assert h.boards["low"]["new"]["status"] == "blocked"
    assert h.boards["high"]["new2"]["status"] == "ready"
    assert "new" in [c["id"] for c in kp.load_state(env["state_path"])["low"]["cards"]]
    assert kp.enforce(**kw) == {}                                  # idempotent


def test_pause_twice_does_not_double_record(env):
    h = _boards()
    kw = dict(run=h, state_path=env["state_path"], config_path=env["config_path"])
    kp.pause(["low"], **kw)
    kp.pause(["low"], **kw)
    ids = [c["id"] for c in kp.load_state(env["state_path"])["low"]["cards"]]
    assert sorted(ids) == ["q1", "r1"]


def test_resume_unknown_board(env):
    out = kp.resume(["nope"], run=_boards(), state_path=env["state_path"],
                    config_path=env["config_path"])
    assert out == {"nope": {"error": "not paused"}}


def test_cli_paused_json_and_usage(monkeypatch, tmp_path, capsys):
    from hscc_daemon import kanban_cli
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"low": {"paused_at": 1, "reason": "r", "cards": [{"id": "a", "was": "ready"}]},
                             "_auto_decompose_was": True}))
    monkeypatch.setattr(kp, "STATE_PATH", str(p))
    assert kanban_cli.cmd_kanban(["paused", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"low": {"paused_at": 1, "reason": "r", "cards": ["a"], "triage_left": []}}
    assert kanban_cli.cmd_kanban(["pause", "--json"]) != 0


def test_pause_interrupts_and_steers_the_orchestrator_resume_notifies(env):
    sent = []
    inj = lambda profile, msg, mode: sent.append((profile, mode, msg)) or {"ok": True, "error": None}
    h = _boards()
    kw = dict(run=h, state_path=env["state_path"], config_path=env["config_path"], inject=inj)
    rep = kp.pause(["low"], "ecofire first", **kw)
    assert rep["low"]["orchestrator"]["ok"]
    profile, mode, msg = sent[0]
    assert (profile, mode) == ("low-orch", "interrupt")
    assert "Cancel what you are doing" in msg and "ecofire first" in msg and "`low`" in msg
    kp.resume(["low"], **kw)
    profile, mode, msg = sent[1]
    assert (profile, mode) == ("low-orch", "auto") and "RESUMED" in msg and "2 card(s)" in msg


def test_missing_inject_tool_is_reported_not_fatal(env):
    h = _boards()
    rep = kp.pause(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"])
    assert rep["low"]["orchestrator"] == {"ok": False,
                                          "error": "/nonexistent/hermes-inject not found"}
    assert sorted(rep["low"]["held"]) == ["q1", "r1"]


def test_enforce_reholds_a_recorded_card_released_behind_the_pause(env):
    h = _boards()
    kp.pause(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"],
             inject=None, cron=None)
    h.boards["low"]["q1"]["status"] = "running"         # an in-flight orch turn unblocked it
    assert kp.enforce(run=h, state_path=env["state_path"]) == {"low": ["q1"]}
    assert h.boards["low"]["q1"]["status"] == "blocked"
    ids = [c["id"] for c in kp.load_state(env["state_path"])["low"]["cards"]]
    assert ids.count("q1") == 1


class FakeCron:
    def __init__(self, jobs):
        self.jobs = dict(jobs)          # id -> "active"/"paused"
        self.calls = []

    def __call__(self, profile, args):
        self.calls.append((profile, args))

        class R:
            returncode, stderr = 0, ""
            stdout = ""
        r = R()
        if args[0] == "list":
            r.stdout = "\n".join(f"  {j} [{st}]\n    Name: x" for j, st in self.jobs.items())
        elif args[0] in ("pause", "resume"):
            self.jobs[args[1]] = "paused" if args[0] == "pause" else "active"
        return r


def test_pause_parks_active_orchestrator_crons_and_resume_restores_them(env):
    h = _boards()
    c = FakeCron({"aaa111": "active", "bbb222": "paused"})
    kw = dict(run=h, state_path=env["state_path"], config_path=env["config_path"], inject=None, cron=c)
    rep = kp.pause(["low"], **kw)
    assert rep["low"]["crons_paused"] == ["aaa111"]
    assert c.jobs == {"aaa111": "paused", "bbb222": "paused"}
    assert all(p == "low-orch" for p, _ in c.calls)
    rep = kp.resume(["low"], **kw)
    assert rep["low"]["crons_resumed"] == ["aaa111"]
    assert c.jobs == {"aaa111": "active", "bbb222": "paused"}     # pre-paused job stays paused
