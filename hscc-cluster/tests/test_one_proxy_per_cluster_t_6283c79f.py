"""t_6283c79f — sparkrun keeps ONE gateway per cache dir, so a second
``proxy: true`` family can only ever report ``loaded=False`` and its consumers
get a URL on a port nothing listens on.

This file PINS the hazard; it does not fix it. The fix is a design decision
(option (c) below) and is carded separately — see the card comment for the two
rejected options and why.

WHAT THE CODE DOES TODAY. ``install_proxy`` is called once per resolved family
that has a ``proxy_port``, and ``template_intent.resolve`` mints a DISTINCT port
per proxy family from ``base_proxy_port``. sparkrun does not keep one gateway
per port — it keeps ONE per cache dir:

  * ``sparkrun/proxy/_supervisor.py`` ``GatewayState.__init__`` binds
    ``state_dir = resolve_sparkrun_cache_dir() / "proxy"`` (no port in it) and
    ``log_path = state_dir / log_name``;
  * ``is_running()`` reads the PID out of that one shared ``state.yaml`` and
    calls ``process_exists`` — port-agnostic;
  * ``sparkrun/api/proxy/_ops.py`` ``_start`` therefore raises
    ``ProxyAlreadyRunning`` for the second family's start *before* comparing
    ports, and ``sparkrun/cli/_proxy.py`` answers that with ``sys.exit(1)``.

So the second proxy family's ``install_proxy`` returns ``loaded=False``, apply's
``proxies/`` step still says ``"status": "ok"``
(``cluster_template.py`` Step 4 appends it unconditionally) and the apply result
is ``success: True`` — while ``_proxy_front_for_target`` writes
``http://localhost:<refused port>/v1`` into consumer configs.

WHY THE OBVIOUS FIXES ARE WRONG.
  (a) "validate: at most one ``proxy: true`` family" — implemented, measured,
      then reverted. It turns 4 existing tests red (two
      ``test_template_declarations`` fixtures whose families both default to
      ``proxy: true``, and the v2 parse golden over shipped ``coding-plus-fast``)
      and, for the 8 shipped templates in this shape, it silently re-aims every
      ``routing:`` target of the losing family — ``_routing_target_endpoint``
      falls back to ``units[0].node:units[0].port`` when ``proxy_port is None``,
      so ``4node/quality``'s ``compaction: family-fast`` stops going through the
      proxy and becomes a node pin (which is exactly what the flag exists to
      avoid), and a v4 ``endpoints:`` target under ``uniform_pool`` becomes a
      hard plan error (``validate_resolved_plan``'s §5 leg). The declaration
      carries real intent; suppressing it is not a fix.
  (b) "pass a per-family ``--cache-dir``/instance" — ``sparkrun proxy start``
      has no such flag (its options are ``--port --host --master-key
      --hosts/--hosts-file --foreground --no-auto-discover --discovery-backend
      --discover-interval --discover-removal-grace-sweeps --gateway --restart
      --clear-cluster --dry-run``), ``gateway_name`` is only a label written
      into the state file, and the only lever — the cache-dir env override —
      relocates sparkrun's WHOLE cache dir, so ``install_proxy``'s own
      ``--cluster hscc`` would fail with "Unknown cluster". Not reachable from
      hscc.

THE FIX THAT FITS (c): one gateway is the model, so every proxy family must
resolve to the SAME port — ``resolve`` stops minting a port per family, Step 4
starts the one gateway once, and the per-family URLs point at the live gateway
instead of a per-family ghost port. That keeps ``proxy: true`` (and every
``family-<name>`` target) meaningful and deletes the dead-port class entirely.

Fakes only: ``subprocess.run`` is stubbed by a fake that MODELS sparkrun's
single-gateway state, so nothing execs sparkrun and the suite's fence stays at
0 live-CLI resolutions.
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

# ── fake topology / cluster.json (TEST-NET-1 only) ─────────────────────────

GATEWAY_IP = "192.0.2.244"
WORKER_IPS = ["192.0.2.246", "192.0.2.247", "192.0.2.248"]

# The shipped shape (see 4node/quality.yaml): two proxy families, a routing
# block whose compaction target is the SECOND one.
TWO_PROXY_YAML = (
    "name: two-proxy\nversion: 3\n"
    "orchestrator:\n  recipe: o.yaml\n  tp: 2\n"
    f"  nodes: [{GATEWAY_IP}, {WORKER_IPS[0]}]\n"
    "families:\n"
    "  - name: coding\n    proxy: true\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[1]}]\n"
    "  - name: fast\n    proxy: true\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[2]}]\n"
    "routing:\n  delegation: family-coding\n  compaction: family-fast\n"
)

ONE_PROXY_YAML = TWO_PROXY_YAML.replace(
    "  - name: fast\n    proxy: true\n", "  - name: fast\n    proxy: false\n")


@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


def _topo():
    return FakeTopo(FakeNode(GATEWAY_IP),
                    [FakeNode(ip) for ip in WORKER_IPS])


CLUSTER_OBJ = {"gateway": {"ip": GATEWAY_IP},
               "workers": [{"ip": ip} for ip in WORKER_IPS]}


# ── the fake sparkrun: ONE gateway per cache dir ───────────────────────────

REFUSAL = ("Error: Proxy is already running (PID 4242) on port {port}. "
           "Use --restart to apply new settings, or 'sparkrun proxy stop' first.")


def _single_gateway(tmp_path):
    """A fake ``subprocess.run`` modelling sparkrun's gateway state.

    One state file = one cache dir's ``proxy/state.yaml``: the first ``proxy
    start`` records the gateway and exits 0; every later start sees a live
    gateway in that SAME file and exits 1 with the CLI's own refusal, whatever
    ``--port`` it was given (that is what ``is_running()`` being port-agnostic
    means). ``--cache-dir`` / ``--instance`` are deliberately not honoured — the
    real CLI has no such flag, so a test cannot pass one either.

    Returns ``(state_file, calls, run)`` — patch ``subprocess.run`` with ``run``.
    """
    state = tmp_path / "sparkrun-cache" / "proxy" / "state.yaml"
    state.parent.mkdir(parents=True, exist_ok=True)
    calls = []

    def _run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[:3] == ["sparkrun", "proxy", "start"]:
            if state.exists():
                return MagicMock(returncode=1, stdout="",
                                 stderr=REFUSAL.format(port=state.read_text()))
            state.write_text(argv[argv.index("--port") + 1])
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return state, calls, _run


def _family(name, port):
    return ti.ResolvedFamily(name=name, proxy_port=port, units=[
        ti.ResolvedUnit("worker", name, "m.yaml", "M", [WORKER_IPS[1]], 8000,
                        1, 1)])


# ── 1. the constraint itself, through install_proxy ────────────────────────

class TestSparkrunKeepsOneGateway:
    """Nothing here depends on any hscc fix: this is what sparkrun does."""

    def test_second_proxy_family_is_refused_and_reports_loaded_false(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)

        first = ct.install_proxy(_family("coding", 4000))
        second = ct.install_proxy(_family("fast", 4001))

        assert first["loaded"] is True, first
        assert second["loaded"] is False, second
        assert "already running" in (second["error"] or "").lower(), second
        assert second["port"] == 4001
        # the refusal names the FIRST family's port: liveness is not per port
        assert state.read_text() == "4000"

    def test_install_proxy_never_asks_for_a_separate_gateway_instance(
            self, tmp_path, monkeypatch):
        """Two starts, two ports, no per-instance flag — which is why the fix
        cannot be 'hand sparkrun a second instance'."""
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)

        ct.install_proxy(_family("coding", 4000))
        ct.install_proxy(_family("fast", 4001))

        starts = [a for a in calls if a[:3] == ["sparkrun", "proxy", "start"]]
        assert len(starts) == 2, calls
        assert [a[a.index("--port") + 1] for a in starts] == ["4000", "4001"]
        for a in starts:
            assert "--cache-dir" not in a and "--instance" not in a, a
            assert "--restart" not in a, "apply must not restart a live gateway"

    def test_consumer_url_for_the_second_family_points_at_a_dead_port(
            self, tmp_path, monkeypatch):
        """The damage, from the SAME resolver apply uses.

        ``_proxy_front_for_target`` answers ``family-<name>`` with that family's
        own ``proxy_port`` — here a port whose start the gateway refused."""
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)
        plan = ti.ResolvedPlan(
            template="two-proxy",
            orchestrator=ti.ResolvedUnit("orchestrator", "orch", "o.yaml", "O",
                                         [GATEWAY_IP], 8000, 2, 1),
            families=[_family("coding", 4000), _family("fast", 4001)])

        ct.install_proxy(_family("coding", 4000))
        ct.install_proxy(_family("fast", 4001))

        resolved = ct._proxy_front_for_target("family-fast", plan)
        assert resolved is not None
        assert resolved[0] == "http://localhost:4001/v1", resolved
        assert state.read_text() == "4000", "no gateway ever bound 4001"


# ── 2. the hazard pin: apply reports success over the dead port ─────────────

class TestApplyOverADeadProxyPort:
    """WHAT TODAY'S APPLY DOES, pinned so the fix cannot land silently.

    Every assertion here is a BUG (see the module docstring, option (c)). When
    the one-gateway-per-cluster fix lands, this class is expected to change:
    one ``proxy start`` argv, no ``loaded=False``, and ``family-fast`` resolving
    to the single live port. If it passes unchanged after that change, the fix
    did not reach the apply path."""

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
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(ct, "_provision_models",
                            lambda plan, **k: {"status": "ok",
                                               "provisioned": [],
                                               "note": "test"})
        return ct, {"calls": calls, "state": state, "hscc": hscc}

    def _proxy_step(self, res):
        return next(s for s in res["steps"] if s["step"] == "proxies/")

    def test_apply_reports_success_though_the_second_proxy_never_loaded(
            self, tmp_path, monkeypatch):
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "two-proxy",
                                  TWO_PROXY_YAML)
        res = ct_mod.apply_template("two-proxy", confirm=True)

        # BUG: the apply succeeds.
        assert res["success"] is True, res
        starts = [a for a in rig["calls"]
                  if a[:3] == ["sparkrun", "proxy", "start"]]
        # BUG: two starts for one gateway, on two distinct ports.
        assert [a[a.index("--port") + 1] for a in starts] == ["4000", "4001"]
        step = self._proxy_step(res)
        # BUG: the step calls itself ok while a family did not load.
        assert step["status"] == "ok", step
        loaded = {d["port"]: d["loaded"] for d in step["details"]}
        assert loaded == {4000: True, 4001: False}, step
        assert any(d["port"] == 4001 and "already running"
                   in (d["error"] or "").lower() for d in step["details"]), step
        # and the gateway that exists is only the first family's
        assert rig["state"].read_text() == "4000"

    def test_single_proxy_template_is_the_control(
            self, tmp_path, monkeypatch):
        """The deployed shape (one proxy family) is unaffected: one start, one
        loaded detail. The bug is exactly the second family."""
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "one-proxy",
                                  ONE_PROXY_YAML)
        res = ct_mod.apply_template("one-proxy", confirm=True)

        assert res["success"] is True, res
        starts = [a for a in rig["calls"]
                  if a[:3] == ["sparkrun", "proxy", "start"]]
        assert len(starts) == 1, starts
        step = self._proxy_step(res)
        assert [d["loaded"] for d in step["details"]] == [True], step
