"""t_8ad06a53 — the pause must stop what it says it stopped (2.17.3 blocker).

The live failure (operator, one worker node — host redacted): ``hscc node
pause <host>`` was answered ``y``, RECORDED the pause and printed ``1 unit(s)
stopped`` while sparkrun answered ``No running workload matches intent <hash>``
and the container stayed up. Cause: ``sparkrun stop`` does not find a workload by
target+host — it recomputes the INTENT HASH from the recipe plus
``--port`` / ``--served-model-name`` / ``--tp`` (sparkrun's
``cli/_stop_logs.py`` → ``cli/_common.build_cluster_id_overrides`` →
``orchestration/job_metadata.generate_intent_id``) and stops the workload whose
intent matches. hscc built its stop from recipe/hosts/port only, so the intent
differed and nothing matched — and the same builder backs the watchdog's
auto-restart stop.

These tests reproduce the contract with a planted fake sparkrun that ONLY stops
when the stop argv carries the same intent flags as the run argv that launched
the workload — the exact bug, not a mock of it.

Hermetic by construction (``nolive-rule.txt``): every host is RFC 5737
documentation space (192.0.2.x), the ``sparkrun`` the code exec's is a planted
script under ``tmp_path`` pinned through ``HSCC_SPARKRUN_BIN`` (the conftest's
autouse fake is re-pointed, never the operator's CLI), and ``ssh``/``docker``
are PATH hijacks that only log. No test here reads the operator's serving.json
or reaches a real host.
"""

import ast
import io
import json
import os
import shlex
import shutil
import sys
from contextlib import redirect_stdout

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import lifecycle, node_pause_exec as npe
from hscc_daemon import node_pauses, serving, sparkrun_bin, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
ORCH = "192.0.2.11"
#: One single-node model — the plain pause target (the operator's shape).
W1 = "192.0.2.12"
#: The far half of the span unit.
W2 = "192.0.2.13"
#: Two single-node models on one machine — the partial-failure shape: both park
#: together, so eligibility allows the pause and one stop can still fail.
W3 = "192.0.2.14"

ORCH_RECIPE = "/srv/recipes/orchestrator.yaml"
WORKER_RECIPE = "/srv/recipes/worker.yaml"
POOL_RECIPE = "/srv/recipes/pool.yaml"

