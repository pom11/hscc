"""t_6283c79f — a template may declare at most one ``proxy: true`` family.

WHY. ``install_proxy`` (``cluster_template.py``) is called once per resolved
family that has a ``proxy_port``, and ``template_intent.resolve`` mints a
DISTINCT port per proxy family from ``base_proxy_port``. sparkrun does not keep
one gateway per port — it keeps ONE gateway per cache dir:

  * ``sparkrun/proxy/_supervisor.py`` ``GatewayState.__init__`` binds
    ``state_dir = resolve_sparkrun_cache_dir() / "proxy"`` (no port in it), and
    ``log_path = state_dir / log_name``;
  * ``is_running()`` reads the PID out of that one shared ``state.yaml`` and
    calls ``process_exists`` — port-agnostic;
  * ``sparkrun/api/proxy/_ops.py`` ``_start`` therefore raises
    ``ProxyAlreadyRunning`` for the second family's start *before* comparing
    ports, and ``sparkrun/cli/_proxy.py`` answers that with ``sys.exit(1)``.

So for the second proxy family ``install_proxy`` can only ever return
``loaded=False``, while ``_proxy_front_for_target`` still writes
``http://localhost:<that port>/v1`` into consumer configs — a route to a port
with nothing listening. Eight of the shipped templates are in that shape.

THE GUARD. ``_structural_validate`` rejects a template declaring more than one
``proxy: true`` family, so ``apply_template``'s pre-flight gate blocks it with a
named error before any write, stop or start — instead of reporting a silent
``loaded=False`` under a ``proxies/`` step that says ``"status": "ok"``.

Fakes only: ``subprocess.run`` is stubbed by a fake that MODELS sparkrun's
single-gateway state (one state file per cache dir, a running gateway makes any
further start exit 1 with the CLI's own wording). No real sparkrun is exec'd and
the suite's sparkrun fence stays at 0 live-CLI resolutions.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import cluster_template as ct                                    # noqa: E402
import template_intent as ti                                     # noqa: E402

# ── fake topology / cluster.json (TEST-NET-1 only) ─────────────────────────

GATEWAY_IP = "192.0.2.244"
WORKER_IPS = ["192.0.2.246", "192.0.2.247", "192.0.2.248", "192.0.2.249"]


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


CLUSTER_OBJ = {"gateway": {"ip": GATEWAY_IP},
               "workers": [{"ip": ip} for ip in WORKER_IPS]}

# One proxy family per declared intent, otherwise the same shape as the
# known-good fixture in test_apply.py (explicit spans, version 3).
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
)

# `proxy:` DEFAULTS to true (``FamilyIntent.proxy: bool = True``), so the extra
# families must say `proxy: false` EXPLICITLY — a template that merely omits the
# key has two proxy families and is exactly what the guard rejects. (That this
# fixture initially omitted it and got blocked is the guard working.)
ONE_PROXY_YAML = TWO_PROXY_YAML.replace(
    "  - name: fast\n    proxy: true\n", "  - name: fast\n    proxy: false\n")

# Three families, ONE proxy — the guard must not fire on this shape.
THREE_FAMILY_ONE_PROXY_YAML = (
    "name: three-one-proxy\nversion: 3\n"
    "orchestrator:\n  recipe: o.yaml\n  tp: 2\n"
    f"  nodes: [{GATEWAY_IP}, {WORKER_IPS[0]}]\n"
    "families:\n"
    "  - name: coding\n    proxy: true\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[1]}]\n"
    "  - name: fast\n    proxy: false\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[2]}]\n"
    "  - name: review\n    proxy: false\n    models:\n"
    "      - recipe: m.yaml\n        tp: 1\n"
    f"    nodes: [{WORKER_IPS[3]}]\n"
)


# ── the fake sparkrun: ONE gateway per cache dir ───────────────────────────

REFUSAL = ("Error: Proxy is already running (PID 4242) on port {port}. "
           "Use --restart to apply new settings, or 'sparkrun proxy stop' first.")


def _single_gateway(tmp_path):
    """A fake ``subprocess.run`` that models sparkrun's gateway state.

    One state file = one cache dir's ``proxy/state.yaml``: the first ``proxy
    start`` records the gateway and exits 0; every later start sees a live
    gateway in that SAME file and exits 1 with the CLI's own refusal, whatever
    ``--port`` it was given (that is what ``is_running()`` being port-agnostic
    means). ``--cache-dir`` / ``--instance`` are deliberately not honoured: the
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


# ── 1. the sparkrun-side fact the guard rests on ───────────────────────────

