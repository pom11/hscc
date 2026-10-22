"""`hscc project doctor` — the template declaration <-> live agreement section.

Spec §6 (docs/design/2026-10-15-template-declarations.md) C3 / t_3306cd3c:
doctor grows the four-leg declaration-agreement panel. Doctor's own hard rule
applies (a false all-clear is the worst output this tool can produce): every
UNVERIFIED leg is a non-zero exit, never a silent ok — and the section itself
is PURE READ: nothing here writes or restarts.

Fakes only: the legs run through the injectable `_run_agreement` seam (no
hscc_daemon import required in these tests), or against planted tmp files.
"""

import argparse

from flightdeck.commands import doctor as cmd


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "run": None,
        "client": None,
        "boards": ["hscc"],
        "topics": None,
        "repo_check": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(name="hscc", repo="/tmp/hscc", board="hscc", topic=140):
    from flightdeck.core.registry import Project
    return Project(name=name, repo=repo, board=board, topic=topic)


def _healthy_repo_check(ok=True):
    def check(proj, **_):
        return {"ok": ok, "detail": "repo ok"}
    return check


def _legs(*statuses):
    """Fake run_agreement returning one leg per status given."""
    names = ["declaration_agreement.hermes_endpoints",
             "declaration_agreement.proxy_declaration",
             "declaration_agreement.balancer_pair",
             "declaration_agreement.live_model_info"]
    status_to_ok = {"ok": True, "problem": False, "unverified": None}

    def run(leg="all", **kw):
        results = []
        for name, st in zip(names, statuses):
            r = {"name": name, "ok": status_to_ok[st],
                 "detail": f"{name.split('.')[-1]} says {st}"}
            if st != "ok":
                r["remediation"] = "hscc cluster monitor --live"
            results.append(r)
        oks = [r["ok"] for r in results]
        ok = False if False in oks else (None if None in oks else True)
        return {"name": "declaration_agreement", "ok": ok,
                "detail": "legs", "legs": results}
    return run


def _world(**declaration_kwargs):
    projects = [_project()]
    snap: dict = {"repo_check": _healthy_repo_check()}
    if declaration_kwargs:
        snap["declaration"] = True
        snap.update(declaration_kwargs)
    return projects, snap


def test_all_legs_ok_prints_section_and_exits_zero(capsys):
    projects, snap = _world(run_agreement=_legs("ok", "ok", "ok", "ok"))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 0
    assert "template declaration <-> live agreement (spec §6):" in out
    assert "hermes_endpoints [ok]" in out


def test_leg_problem_is_nonzero_and_names_load_instrument(capsys):
    projects, snap = _world(run_agreement=_legs("ok", "problem", "ok", "ok"))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "proxy_declaration [PROBLEM]" in out
    # The §3.2 remediation rides through the adapter into the rendered line.
    assert "hscc cluster monitor --live" in out


def test_leg_unverified_is_nonzero_never_silent_ok(capsys):
    """Fail-closed doctrine (spec §6 / provider_routing): an unreadable leg
    exits non-zero exactly like a disagreement — 'could not check' is not
    'trustworthy'."""
    projects, snap = _world(run_agreement=_legs("ok", "ok", "unverified", "ok"))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "balancer_pair [UNVERIFIED]" in out


def test_missing_peer_module_is_unverified_not_crash(capsys, monkeypatch):
    """A standalone flightdeck install (no hscc_daemon) reports the section
    UNVERIFIED — never claims ok, never raises."""
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("hscc_daemon"):
            raise ImportError("no peer package")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    projects, snap = _world(declaration=True)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "declaration_agreement [UNVERIFIED]" in out
    assert "unavailable" in out


def test_raising_legs_run_is_unverified_not_crash(capsys):
    def boom(leg="all", **kw):
        raise RuntimeError("disk vanished")
    projects, snap = _world(run_agreement=boom)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "unverified" in out and "disk vanished" in out


def test_json_payload_carries_declaration_key(capsys):
    projects, snap = _world(run_agreement=_legs("ok", "problem", "ok", "ok"),
                            json=True)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    import json
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "_declaration" in payload
    legs = payload["_declaration"]
    assert legs["declaration_agreement.proxy_declaration"]["status"] == "problem"


def test_section_absent_when_sentinel_not_set(capsys):
    """The pre-existing triangle-only tests inject no `declaration` kwarg:
    the section is then ABSENT (untouched behaviour), not faked."""
    projects, snap = _world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 0
    assert "spec §6" not in out
    assert "_declaration" not in out


def test_adapter_maps_remediation_into_detail_only_for_bad_legs():
    legs = cmd._declaration_checks(
        _run_agreement=_legs("ok", "problem", "unverified", "ok"))
    assert len(legs) == 4
    by_name = {l["check"]: l for l in legs}
    ok_leg = by_name["declaration_agreement.hermes_endpoints"]
    assert ok_leg["status"] == "ok"
    assert "remediation" not in ok_leg["detail"]
    bad = by_name["declaration_agreement.proxy_declaration"]
    assert bad["status"] == "problem"
    assert "remediation:" in bad["detail"]


def test_default_seam_runs_real_module_against_planted_state(tmp_path,
                                                             monkeypatch):
    """The production wiring (no injected run_agreement) against planted tmp
    files: an applied record with NO declarations and no uniform_pool => all
    five legs resolve to documented not-applicable => every status ok.

    Hermetic inputs for EVERY leg, including the fifth (t_be4b94ca):
    ``serving.SERVING_JSON`` points at a path that does not exist, so the
    unit-alias leg reports "serving.json declares no units" instead of
    reading (and probing!) the operator's live serving.json."""
    import json as _json
    from hscc_daemon import serving as _serving
    applied = tmp_path / "applied.json"
    applied.write_text(_json.dumps({"template": "x", "uniform_pool": False}))
    monkeypatch.setattr(_serving, "SERVING_JSON", str(tmp_path / "no-serving.json"))
    legs = cmd._declaration_checks(
        applied_state_path=str(applied),
        proxy_config=str(tmp_path / "absent.yaml"),
        litellm_config=str(tmp_path / "absent2.yaml"),
        balancer_module=str(tmp_path / "absent3.py"))
    assert [l["status"] for l in legs] == ["ok"] * 5