#: The unit the operator's pause failed on: single node, tp 1, and launched
#: with `--served-model-name <concrete> <alias>` — the flag the stop dropped.
UNIT_W1 = {"id": "w1", "role": "worker", "nodes": [W1], "port": 8100,
           "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1}
UNIT_W3A = {"id": "w3a", "role": "worker", "nodes": [W3], "port": 8300,
            "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1}
UNIT_W3B = {"id": "w3b", "role": "worker", "nodes": [W3], "port": 8310,
            "recipe": WORKER_RECIPE, "model": "meta-llm/Work-9B", "tp": 1}
UNIT_SPAN = {"id": "wspan", "role": "worker", "nodes": [W1, W2], "port": 8200,
             "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 2}
UNIT_ORCH = {"id": "orch", "role": "orchestrator", "nodes": [ORCH],
             "port": 8000, "recipe": ORCH_RECIPE, "model": "meta-llm/Orch-8B",
             "tp": 1}
UNIT_POOL = {"id": "pool", "role": "worker", "nodes": [W1], "port": 8100,
             "recipe": POOL_RECIPE, "model": "meta-llm/Pool-8B", "tp": 1}

#: The roster the argv pins read: solo worker, span worker and orchestrator, so
#: one `plan()` exercises all three shapes.
SERVING = {"port": 8000, "units": [UNIT_ORCH, UNIT_W1, UNIT_SPAN]}
#: The pause target's own roster — one unit on W1, so pausing W1 owns exactly
#: the workload under test instead of also enumerating a declared span unit and
#: tripping the multi-node eligibility rule for an unrelated reason.
W1_SERVING = {"port": 8000, "units": [UNIT_W1]}
W3_SERVING = {"port": 8000, "units": [UNIT_W3A, UNIT_W3B]}
#: The operator's real shape: a uniform-pool fleet, where every unit advertises
#: THREE names (`<concrete> worker-model orchestrator-model`) — so the pool needs
#: a second node for the alias to survive a pause of the first.
UNIT_POOL2 = {"id": "pool2", "role": "worker", "nodes": [W2], "port": 8100,
              "recipe": POOL_RECIPE, "model": "meta-llm/Pool-8B", "tp": 1}
POOL_SERVING = {"port": 8000, "uniform_pool": True,
                "units": [UNIT_POOL, UNIT_POOL2]}

#: The argv the buggy builder produced (recipe/hosts/port only). Kept here as
#: the negative control: every test that proves the fix also proves THIS form
#: is refused by the same fake, so a passing suite is not a vacuous one.
PREFIX_STOP = ["sparkrun", "stop", WORKER_RECIPE, "--hosts", W1, "--port",
               "8100"]


def _group(unit, pool=False):
    # The sweep's served name comes from the SAME builder the launch uses, so
    # the fixture cannot disagree with itself about the unit's intent.
    meta = {"recipe": unit["recipe"], "hosts": list(unit["nodes"]),
            "port": unit["port"], "model": unit["model"],
            "served_model_name": serving._served_model_name(
                dict(unit, uniform_pool=pool)),
            "tensor_parallel": unit.get("tp") or 1}
    return {f"cid_{unit['id']}": {
        "meta": meta,
        "containers": [{"host": h, "role": unit["role"], "status": "running",
                        "image": "img"} for h in unit["nodes"]]}}


def payload_for(*units, pool=False):
    groups = {}
    for u in units:
        groups.update(_group(u, pool=pool))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_hosts": [],
            "total_containers": len(units), "host_count": len(units)}


def reader(payload):
    return lambda: (payload, None)


# ── the fake sparkrun: intent-matching, like the real one ──────────────────
FAKE_SPARKRUN = '''#!/usr/bin/env python3
"""Fake sparkrun that honours the real stop contract (t_8ad06a53).

A workload is addressed by the INTENT sparkrun recomputes from the recipe plus
--port/--served-model-name/--tp; hosts and serve args are NOT part of it. So a
`stop` that dropped a flag the `run` carried matches NOTHING — which is exactly
what the operator saw, and what a plain argv-recording mock would have hidden.
"""
import json
import os
import shlex
import sys

CALLS = os.environ["FAKE_CALL_LOG"]
INTENTS = os.environ["FAKE_INTENTS"]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def intent(argv):
    return json.dumps({"target": argv[1] if len(argv) > 1 else None,
                       "port": flag(argv, "--port"),
                       "served_model_name": flag(argv, "--served-model-name"),
                       "tp": flag(argv, "--tp")}, sort_keys=True)


def live_intents():
    try:
        with open(INTENTS) as fh:
            return [line.rstrip("\\n") for line in fh if line.strip()]
    except OSError:
        return []


def main():
    argv = sys.argv[1:]
    with open(CALLS, "a") as fh:
        # shlex, not " ".join: a multi-word --served-model-name must survive the
        # log so the test can read the EXACT argv back (a split-on-space log
        # would hide a stop that passed the name as two tokens).
        fh.write("sparkrun " + shlex.join(argv) + "\\n")
    verb = argv[0] if argv else ""
    if "--all" in argv:
        print("fake sparkrun: refusing --all (hscc must never emit it here)",
              file=sys.stderr)
        return 2
    if verb == "run":
        with open(INTENTS, "a") as fh:
            fh.write(intent(argv) + "\\n")
        print("launched")
        return 0
    if verb == "stop":
        key = intent(argv)
        live = live_intents()
        if key not in live:
            print("No running workload matches intent " + key[:16],
                  file=sys.stderr)
            return 1
        live.remove(key)
        with open(INTENTS, "w") as fh:
            fh.writelines(line + "\\n" for line in live)
        print("STOPPED")
        return 0
    return 0


sys.exit(main())
'''


@pytest.fixture
def fake_sparkrun(tmp_path, monkeypatch):
    """The intent-matching CLI on ``HSCC_SPARKRUN_BIN`` + fake ssh/docker.

    The CLI is deliberately NOT on PATH: the shared sparkrun fence identifies
    the operator's live CLI with ``shutil.which``, and shadowing it with a fake
    would make the fence mis-classify the fake as live.
    """
    bin_dir = tmp_path / "planted-bin"
    bin_dir.mkdir()
    net_dir = tmp_path / "planted-netbin"
    net_dir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("")
    intents = tmp_path / "intents.json"
    intents.write_text("")

    cli = bin_dir / "sparkrun"
    cli.write_text("#!" + sys.executable + "\n" + FAKE_SPARKRUN)
    cli.chmod(0o755)
    assert os.path.realpath(shutil.which("sparkrun") or "") != \
        os.path.realpath(str(cli)), \
        "the planted CLI must stay off PATH — the fence identifies the live " \
        "CLI with shutil.which and would mis-classify the fake"
    for name in ("ssh", "docker"):
        script = net_dir / name
        script.write_text("#!/bin/sh\n"
                          "printf '%s ' \"%s\" >> \"$FAKE_CALL_LOG\"\n"
                          "printf '\\n' >> \"$FAKE_CALL_LOG\"\n"
                          "exit 1\n")   # forbidden transport: fail if used
        script.chmod(0o755)

    monkeypatch.setenv("PATH", str(net_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
    monkeypatch.setenv("FAKE_CALL_LOG", str(calls))
    monkeypatch.setenv("FAKE_INTENTS", str(intents))
    sparkrun_bin.reset_cache()

    class Fake:
        def __init__(self):
            self.calls = calls
            self.intents = intents

        def launched(self, argv):
            """Hand the fake the argv `hscc cluster up` would have launched —
            this is what puts a workload on the node, with its real intent."""
            res = util.run_cmd(list(argv))
            assert res["ok"], res
            return res

        def log(self):
            return self.calls.read_text().splitlines()

        def live(self):
            raw = self.intents.read_text().strip()
            return [json.loads(line) for line in raw.splitlines() if line]

        def stop_argv(self):
            # shlex, not split(): a quoted multi-word --served-model-name must
            # come back as ONE token, or a stop that split the name in two
            # would read as correct here.
            return [shlex.split(line[len("sparkrun "):])
                    for line in self.log() if line.startswith("sparkrun stop")]

    yield Fake()
    sparkrun_bin.reset_cache()


# ── the CLI harness (same shape test_node_cli uses, no run_cmd patch) ──────
def run(argv):
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            rc = unified._handle_node(list(argv))
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


@pytest.fixture
def tty(monkeypatch):
    def _attach(*answers):
        monkeypatch.setattr(unified, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO("".join(f"{a}\n" for a in answers)))
    return _attach


@pytest.fixture
def fleet(monkeypatch):
    """Config + status seams only — `util.run_cmd` stays REAL so the stop
    really execs the planted CLI. A patched run_cmd could not catch this bug."""
    monkeypatch.setattr(serving, "load_serving", lambda path=None: W1_SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        reader(payload_for(UNIT_W1)))


@pytest.fixture
def two_solo_units(monkeypatch):
    """One machine, two single-node models, only ONE of them actually running
    under a matching intent — the partial-failure shape."""
    monkeypatch.setattr(serving, "load_serving", lambda path=None: W3_SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        reader(payload_for(UNIT_W3A, UNIT_W3B)))


@pytest.fixture
def pool_fleet(monkeypatch):
    monkeypatch.setattr(serving, "load_serving", lambda path=None: POOL_SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        reader(payload_for(UNIT_POOL, UNIT_POOL2, pool=True)))


def launch_argv(unit, pool=False):
    """What `hscc cluster up` issues for a unit — the intent the stop has to
    match, built by the sanctioned builder with fleet_up_plan's stamping."""
    return serving._unit_run_cmd(dict(unit, uniform_pool=bool(pool)))


# ── the regression: pause stops the workload its unit declares ─────────────
class TestPauseStopsTheDeclaredIntent:
    def test_the_pause_stop_actually_stops_the_running_workload(
            self, fleet, tty, fake_sparkrun):
        """The bug, end to end: the workload is on the node under the intent the
        declared unit's launch produces, the operator answers y, and the fake
        sparkrun — which stops ONLY on an intent match — must stop it."""
        fake_sparkrun.launched(launch_argv(UNIT_W1))
        assert fake_sparkrun.live(), "the fixture workload is up"
        tty("y")
        rc, out = run(["pause", W1, "--reason", "disk swap"])
        assert rc == 0, out
        assert "1 unit(s) stopped" in out, out
        assert fake_sparkrun.stop_argv(), "no stop was ever exec'd"
        assert not fake_sparkrun.live(), \
            "the fake never matched the intent — nothing really came down"
        entry = node_pauses.paused(W1)
        assert entry is not None and entry["stopped"] is True
        assert not [l for l in fake_sparkrun.log()
                    if l.startswith(("ssh", "docker"))]

    def test_the_stop_argv_carries_every_intent_flag_the_run_carried(
            self, fake_sparkrun):
        """The one-seam guarantee, stated as data: for the SAME declared unit,
        every intent flag in the run argv appears in the stop argv with the same
        value (hosts, port, served-model-name, tp)."""
        unit = UNIT_SPAN
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for(UNIT_W1, UNIT_SPAN)))
        record = next(u for u in plan["units"] if u["unit_id"] == "wspan")
        run_flags = _flag_pairs(record["resume_cmd"])
        stop_flags = _flag_pairs(record["stop_cmd"])
        for flag in ("--hosts", "--port", "--served-model-name", "--tp"):
            assert flag in run_flags, record["resume_cmd"]
            assert stop_flags.get(flag) == run_flags[flag], (
                f"{flag} drifted between run and stop: "
                f"{record['stop_cmd']} vs {record['resume_cmd']}")

    def test_a_uniform_pool_unit_stops_with_the_same_three_names(
            self, pool_fleet, tty, fake_sparkrun):
        """The operator's shape: a uniform-pool unit advertises `<concrete>
        worker-model orchestrator-model` — THREE names, stamped by
        `fleet_up_plan`. A pause that derived the two-name form would miss the
        intent exactly like the old builder did."""
        unit = POOL_SERVING["units"][0]
        launch = launch_argv(unit, pool=True)
        assert _flag_pairs(launch)["--served-model-name"] == (
            "meta-llm/Pool-8B worker-model orchestrator-model")
        fake_sparkrun.launched(launch)
        tty("y")
        rc, out = run(["pause", W1])
        assert rc == 0, out
        assert not fake_sparkrun.live(), "the three-name intent never matched"

    def test_the_stop_and_the_resume_agree_for_every_declared_unit(self):
        """Drift pin: the pause's own resume is the sanctioned argv, so the
        stop's intent flags must equal ITS flags for every unit in the roster —
        the invariant that makes 'no second implementation' checkable."""
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for(UNIT_W1, UNIT_SPAN)))
        assert plan["units"], plan
        for record in plan["units"]:
            if not record["resume_cmd"]:
                continue
            stop = _flag_pairs(record["stop_cmd"])
            run_ = _flag_pairs(record["resume_cmd"])
            for flag in ("--hosts", "--port", "--served-model-name", "--tp"):
                assert stop.get(flag) == run_.get(flag), record

    def test_the_stop_carries_no_serve_args_the_run_holds(self, fake_sparkrun):
        """`-o` overrides ride the RUN argv (sparkrun does not hash serve args
        into the intent) and must NOT ride the stop: `sparkrun stop` has no
        -o/--option at all, so smuggling one in turns a working stop into a
        hard `No such option` failure."""
        unit = dict(UNIT_W1, max_num_seqs=5)
        run_argv = serving._unit_run_cmd(unit)
        assert "-o" in run_argv, "fixture must carry an override"
        stop = serving.unit_stop_cmd(unit)
        assert "-o" not in stop and "--option" not in stop, stop
        assert serving._flag_after(stop, "--served-model-name") == \
            serving._flag_after(run_argv, "--served-model-name")