class TestSparkrunKeepsOneGateway:
    """Green before AND after the guard: this is the constraint being enforced,
    exercised through ``install_proxy`` itself."""

    def test_second_proxy_family_is_refused_and_reports_loaded_false(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        state, calls, run = _single_gateway(tmp_path)
        monkeypatch.setattr(subprocess, "run", run)

        first = ct.install_proxy(_family("coding", 4000))
        second = ct.install_proxy(_family("fast", 4001))

        assert first["loaded"] is True, first
        assert second["loaded"] is False, second
        assert "already running" in (second["error"] or "").lower()
        # and the port that was refused is the one the second family was given
        assert second["port"] == 4001
        assert state.read_text() == "4000"

    def test_install_proxy_never_asks_for_a_separate_gateway_instance(
            self, tmp_path, monkeypatch):
        """No ``--cache-dir`` / ``--instance`` in the argv — the CLI has no such
        flag, which is why the fix cannot be 'pass a per-family instance'."""
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

    def test_consumer_url_for_the_second_family_points_at_the_dead_port(
            self, tmp_path, monkeypatch):
        """The consequence, from the SAME resolver apply uses.

        ``_proxy_front_for_target`` answers a ``family-<name>`` target with the
        family's own ``proxy_port`` — so a consumer gets a URL on the port whose
        start the fake gateway state refused."""
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
        # the single recorded gateway serves the FIRST family's port only
        assert state.read_text() == "4000"


# ── 2. the guard: the apply gate blocks a two-proxy template ───────────────

class TestApplyBlocksMoreThanOneProxyFamily:
    """RED before the guard: apply walks into Step 4, gets ``loaded=False`` for
    the second family, and still reports success under a ``proxies/`` step
    whose status is ``"ok"``. GREEN after: the gate blocks with a named error
    before anything is written, stopped or started."""

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
        prov = {"n": 0}

        def _prov(plan, **k):
            prov["n"] += 1
            return {"status": "ok", "provisioned": [], "note": "test"}
        monkeypatch.setattr(ct, "_provision_models", _prov)
        return ct, {"calls": calls, "state": state, "prov": prov, "hscc": hscc}

    def test_apply_blocks_and_starts_nothing(self, tmp_path, monkeypatch):
        _, rig = self._setup(tmp_path, monkeypatch, "two-proxy", TWO_PROXY_YAML)
        res = ct.apply_template("two-proxy", confirm=True)

        assert res["status"] == "blocked", res
        assert res["success"] is False
        # a NAMED error, not a silent per-family loaded=False
        err = " || ".join(res["errors"])
        assert "coding" in err and "fast" in err, err
        assert "proxy: true" in err, err
        assert "one gateway" in err.lower(), err
        # blocked BEFORE any stop / start / provision / write
        assert rig["prov"]["n"] == 0
        assert [a for a in rig["calls"]
                if a[:3] == ["sparkrun", "proxy", "start"]] == [], rig["calls"]
        assert not (rig["hscc"] / "serving.json").exists()
        assert not (rig["hscc"] / "models.json").exists()

    def test_gate_is_offline_and_matches_standalone_validate(
            self, tmp_path, monkeypatch):
        """The rule is a property of the TEMPLATE, so the offline structural
        layer must catch it — `hscc template validate --structural-only` and
        apply's own gate then agree (apply's errors are exactly validate's)."""
        ct_mod, _ = self._setup(tmp_path, monkeypatch, "two-proxy",
                                TWO_PROXY_YAML)
        val = ct_mod.validate_template("two-proxy", structural_only=True)
        res = ct_mod.apply_template("two-proxy", confirm=True)

        assert val["ok"] is False and val["structural"]["ok"] is False, val
        assert any("proxy: true" in e for e in val["structural"]["errors"]), val
        assert res["errors"] == (val["structural"]["errors"]
                                 + val["placement"]["errors"])

    def test_one_proxy_family_still_applies(self, tmp_path, monkeypatch):
        """The guard must not fire on the deployed shape (exactly one proxy)."""
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "one-proxy",
                                  ONE_PROXY_YAML)
        res = ct_mod.apply_template("one-proxy", confirm=True)

        assert res["success"] is True, res
        assert rig["prov"]["n"] == 1
        starts = [a for a in rig["calls"]
                  if a[:3] == ["sparkrun", "proxy", "start"]]
        assert len(starts) == 1, starts
        assert starts[0][starts[0].index("--port") + 1] == "4000"

    def test_three_families_one_proxy_still_applies(self, tmp_path, monkeypatch):
        ct_mod, rig = self._setup(tmp_path, monkeypatch, "three-one-proxy",
                                  THREE_FAMILY_ONE_PROXY_YAML)
        res = ct_mod.apply_template("three-one-proxy", confirm=True)

        assert res["success"] is True, res
        assert len([a for a in rig["calls"]
                    if a[:3] == ["sparkrun", "proxy", "start"]]) == 1


# ── 3. the shipped template library must be honest about the rule ──────────

def test_no_shipped_template_declares_two_proxy_families():
    """The census the guard is enforced over.

    Counted through the repo's own intent parser, NOT by grepping
    ``proxy: true``: ``FamilyIntent.proxy`` defaults to **true**, so a template
    whose extra families simply omit the key also declares two proxy families
    and must be caught here."""
    tdir = PLUGIN_DIR / "templates"
    offenders = []
    for path in sorted(tdir.rglob("*.yaml")):
        import yaml
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception:                                     # noqa: BLE001
            continue
        if not isinstance(raw.get("families"), list):
            continue
        tpl = ct._ti().ClusterTemplate.from_dict(raw)
        names = [f.name for f in tpl.families if f.proxy]
        if len(names) > 1:
            offenders.append(f"{path.relative_to(tdir)}: {names}")
    assert offenders == [], (
        f"{len(offenders)} shipped template(s) declare more than one proxy "
        "family, which sparkrun cannot serve (one gateway per cache dir):\n  "
        + "\n  ".join(offenders))
