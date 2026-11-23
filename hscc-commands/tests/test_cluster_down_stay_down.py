"""`/cluster-down` is a SURFACE on the engine now, not a second fleet-down.

t_bf8577f9 (audit F5) rewrote this suite. Its previous subject was t_3a0bdca9
(D1/D5/D6), which had given the slash verb its own per-node `sparkrun stop --all
--hosts <node>` loop, its own already-down markers and its own marker stamp.
That made two implementations of one destructive verb, and the two disagreed:
measured side by side (NOTES-t_bf8577f9.md — fake sparkrun, identical output),
rc 1 carrying both `No running workload matches intent …` and `Error: could not
query <host>: ssh: Permission denied` renders ERROR on the CLI verb, and rendered
`○ … nothing running` plus "All units stopped." here.

These pins are what replaced that loop:

  1. a real down calls the engine EXACTLY ONCE with `dry_run=False` and this
     surface's own marker reason (provenance survives delegation);
  2. the rendered verdict comes from the engine's dict — ✅ / ○ (never claimed
     as a stop) / ❌ with the error tail — and the surface never declares
     "all units stopped" on its own authority;
  3. the stay-down summary appears only when the engine reports the record was
     claimed; the un-claimed path keeps its ⚠️ warning;
  4. the plan (bare call AND `dry-run`) issues nothing and shows the exact
     command the engine will run;
  5. with the engine unavailable the verb REFUSES — no fallback to a different
     stop, and no sparkrun process reached at all.

Fakes only: 192.0.2.x unit nodes, a fake engine module, and a fake `sparkrun`
script on PATH recording any call it receives. The autouse fence from
conftest.py stays armed, so the operator's live CLI remains unreachable here,
and no test writes the operator's real ~/.hscc/expected-up.json.
"""

import os
import shutil

import cmdlib
import __init__ as plugin


SERVING = {
    "version": 1, "port": 8000,
    "units": [
        {"id": "orch-a3b", "role": "orchestrator", "model": "orch",
         "recipe": "~/r/a3b.yaml", "nodes": ["192.0.2.1"]},
        {"id": "worker-2", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["192.0.2.2"], "keepalive": True},
        {"id": "worker-3", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["192.0.2.3"], "keepalive": True},
    ],
}

FLEET_DOWN = ["sparkrun", "stop", "--all", "--cluster", "hscc"]

# What the engine prints around a real stop. This surface renders its own
# command line and its own stay-down summary, so neither line may leak through.
ENGINE_WILL_RUN = "Will run: " + " ".join(FLEET_DOWN)
ENGINE_CONFIRM = ("fleet marked deliberately down — the daemon will not relaunch "
                  "workers, the proxy or the notifier until `hscc cluster up`")


class FakeEngine:
    """Stands in for `hscc-cluster/hscc.py::cmd_cluster_down`, recording calls."""

    STAY_DOWN_CONFIRMATION = ENGINE_CONFIRM

    def __init__(self, res=None, prints=()):
        self.calls = []
        self.prints = list(prints)
        self.res = dict(res if res is not None else {
            "success": True, "returncode": 0, "command": list(FLEET_DOWN),
            "output": "Stopped 3 job(s), 3 container(s) across 3 host(s).",
            "error": "",
            "stay_down_marker": {"cleared": True, "reason": "/cluster-down",
                                 "error": None},
        })

    class _Serving:
        @staticmethod
        def fleet_down_cmd():
            return list(FLEET_DOWN)

    def _load_daemon_serving(self):
        return self._Serving

    def cmd_cluster_down(self, dry_run=False, reason=None):
        self.calls.append({"dry_run": dry_run, "reason": reason})
        for line in self.prints:
            print(line)
        return dict(self.res)


def _fakes(monkeypatch, engine):
    """Fake serving.json + the plugin's engine seam. Returns the engine fake."""
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(plugin, "_cluster_engine", lambda: engine)
    return engine


# ── 1. delegation, and the marker provenance that survives it ────────────────

def test_confirmed_down_calls_the_engine_once_with_this_surfaces_reason(monkeypatch):
    eng = _fakes(monkeypatch, FakeEngine())
    plugin.cmd_cluster_down("confirm")

    assert eng.calls == [{"dry_run": False, "reason": "/cluster-down"}]


def test_the_marker_reason_is_this_surfaces_string_not_the_cli_default(monkeypatch):
    """`/cluster-down` still stamps its OWN reason: the record's presence is the
    safety signal, its text is provenance (see hscc_daemon/verify.py)."""
    eng = _fakes(monkeypatch, FakeEngine())
    plugin.cmd_cluster_down("confirm")

    assert eng.calls[0]["reason"] == plugin.STAY_DOWN_REASON == "/cluster-down"