def _flag_pairs(argv):
    """{flag: value} for the ``(flag, value)`` pairs in an argv."""
    argv = list(argv or [])
    out = {}
    for i, tok in enumerate(argv):
        if tok.startswith("-") and i + 1 < len(argv) \
                and not argv[i + 1].startswith("-"):
            out[tok] = argv[i + 1]
    return out


# ── the negative controls: the pre-fix argv really is refused ──────────────
class TestPreFixArgvIsRefused:
    def test_the_old_argv_matches_nothing_on_the_same_fake(self, fake_sparkrun):
        """Mutation probe: with the workload up under its declared intent, the
        PRE-FIX stop (recipe/hosts/port) gets sparkrun's real answer — non-zero
        and no stop. Without this the suite above could be green for free."""
        fake_sparkrun.launched(launch_argv(UNIT_W1))
        res = util.run_cmd(PREFIX_STOP)
        assert res["ok"] is False, res
        assert "No running workload matches intent" in (
            res["stderr"] + res["output"])
        assert fake_sparkrun.live(), "the old argv stopped something after all"

    def test_the_pause_reports_that_failure_instead_of_claiming_a_stop(
            self, fleet, tty, fake_sparkrun, monkeypatch):
        """Failure honesty (the card's rule 3): with a stop that cannot match,
        the verb exits non-zero, never says `unit(s) stopped`, names each unit's
        own outcome, and records the pause with stopped=False."""
        monkeypatch.setattr(
            serving, "unit_stop_cmd",
            lambda unit: ["sparkrun", "stop", unit["recipe"], "--hosts",
                          ",".join(unit["nodes"]), "--port", str(unit["port"])])
        fake_sparkrun.launched(launch_argv(UNIT_W1))
        tty("y")
        rc, out = run(["pause", W1])
        assert rc == 1, out
        assert "unit(s) stopped" not in out, out
        assert "w1: NOT stopped" in out, out
        assert "No running workload matches intent" in out, out
        entry = node_pauses.paused(W1)
        # The node IS parked (docs/ops/node-pause.md's ordering rule: the heal
        # paths must skip it even though a unit is up) — only the claim is out.
        assert entry is not None and entry["stopped"] is False
        assert fake_sparkrun.live(), "the unit stayed up, as it must here"

    def test_a_partial_failure_names_the_unit_that_is_still_up(
            self, two_solo_units, tty, fake_sparkrun):
        """Two declared units on one host, ONE of them running under a matching
        intent: the report says `1 of 2 unit(s) did not stop cleanly`, never a
        bare success count — and it still names the unit that came down."""
        fake_sparkrun.launched(launch_argv(UNIT_W3A))
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 1, out
        assert "1 of 2 unit(s) did not stop cleanly" in out, out
        assert "unit(s) stopped" not in out, out
        assert "w3a: stopped" in out, out
        assert "w3b: NOT stopped" in out, out
        stops = fake_sparkrun.stop_argv()
        # w3b's stop WAS issued with its own declared intent — the fake refused
        # it because nothing is running under that intent. That is the honest
        # half of "1 of 2": the verb tried, was refused, and said so.
        assert any("meta-llm/Work-9B worker-model" in " ".join(a)
                   for a in stops), stops
        assert not fake_sparkrun.live(), "the unit that WAS up came down"
        entry = node_pauses.paused(W3)
        assert entry is not None and entry["stopped"] is False


