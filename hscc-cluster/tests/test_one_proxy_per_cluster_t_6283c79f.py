"""t_6283c79f — sparkrun keeps ONE gateway per cache dir, so a second
``proxy: true`` family could only ever report ``loaded=False`` and its consumers
got a URL on a port nothing listens on.

This file PINS the constraint (the fake below MODELS sparkrun's single-gateway
state) and the shape the fix had to land in. The two rejected options and the
measurements that killed them are recorded below so they are not re-litigated.

WHAT THE BUG DID (measured on `origin/main` before the fix, still pinned below
so it cannot come back). ``install_proxy`` was called once per resolved family
that has a ``proxy_port``, and ``template_intent.resolve`` minted a DISTINCT port
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

So the second proxy family's ``install_proxy`` returned ``loaded=False``, apply's
``proxies/`` step said ``"status": "ok"`` anyway
(``cluster_template.py`` Step 4 appended it unconditionally) and the apply result
was ``success: True`` — while ``_proxy_front_for_target`` wrote
``http://localhost:<refused port>/v1`` into consumer configs.

The REPORTING half was fixed first (t_4a15fb46): apply's ``proxies/`` status is
now DERIVED from those family results (``cluster_template._proxy_step_report``)
instead of a hardcoded ``"ok"``, so an unserved port makes the step ``warn`` and
the apply ``success: False``. The DEAD-PORT half is fixed here: every proxy
family resolves to the ONE gateway, apply starts it once, and no consumer is
ever pointed at a port nothing bound.

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

THE FIX (c) — SHIPPED: one gateway is the model, so every proxy family resolves
to the SAME port — ``resolve`` mints no port per family, Step 4 starts the one
gateway ONCE per apply, and the per-family URLs name the live gateway instead of
a per-family ghost port. That keeps ``proxy: true`` (and every ``family-<name>``
target) meaningful and deletes the dead-port class entirely. ``TestApplyOverTheOneGateway``
below is this file's hazard pin in its post-fix shape: it is the half that had to
flip, and it fails if a per-family port or a per-family start ever comes back.

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

    def test_a_plan_naming_a_second_port_resolves_to_it_verbatim(
            self, tmp_path, monkeypatch):
        """Why the MINT SITE is the load-bearing place, not the resolver helpers.

        ``_proxy_front_for_target`` reports whatever port the plan carries — it
        cannot know the gateway refused that port. So a per-family port minted
        upstream becomes a dead URL downstream with nothing in between
        complaining: the fix has to be in ``resolve``, and a regression there is
        what ``TestApplyOverTheOneGateway`` below catches.
        """
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


# ── 2. the hazard pin, post-fix: apply drives the ONE gateway ────────────────

class TestApplyOverTheOneGateway:
    """THE FIX, pinned end to end — the flip of this file's hazard pin.

    Before the fix this class asserted the BUG: two ``proxy start`` argv on two
    distinct ports, ``loaded == {4000: True, 4001: False}``, and
    ``family-fast`` resolving to the port the gateway never bound. Every one of
    those is inverted here: ONE start for the whole apply, no ``loaded=False``,
    both families' URLs naming the live gateway — including in the
    ``config.yaml`` apply actually wrote. Re-introducing a per-family port or a
    per-family start turns this class red.

    The ``proxies/`` status stays DERIVED from the family reports (t_4a15fb46,
    ``cluster_template._proxy_step_report``): with a single gateway that loaded,
    there is no failed family, so the step is ``ok`` and the apply succeeds —
    asserted below rather than assumed.
    """

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

    def test_apply_starts_the_one_gateway_once_for_two_proxy_families(
            self, tmp_path, monkeypatch):
        """Both halves: one gateway, and a report derived from what loaded.

        ``proxies/`` is built by ``_proxy_step_report`` (t_4a15fb46) rather than
        appended as a hardcoded ``"ok"``; because the one gateway loaded for
        both families, the derived status is ``ok`` and the apply succeeds.
        """
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "two-proxy",
                                  TWO_PROXY_YAML)
        res = ct_mod.apply_template("two-proxy", confirm=True)

        # The reporting half still derives success from what loaded: one
        # gateway serves both families, so nothing failed.
        assert res["success"] is True, res
        starts = [a for a in rig["calls"]
                  if a[:3] == ["sparkrun", "proxy", "start"]]
        # ONE start for the whole apply — sparkrun keeps one gateway per cache
        # dir, so a second one could only be a refusal.
        assert len(starts) == 1, starts
        assert [a[a.index("--port") + 1] for a in starts] == ["4000"], starts
        step = self._proxy_step(res)
        # Derived from the family reports (t_4a15fb46): one gateway, one loaded
        # detail, and nothing failed.
        assert step["status"] == "ok", step
        assert step["failed"] == [], step
        loaded = {d["port"]: d["loaded"] for d in step["details"]}
        assert loaded == {4000: True}, step
        # The report reads as ONE gateway with N consumers, not N gateways.
        assert step["details"][0]["serving"] == ["coding", "fast"], step
        # and the gateway that exists serves both families
        assert rig["state"].read_text() == "4000"

    def test_the_written_config_names_the_live_gateway_for_both_families(
            self, tmp_path, monkeypatch):
        """The damage asserted where it actually landed: the ``config.yaml`` on
        disk, not the resolver one level above it.

        ``routing: compaction: family-fast`` names the SECOND proxy family — the
        one whose start used to be refused — so every URL written for it used to
        point at :4001, a port nothing ever bound.
        """
        import yaml

        ct_mod, rig = self._setup(tmp_path, monkeypatch, "two-proxy",
                                  TWO_PROXY_YAML)
        res = ct_mod.apply_template("two-proxy", confirm=True)
        assert res["success"] is True, res

        written = (rig["hscc"] / "config.yaml").read_text()
        assert "4001" not in written, written
        cfg = yaml.safe_load(written)
        by_name = {p["name"]: p for p in cfg["providers"]}
        for fam in ("family-coding", "family-fast"):
            assert by_name[fam]["base_url"] == "http://localhost:4000/v1", (
                fam, by_name[fam])
        # The consumer that routes to the second family lands on the same
        # gateway, still as a proxy URL (option (a) would have made it a node
        # pin — that is the shape this assertion protects).
        assert cfg["auxiliary"]["compression"]["base_url"] == (
            "http://localhost:4000/v1"), cfg["auxiliary"]["compression"]
        assert rig["state"].read_text() == "4000"
        # Per-family bookkeeping dirs stay per-family — they are hscc's own
        # record, and they do NOT imply a gateway each.
        for fam in ("coding", "fast"):
            assert (rig["hscc"] / "proxies" / fam).is_dir(), fam

    def test_the_preview_names_the_gateway_once(self, tmp_path, monkeypatch):
        """The same template, as previewed: one gateway row, not N of them.

        Every proxy family resolves to the same port now, so a per-family
        ``port 4000`` row would read as N gateways that can never exist. The
        rows keep what IS per-family (the node span) and say nothing about a
        port; the summary names the one gateway and who it serves.
        """
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "two-proxy",
                                  TWO_PROXY_YAML)
        prom = ct_mod.preview_template("two-proxy")["changes"]
        row = next(c for c in prom if c["file"] == "proxies/")
        assert row["summary"] == "1 gateway on :4000 serving coding, fast", row
        assert all("port" not in d for d in row["details"]), row["details"]
        assert len(row["details"]) == 2, row["details"]
        fam_lines = [l for l in next(
            c for c in prom if c["file"] == "config.yaml")["details"]
            if l.strip().startswith("family-")]
        assert len(fam_lines) == 2, fam_lines
        assert all("(shared gateway)" in l for l in fam_lines), fam_lines
        assert "4001" not in json.dumps(prom), prom

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
