"""The ``proxies/`` apply step must report what the families actually said.

WHAT WAS WRONG (pinned here so it cannot come back). Step 4 of
``apply_template`` appended ``{"step": "proxies/", "status": "ok"}``
UNCONDITIONALLY, after the per-family ``install_proxy`` results had been copied
into ``details`` and read by nobody. So the step claimed ok whatever the
families reported, and ``apply`` printed success over a proxy that never
loaded — on the deployed single-proxy template (``4node/flash-next-all``,
family ``coding``, port 4000) on literally every re-apply while the gateway is
up, and permanently for the second proxy family of the 8 shipped templates that
declare more than one.

WHY A STATUS IS NOW DERIVED, AND WHY ONE CASE STAYS ok. sparkrun keeps ONE
gateway per cache dir and refuses a second ``proxy start`` while it is live
(``src/sparkrun/api/proxy/_ops.py:533-540`` raises ``ProxyAlreadyRunning``,
``src/sparkrun/cli/_proxy.py:181-190`` prints it and exits 1), and the refusal
names the LIVE gateway's port, not the requested one. That port is what makes
the two cases separable, and they are opposite:

  * refusal names the port this family asked for → the port IS served. The
    re-apply is idempotent, the step stays ``ok``, ``already_running`` records
    it, and no error is invented. Reporting this as a failure would flip a
    routine re-apply of the deployed template to ``success: False`` — which also
    stops ``applied_template.json`` from being written (``apply_template`` gates
    that write on success), so doctor would lose the record of what is live.
  * refusal names a DIFFERENT port → nothing listens on the port this family's
    consumers were just pointed at. That is a failure: the step becomes
    ``warn``, apply's non-OK sweep flips ``success`` to False, and the named
    per-family error is in the step instead of hidden in ``details``.

Fakes only: ``subprocess.run`` is stubbed by a fake that MODELS sparkrun's
single-gateway state (one state file, first start wins, later starts get the
CLI's refusal), so nothing execs sparkrun and the suite's fence stays at 0
live-CLI resolutions.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import cluster_template as ct                                    # noqa: E402
import template_intent as ti                                     # noqa: E402

# ── fake topology / template (TEST-NET-1 only) ─────────────────────────────

GATEWAY_IP = "192.0.2.244"
WORKER_IPS = ["192.0.2.246", "192.0.2.247", "192.0.2.248"]

_TWO_FAMILY_BODY = (
    "name: {name}\nversion: 3\n"
    "orchestrator:\n  recipe: o.yaml\n  tp: 2\n"
    f"  nodes: [{GATEWAY_IP}, {WORKER_IPS[0]}]\n"
    "families:\n"
    "  - name: coding\n    proxy: true\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[1]}]\n"
    "  - name: fast\n    proxy: {fast_proxy}\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[2]}]\n"
    "routing:\n  delegation: family-coding\n  compaction: family-fast\n"
)

TWO_PROXY_YAML = _TWO_FAMILY_BODY.format(name="two-proxy", fast_proxy="true")
ONE_PROXY_YAML = _TWO_FAMILY_BODY.format(name="one-proxy", fast_proxy="false")

CLUSTER_OBJ = {"gateway": {"ip": GATEWAY_IP},
               "workers": [{"ip": ip} for ip in WORKER_IPS]}


@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


def _topo():
    return FakeTopo(FakeNode(GATEWAY_IP), [FakeNode(ip) for ip in WORKER_IPS])


def _family(name, port):
    return ti.ResolvedFamily(name=name, proxy_port=port, units=[
        ti.ResolvedUnit("worker", name, "m.yaml", "M", [WORKER_IPS[1]], 8000,
                        1, 1)])


# sparkrun's own wording (cli/_proxy.py:187), including the PID and the port of
# the gateway that is ALREADY up. Keep it verbatim: the matcher under test
# parses exactly this text.
REFUSAL = ("Error: Proxy is already running (PID 4242) on port {port}. "
           "Use --restart to apply new settings, or 'sparkrun proxy stop' "
           "first.")


def _refused(port):
    return MagicMock(returncode=1, stdout="", stderr=REFUSAL.format(port=port))


def _started():
    return MagicMock(returncode=0, stdout="", stderr="")


def _single_gateway(tmp_path):
    """Fake ``subprocess.run`` modelling sparkrun's one-gateway-per-cache-dir.

    The first ``proxy start`` records the gateway and exits 0; every later start
    sees a live gateway in the SAME state file and exits 1 with the CLI's
    refusal naming the port that gateway holds, whatever ``--port`` it was asked
    for. Returns ``(state_file, calls, run)``.
    """
    state = tmp_path / "sparkrun-cache" / "proxy" / "state.yaml"
    state.parent.mkdir(parents=True, exist_ok=True)
    calls = []

    def _run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[:3] == ["sparkrun", "proxy", "start"]:
            if state.exists():
                return _refused(state.read_text())
            state.write_text(argv[argv.index("--port") + 1])
            return _started()
        return _started()

    return state, calls, _run


# ── 1. install_proxy classifies the refusal ────────────────────────────────

class TestInstallProxyClassifiesTheRefusal:
    def test_a_refusal_for_this_very_port_is_idempotent_success(
            self, tmp_path, monkeypatch):
        """The deployed reality: the gateway on :4000 is up, apply re-runs and
        sparkrun refuses. The port IS served — so this is success, not error."""
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        calls = []

        def _run(argv, **kw):
            calls.append([str(a) for a in argv])
            return _refused(4000)

        monkeypatch.setattr(subprocess, "run", _run)
        res = ct.install_proxy(_family("coding", 4000))

        assert res["loaded"] is True, res
        assert res["already_running"] is True, res
        assert res["running_port"] == 4000, res
        assert res["error"] is None, res
        assert res["family"] == "coding" and res["port"] == 4000
        assert res["via"] == "sparkrun-proxy"
        # The idempotent answer is to LEAVE the gateway alone: not to restart it.
        assert "--restart" not in calls[0], calls[0]

    def test_a_refusal_for_another_port_stays_a_failure(self, tmp_path,
                                                        monkeypatch):
        """The hazard shape: this family wants 4001, the single live gateway
        holds 4000. Nothing serves 4001, and the apply must say so."""
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _refused(4000))
        res = ct.install_proxy(_family("fast", 4001))

        assert res["loaded"] is False, res
        assert res["already_running"] is False, res
        assert res["running_port"] == 4000, res
        # sparkrun's own words, unrewritten — they name the port that DOES exist.
        assert "already running" in (res["error"] or "").lower(), res
        assert "port 4000" in res["error"], res

    def test_an_unrecognised_failure_is_never_swallowed(self, tmp_path,
                                                        monkeypatch):
        """Fail closed: no match on the refusal wording means a failure, however
        plausible the message sounds."""
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        monkeypatch.setattr(subprocess, "run",
                            lambda argv, **kw: MagicMock(
                                returncode=1, stdout="",
                                stderr="port already in use, exiting"))
        res = ct.install_proxy(_family("coding", 4000))

        assert res["loaded"] is False, res
        assert res["already_running"] is False, res
        assert res["running_port"] is None, res
        assert res["error"] == "port already in use, exiting", res

    def test_a_blank_failure_still_reports_an_error(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        monkeypatch.setattr(subprocess, "run",
                            lambda argv, **kw: MagicMock(returncode=1,
                                                         stdout="", stderr=""))
        res = ct.install_proxy(_family("coding", 4000))

        assert res["loaded"] is False, res
        assert res["error"], res

    def test_helper_edges(self):
        """The matcher is the whole safety of option (a): it must not classify a
        message it cannot read, and must not bend for spacing or case."""
        ok, live = ct._classify_proxy_refusal(REFUSAL.format(port=4000), 4000)
        assert (ok, live) == (True, 4000)
        ok, live = ct._classify_proxy_refusal(REFUSAL.format(port=4000), 4001)
        assert (ok, live) == (False, 4000)
        ok, live = ct._classify_proxy_refusal(
            "proxy is ALREADY RUNNING (PID 7)  on port  4000.", 4000)
        assert (ok, live) == (True, 4000)
        assert ct._classify_proxy_refusal("", 4000) == (False, None)
        assert ct._classify_proxy_refusal(None, 4000) == (False, None)
        # No port in the message → no claim about which port is served.
        assert ct._classify_proxy_refusal("already running", 4000) == (
            False, None)
        # A family with no proxy port can never be told it is "already served".
        assert ct._classify_proxy_refusal(REFUSAL.format(port=4000),
                                         None) == (False, 4000)


# ── 2. the step is built from the families' answers ────────────────────────

def _action(family, port, loaded, already=False, error=None):
    return {"family": family, "port": port, "loaded": loaded,
            "already_running": already, "running_port": port if already else None,
            "error": error, "via": "sparkrun-proxy"}


class TestProxyStepReport:
    def test_everything_loaded_is_ok(self):
        step = ct._proxy_step_report([_action("coding", 4000, True)], [])
        assert step["status"] == "ok", step
        assert step["proxies"] == 1 and step["failed"] == []
        assert "note" not in step, step

    def test_an_already_running_family_is_ok_and_said_so(self):
        step = ct._proxy_step_report(
            [_action("coding", 4000, True, already=True)], [])
        assert step["status"] == "ok", step
        assert step["already_running"] == [4000], step
        assert step["failed"] == []
        # The honest caveat: the live gateway keeps its old settings.
        assert "NOT pushed into it" in step["note"], step

    def test_a_family_that_did_not_load_is_warn_and_names_it(self):
        step = ct._proxy_step_report(
            [_action("coding", 4000, True),
             _action("fast", 4001, False,
                     error="Error: Proxy is already running (PID 1) on port 4000.\n"
                           "Use --restart ...")],
            [])
        assert step["status"] == "warn", step
        assert [f["family"] for f in step["failed"]] == ["fast"], step
        assert step["failed"][0]["port"] == 4001, step
        assert "already running" in step["failed"][0]["error"], step
        # Multi-line stderr never leaks into the one-line report.
        assert "\n" not in step["failed"][0]["error"], step
        assert "fast(:4001)" in step["note"], step
        # The raw per-family results are still there for whoever wants them.
        assert len(step["details"]) == 2

    def test_no_proxy_families_at_all_is_ok(self):
        step = ct._proxy_step_report([], ["vision"])
        assert step["status"] == "ok" and step["proxies"] == 0
        assert step["pruned_orphans"] == ["vision"]


# ── 3. apply: the claim the card is about ──────────────────────────────────

class TestApplyReportsWhatTheProxiesDid:
    """End-to-end through ``apply_template`` with the single-gateway fake."""

    def _setup(self, tmp_path, monkeypatch, name, yaml_text):
        tdir = tmp_path / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{name}.yaml").write_text(yaml_text)
        cj = tmp_path / "cluster.json"
        cj.write_text(json.dumps(CLUSTER_OBJ))
        hscc = tmp_path / "hscc"
        hscc.mkdir()
        for attr, val in [("TEMPLATE_DIR", tdir), ("CLUSTER_JSON", cj),
                          ("HSCC_DIR", hscc),
                          ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo())
        monkeypatch.setattr(ct, "_http_get_default",
                            lambda _u, api_key=None: json.dumps({"data": [
                                {"id": "orchestrator-model"},
                                {"id": "worker-model"}]}))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30,
                                                    fits=True))
        monkeypatch.setattr(ct, "_provision_models",
                            lambda plan, **k: {"status": "ok",
                                               "provisioned": [],
                                               "note": "test"})
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)
        return {"calls": calls, "state": state, "hscc": hscc}

    def _proxy_step(self, res):
        return next(s for s in res["steps"] if s["step"] == "proxies/")

    def test_a_reapply_while_the_gateway_is_up_is_still_a_success(
            self, tmp_path, monkeypatch):
        """THE deployed shape (one proxy family, gateway already serving :4000).

        Every routine re-apply lands here. It must stay a success — otherwise
        ``applied_template.json``, which the success gate below writes, silently
        stops recording what is live.
        """
        rig = self._setup(tmp_path, monkeypatch, "one-proxy", ONE_PROXY_YAML)
        # The gateway is up BEFORE this apply — the operator's actual state.
        assert ct.install_proxy(_family("coding", 4000))["loaded"] is True
        rig["calls"].clear()

        res = ct.apply_template("one-proxy", confirm=True)
        step = self._proxy_step(res)

        assert step["status"] == "ok", step
        assert step["already_running"] == [4000], step
        assert step["failed"] == [], step
        assert res["success"] is True, res
        # The success gate's side effect still happens.
        assert (rig["hscc"] / "applied_template.json").exists()
        starts = [a for a in rig["calls"] if a[:3] == ["sparkrun", "proxy",
                                                       "start"]]
        assert len(starts) == 1 and "--restart" not in starts[0], starts

    def test_a_proxy_port_that_nothing_serves_fails_the_apply(
            self, tmp_path, monkeypatch):
        """The two-proxy-family shape: the second family's port is never served,
        and apply can no longer print success over it."""
        rig = self._setup(tmp_path, monkeypatch, "two-proxy", TWO_PROXY_YAML)

        res = ct.apply_template("two-proxy", confirm=True)
        step = self._proxy_step(res)

        assert step["status"] == "warn", step
        assert [d["loaded"] for d in step["details"]] == [True, False], step
        assert step["failed"] and step["failed"][0]["family"] == "fast", step
        assert step["failed"][0]["port"] == 4001, step
        assert "fast(:4001)" in step["note"], step
        assert res["success"] is False, res
        # ...and the failure is loud enough to stop the applied-state write.
        assert not (rig["hscc"] / "applied_template.json").exists()
        # The gateway that exists is still only the first family's.
        assert rig["state"].read_text() == "4000"

    def test_a_clean_apply_is_unchanged(self, tmp_path, monkeypatch):
        """Control: a fresh apply with nothing running still says ok, with no
        already_running/failed noise and no note."""
        rig = self._setup(tmp_path, monkeypatch, "one-proxy", ONE_PROXY_YAML)

        res = ct.apply_template("one-proxy", confirm=True)
        step = self._proxy_step(res)

        assert step["status"] == "ok", step
        assert "already_running" not in step, step
        assert step["failed"] == [] and "note" not in step, step
        assert res["success"] is True, res
        assert (rig["hscc"] / "applied_template.json").exists()