# ── the second consumer: the watchdog auto-restart (lifecycle.py:438) ──────
@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    """A watchdog restart with no lease, no pause and no registered unit — the
    only shape in which `restart_vllm` issues a stop at all (rule 2 refuses a
    cluster-up-owned unit)."""
    from hscc_daemon import heal_lease, state as state_mod
    log_lines = []
    monkeypatch.setattr(lifecycle, "log",
                        lambda msg, level="INFO": log_lines.append(
                            f"{level}: {msg}"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE",
                        str(tmp_path / "block.json"))
    monkeypatch.setattr(heal_lease, "HEAL_LEASE_FILE",
                        str(tmp_path / "heal-lease.json"))
    monkeypatch.setattr(serving, "load_serving", lambda path=None: {})
    lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                   "blocked_at": None, "failures": [],
                                   "auto_restart_count": 0})
    return {"lines": log_lines}


class TestWatchdogStopMirrorsItsStart:
    def test_the_watchdog_stop_matches_the_intent_its_start_launches(
            self, watchdog, fake_sparkrun, monkeypatch):
        """The same bug class on the daemon's own restart path (card rule 2).
        `VLLM_START_CMD` is what the watchdog launches, so the stop has to carry
        its intent flags — read back from that constant, never re-derived."""
        start = ["sparkrun", "run", ORCH_RECIPE, "--cluster", "hscc",
                 "--hosts", serving.PRIMARY_NODE, "--port", "8000",
                 "--no-follow", "--ensure", "--served-model-name",
                 "meta-llm/Orch-8B orchestrator-model", "--tp", "1"]
        monkeypatch.setattr(serving, "VLLM_START_CMD", start)
        monkeypatch.setattr(serving, "VLLM_RECIPE", ORCH_RECIPE)
        fake_sparkrun.launched(start)

        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["ok"] is True, res
        stops = fake_sparkrun.stop_argv()
        assert len(stops) == 1, fake_sparkrun.log()
        stop_flags = _flag_pairs(["sparkrun", "stop"] + stops[0])
        run_flags = _flag_pairs(start)
        for flag in ("--port", "--served-model-name", "--tp"):
            assert stop_flags.get(flag) == run_flags[flag], (
                f"watchdog stop dropped {flag}: {stops[0]} vs {start}")

    def test_the_pair_stays_agreeable_when_the_start_gains_a_flag(
            self, monkeypatch):
        """The seam is `VLLM_START_CMD` itself: a future constant that adds
        `--tp 2` moves the stop with it, with no second edit site."""
        start = ["sparkrun", "run", ORCH_RECIPE, "--cluster", "hscc",
                 "--hosts", "192.0.2.10", "--port", "8100", "--no-follow",
                 "--ensure", "--served-model-name", "a b", "--tp", "2"]
        flags = serving.stop_flags_from_run_argv(start)
        cmd = serving.vllm_stop_cmd(node="192.0.2.10", recipe=ORCH_RECIPE,
                                    **flags)
        assert cmd == ["sparkrun", "stop", ORCH_RECIPE, "--hosts",
                       "192.0.2.10", "--port", "8100",
                       "--served-model-name", "a b", "--tp", "2"]

    def test_the_constants_stop_mirrors_the_constants_start(self, monkeypatch):
        """`_rebuild_vllm_cmds` builds both, and the stop reads the start."""
        monkeypatch.setattr(serving, "VLLM_RECIPE", ORCH_RECIPE)
        serving._rebuild_vllm_cmds()
        start = serving.VLLM_START_CMD
        stop = serving.VLLM_STOP_CMD
        assert stop[1] == "stop" and stop[2] == os.path.expanduser(ORCH_RECIPE)
        for flag in ("--port", "--served-model-name", "--tp"):
            assert (flag in stop) == (flag in start), (start, stop)
            if flag in start:
                assert serving._flag_after(stop, flag) == \
                    serving._flag_after(start, flag)