def test_the_real_seam_reaches_the_real_engine_module():
    """No patched seam: `cmdlib._import_cluster_module('hscc')` must genuinely
    resolve the sibling plugin, or every confirmation would hit the refusal."""
    eng = plugin._cluster_engine()

    assert eng is not None, "engine seam did not resolve hscc-cluster/hscc.py"
    assert callable(getattr(eng, "cmd_cluster_down", None))
    assert eng.STAY_DOWN_REASON == "hscc cluster down"


# ── 2. the verdict is the engine's, not a second opinion ─────────────────────

def test_clean_stop_renders_ok_and_does_not_claim_units_stopped(monkeypatch):
    eng = _fakes(monkeypatch, FakeEngine(prints=[ENGINE_WILL_RUN, ENGINE_CONFIRM]))
    out = plugin.cmd_cluster_down("confirm")

    assert "✅ fleet down — sparkrun exited 0" in out
    assert "❌" not in out
    # The pre-F5 surface ended with "All units stopped." on its own authority.
    assert "All units stopped" not in out
    # sparkrun's per-host text lives in the RESULT (`run_cmd` captures both
    # streams; the engine prints only its own echo), so the renderer must read
    # it from there — passing through only printed lines would show the operator
    # nothing about what actually stopped.
    assert "  sparkrun:" in out
    assert "Stopped 3 job(s)" in out
    # The engine's `Will run:` echo is not repeated next to `command:`.
    assert out.count(" ".join(FLEET_DOWN)) == 1
    # The stay-down line appears ONCE: this surface's STAY_DOWN_SUMMARY (which
    # extends the engine's wording to name the missing `/cluster-up` verb), not
    # the engine's printed confirmation echoed on top of it.
    assert out.count("fleet marked deliberately down") == 1


def test_sparkruns_stderr_reaches_the_operator(monkeypatch):
    """The per-host failure text is in `res["error"]`; a chat surface that
    dropped it would leave the operator with an exit code and nothing to act on."""
    _fakes(monkeypatch, FakeEngine(res={
        "success": False, "returncode": 1, "command": list(FLEET_DOWN),
        "output": "Stopped 2 job(s), 2 container(s) across 2 host(s).",
        "error": "Error: could not query 192.0.2.3: ssh: Connection refused",
        "stay_down_marker": {"cleared": True, "reason": "/cluster-down",
                             "error": None},
    }))
    out = plugin.cmd_cluster_down("confirm")

    assert "  sparkrun (stderr):" in out
    assert "could not query 192.0.2.3: ssh: Connection refused" in out
    assert "❌ fleet down FAILED (sparkrun exit 1)" in out


def test_already_down_is_not_rendered_as_a_stop(monkeypatch):
    _fakes(monkeypatch, FakeEngine(res={
        "success": False, "returncode": 1, "command": list(FLEET_DOWN),
        "output": "Stopped 2 job(s), 2 container(s) across 2 host(s).",
        "error": "Error: No running workload matches intent all on hosts 192.0.2.3",
        "benign_no_workloads": True,
        "benign_note": "nothing to stop — the fleet was already down "
                       "(sparkrun exited nonzero on an already-down host)",
        "stay_down_marker": {"cleared": True, "reason": "/cluster-down",
                             "error": None},
    }))
    out = plugin.cmd_cluster_down("confirm")

    assert "○ fleet down —" in out and "already down" in out
    assert "✅" not in out and "❌" not in out


def test_a_real_failure_keeps_the_error_tail_and_says_failed(monkeypatch):
    """The regression this card exists for. The old classifier called THIS exact
    output `○ nothing running`, because an already-down line rode along with it
    and there was no real-failure denylist here (the engine has one)."""
    _fakes(monkeypatch, FakeEngine(res={
        "success": False, "returncode": 1, "command": list(FLEET_DOWN),
        "output": "Stopped 2 job(s), 2 container(s) across 2 host(s).",
        "error": "Error: could not query 192.0.2.3: ssh: Permission denied "
                 "(publickey)\nError: No running workload matches intent all "
                 "on hosts 192.0.2.4",
        "stay_down_marker": {"cleared": True, "reason": "/cluster-down",
                             "error": None},
    }))
    out = plugin.cmd_cluster_down("confirm")

    assert "❌ fleet down FAILED (sparkrun exit 1)" in out
    assert "could not query 192.0.2.3: ssh: Permission denied" in out
    assert "○" not in out and "nothing running" not in out
    assert "Nothing is marked cleanly down" in out


# ── 3. the stay-down record, rendered from the engine's report ────────────────

def test_summary_line_names_the_way_back_up_in_the_same_line(monkeypatch):
    _fakes(monkeypatch, FakeEngine())
    line = [l for l in plugin.cmd_cluster_down("confirm").splitlines()
            if "fleet marked deliberately down" in l][0]

    # Pinned wording (t_3a0bdca9 D5), plus the escape hatch in the SAME line:
    # there is no /cluster-up verb, so naming only "cluster up" would mislead.
    assert ("the daemon will not relaunch workers, the proxy or the notifier "
            "until `hscc cluster up`") in line
    assert "/cluster-up" in line