# ── the posture that must never relax: no --all, no guessed flags ──────────
class TestStopArgvPosture:
    UNITS = [
        {"id": "a", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "b", "role": "worker", "nodes": [W1, W2], "port": 8200,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 2},
        {"id": "c", "role": "orchestrator", "nodes": [ORCH], "port": 8000,
         "recipe": ORCH_RECIPE, "tp": None},
        {"id": "d", "role": "worker", "nodes": [W1], "port": None,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B"},
        {"id": "e", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": "~/recipes/worker.yaml", "model": "meta-llm/Work-8B"},
        {"id": "f", "role": "worker", "nodes": [], "port": 8100,
         "recipe": WORKER_RECIPE},
        {"id": "g", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": ""},
        {"id": "h", "role": "worker", "nodes": [W1], "port": 8100},
    ]

    @pytest.mark.parametrize("unit", UNITS, ids=[u["id"] for u in UNITS])
    def test_no_stop_argv_ever_offers_all(self, unit):
        """`--all` would take the whole fleet down to stop one unit. A stop with
        no provable target is UNBUILDABLE (None) — the caller aborts."""
        cmd = serving.unit_stop_cmd(unit)
        assert cmd is None or "--all" not in cmd, cmd
        assert cmd is None or "-a" not in cmd, cmd
        assert cmd is None or not any(tok.startswith("-o") for tok in cmd)
        assert cmd is None or cmd[:2] == ["sparkrun", "stop"]

    @pytest.mark.parametrize("unit", UNITS[:5], ids=[u["id"] for u in UNITS[:5]])
    def test_the_run_and_stop_intents_are_equal_by_value(self, unit):
        """Whatever the roster shape, the stop never carries an intent flag whose
        value differs from the run's for the SAME unit. One-directional on
        purpose: a run may carry more (``--cluster``, ``--ensure``, ``-o``), and
        a unit with no known port may leave ``--port`` off the stop rather than
        guess it — what is forbidden is DISAGREEMENT."""
        cmd_stop = serving.unit_stop_cmd(unit)
        if cmd_stop is None:
            pytest.skip("no explicit target — unbuildable by design")
        cmd_run = serving._unit_run_cmd(dict(unit))
        stop, run_ = _flag_pairs(cmd_stop), _flag_pairs(cmd_run)
        for flag in ("--hosts", "--port", "--served-model-name", "--tp"):
            if flag in stop:
                assert stop[flag] == run_.get(flag), (unit, cmd_stop, cmd_run)

    def test_the_targetless_stop_is_refused_never_broadened(self):
        assert serving.unit_stop_cmd({"recipe": "", "nodes": [W1]}) is None
        assert serving.unit_stop_cmd({"recipe": WORKER_RECIPE,
                                      "nodes": []}) is None
        assert serving.vllm_stop_cmd(node=W1, recipe="") is None

    def test_no_consumer_builds_a_stop_argv_of_its_own(self):
        """One seam: neither consumer may hand-assemble a ``sparkrun stop``
        argv or hand ``--all`` to the builder. Prose MAY mention ``--all`` (both
        modules quote sparkrun's own ``Must specify TARGET or --all`` error), so
        the pin reads the AST — a ``--all`` may appear in no list literal and no
        stop-builder argument anywhere in these modules."""
        for mod in (npe.__file__, lifecycle.__file__):
            tree = ast.parse(open(mod).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.List):
                    vals = [e.value for e in node.elts
                            if isinstance(e, ast.Constant)]
                    assert "--all" not in vals, (
                        f"{mod}:{node.lineno} puts --all in an argv")
                    assert vals[:2] != ["sparkrun", "stop"], (
                        f"{mod}:{node.lineno} builds a stop argv by hand — use "
                        "serving.unit_stop_cmd / vllm_stop_cmd")
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", "")
                    if name in ("unit_stop_cmd", "vllm_stop_cmd"):
                        args = [e.value for e in node.args
                                if isinstance(e, ast.Constant)]
                        kwargs = [k.arg for k in node.keywords]
                        assert "--all" not in args, (
                            f"{mod}:{node.lineno} passes --all to {name}")
                        assert not any("all" in (k or "") for k in kwargs), (
                            f"{mod}:{node.lineno} broadens {name} with {kwargs}")