def test_uncleared_record_warns_and_offers_the_cli_verb(monkeypatch):
    """Fail-soft stays: a marker problem never breaks the verb, and the operator
    is never told the fleet is marked down when it is not."""
    _fakes(monkeypatch, FakeEngine(res={
        "success": True, "returncode": 0, "command": list(FLEET_DOWN),
        "output": "Stopped", "error": "",
        "stay_down_marker": {"cleared": False, "reason": "/cluster-down",
                             "error": "Read-only file system: '/Users'"},
    }))
    out = plugin.cmd_cluster_down("confirm")

    assert "⚠️" in out
    assert "stay-down marker could NOT be written (Read-only file system" in out
    assert "the daemon MAY relaunch these units" in out
    assert "`hscc cluster down`" in out
    assert "fleet marked deliberately down" not in out


def test_a_surface_with_no_marker_report_says_so_rather_than_claiming_it(
        monkeypatch):
    """An engine with no `stay_down_marker` key (an install older than this card)
    must not be rendered as though the record had been claimed."""
    _fakes(monkeypatch, FakeEngine(res={
        "success": True, "returncode": 0, "command": list(FLEET_DOWN),
        "output": "Stopped", "error": "",
    }))
    out = plugin.cmd_cluster_down("confirm")

    assert "could NOT be written" in out
    assert "fleet marked deliberately down" not in out


# ── 4. the plan paths touch nothing and show the real argv ───────────────────

def test_unconfirmed_invocation_issues_nothing(monkeypatch):
    eng = _fakes(monkeypatch, FakeEngine())
    out = plugin.cmd_cluster_down("")          # no `confirm`

    assert eng.calls == []
    assert "Confirm" in out and "192.0.2.1" in out
    assert "fleet marked deliberately down" not in out


def test_dry_run_prints_the_plan_and_issues_nothing(monkeypatch):
    """Parity with `hscc cluster down --dry-run`, which the slash verb lacked."""
    eng = _fakes(monkeypatch, FakeEngine())
    out = plugin.cmd_cluster_down("dry-run")

    assert eng.calls == []
    assert "dry run (nothing executed)" in out
    assert " ".join(FLEET_DOWN) in out


def test_the_plan_shows_the_same_command_the_engine_will_run(monkeypatch):
    """The audit had to read two source files to compare the plans; the two
    surfaces now show the same argv, read from the same builder."""
    eng = _fakes(monkeypatch, FakeEngine())
    plan = plugin.cmd_cluster_down("dry-run")

    assert " ".join(eng._load_daemon_serving().fleet_down_cmd()) in plan


def test_a_dry_run_token_beats_confirm(monkeypatch):
    """`/cluster-down dry-run confirm` previews, it does not stop: the safe
    reading of a call asking for both (same doctrine as `--yes` vs `--dry-run`)."""
    eng = _fakes(monkeypatch, FakeEngine())
    out = plugin.cmd_cluster_down("dry-run confirm")

    assert eng.calls == [] and "nothing executed" in out


# ── 5. no engine, no stop — and no secret second implementation ──────────────

def test_engine_unavailable_refuses_and_stops_nothing(monkeypatch, tmp_path):
    """Refusal, not fallback: running a *different* fleet-down is the defect."""
    logged = tmp_path / "sparkrun-calls.log"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    script = fakebin / "sparkrun"
    script.write_text(f"#!/bin/sh\necho \"$*\" >> {logged}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    found = shutil.which("sparkrun")
    assert found and os.path.realpath(found) == os.path.realpath(script)

    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(plugin, "_cluster_engine", lambda: None)

    out = plugin.cmd_cluster_down("confirm")

    assert "REFUSED" in out and "nothing was stopped" in out
    assert "`hscc cluster down`" in out
    assert not logged.exists(), "the surface reached a sparkrun process on its own"


def test_the_plugin_holds_no_sparkrun_stop_of_its_own():
    """Structural gate for F5: the second implementation must not creep back.

    `cmd_cluster_down` and its helpers may not build a stop argv or fan one out
    per node — the ONLY fleet-down lives in hscc-cluster/hscc.py. A source-level
    pin is deliberate: the behavioural test above proves only the path a test
    reached, not the absence of a parallel one.
    """
    import inspect

    src = (inspect.getsource(plugin.cmd_cluster_down)
           + inspect.getsource(plugin._down_preview)
           + inspect.getsource(plugin._fleet_down_command)
           + inspect.getsource(plugin._cluster_engine))
    for banned in ("SPARKRUN", "cmdlib._run", '"stop"', "--hosts",
                   "ThreadPoolExecutor", "subprocess"):
        assert banned not in src, f"/cluster-down grew its own stop path: {banned!r}"
