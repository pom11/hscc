"""t_31c8b258 — pane-backed orchestrators get NO gateway notify+wake subs.

The contract: Hermes auto-creates a ``notify+wake`` subscription on
``kanban_create`` unless the creating profile's
``kanban.auto_subscribe_on_create`` gate is off. A pane-backed orchestrator's
wake path is its tmux pane + the hscc orch-waker, so the gate must be off
exactly then. Pane-backed-ness is resolved from Hermes' active-session
registry (``surface == "cli"``); every fixture here is a fake snapshot or a
tmp ``HERMES_HOME``, never the operator's registry or a live tmux server.

The regression shape is the 2026-11-23 duplicate ``hscc-orch``: the same
concern (general ``t_34804e61`` vs hscc ``t_3df8595c``) worked twice because a
gateway ``local`` session was subscribed while the real orch sat in a pane.
"""

import json
import os

import yaml

import generator
import orch_subscribe
import orchestrators


def _leases(*entries):
    return list(entries)


def _lease(surface, session_id):
    return {"surface": surface, "session_id": session_id, "pid": 1234}


def _write_profile(tmp_path, name="hscc-orch", config=None, leases=None):
    pdir = tmp_path / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (pdir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if leases is not None:
        runtime = pdir / "runtime"
        runtime.mkdir()
        (runtime / "active_sessions.json").write_text(
            json.dumps({"entries": leases}), encoding="utf-8")
    return str(pdir)


def _registry_file(pdir, entries):
    runtime = os.path.join(pdir, "runtime")
    os.makedirs(runtime, exist_ok=True)
    with open(os.path.join(runtime, "active_sessions.json"), "w", encoding="utf-8") as fh:
        json.dump({"entries": entries}, fh)


def test_live_cli_lease_is_the_pane_session(tmp_path):
    pdir = _write_profile(tmp_path, leases=[
        _lease("cli", "pane-session-1"),
        _lease("gateway", "local-gateway-session"),
    ])
    assert orch_subscribe.live_pane_sessions("hscc-orch", profile_dir=pdir) == [
        "pane-session-1"]


def test_gateway_lease_does_not_count_as_a_pane(tmp_path):
    pdir = _write_profile(tmp_path, leases=[
        _lease("gateway", "local-gateway-session"),
        _lease("tui", "tui-session"),
    ])
    assert orch_subscribe.live_pane_sessions("hscc-orch", profile_dir=pdir) == []


def test_unreadable_registry_is_not_proof_of_a_pane(tmp_path):
    pdir = _write_profile(tmp_path)  # no runtime/active_sessions.json
    decision = orch_subscribe.subscription_decision(
        "hscc-orch", profile_dir=pdir)
    assert decision["allowed"] is True
    assert decision["reason"] == orch_subscribe.REASON_NO_LIVE_PANE
    assert decision["pane_sessions"] == []


def test_pane_backed_orchestrator_refuses_the_gateway_subscription(tmp_path):
    pdir = _write_profile(tmp_path, leases=[_lease("cli", "pane-session-1")])
    decision = orch_subscribe.subscription_decision(
        "hscc-orch", target_session="local-gateway-session", profile_dir=pdir)
    assert decision["allowed"] is False
    assert decision["reason"] == orch_subscribe.REASON_PANE_BACKED
    assert decision["pane_sessions"] == ["pane-session-1"]
    assert decision["target_is_live_pane"] is False, (
        "a local gateway target is the duplicate-driver shape")


def test_target_session_that_is_the_pane_is_reported_exactly(tmp_path):
    pdir = _write_profile(tmp_path, leases=[_lease("cli", "pane-session-1")])
    decision = orch_subscribe.subscription_decision(
        "hscc-orch", target_session="pane-session-1", profile_dir=pdir)
    assert decision["allowed"] is False
    assert decision["target_is_live_pane"] is True


def test_policy_writes_the_gate_off_and_preserves_foreign_keys(tmp_path):
    pdir = _write_profile(tmp_path, config={
        "model": {"base_url": "http://192.0.2.10:4000/v1"},
        "kanban": {"max_in_progress": 6, "auto_subscribe_on_create": True},
    }, leases=[_lease("cli", "pane-session-1")])
    result = orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)
    assert result["allowed"] is False
    assert result["action"] == "written" and result["changed"] is True
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    assert cfg["kanban"]["auto_subscribe_on_create"] is False
    assert cfg["kanban"]["max_in_progress"] == 6
    assert cfg["model"]["base_url"] == "http://192.0.2.10:4000/v1"


def test_policy_is_idempotent(tmp_path):
    pdir = _write_profile(tmp_path, config={
        "kanban": {"auto_subscribe_on_create": True},
    }, leases=[_lease("cli", "pane-session-1")])
    assert orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)["action"] == "written"
    second = orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)
    assert second["action"] == "unchanged" and second["changed"] is False


def test_paneless_policy_leaves_an_operator_true_alone(tmp_path):
    pdir = _write_profile(tmp_path, config={
        "kanban": {"auto_subscribe_on_create": True},
    }, leases=[_lease("gateway", "local-gateway-session")])
    result = orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)
    assert result["allowed"] is True and result["action"] == "kept"
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        assert yaml.safe_load(fh)["kanban"]["auto_subscribe_on_create"] is True


def test_missing_config_is_reported_never_created(tmp_path):
    pdir = _write_profile(tmp_path, leases=[_lease("cli", "pane-session-1")])
    result = orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)
    assert result["action"] == "missing-config"
    assert result["changed"] is False
    assert not os.path.exists(os.path.join(pdir, "config.yaml"))


def test_unexpected_kanban_shape_is_never_replaced(tmp_path):
    pdir = _write_profile(tmp_path, config={
        "kanban": "operator-scalar",
    }, leases=[_lease("cli", "pane-session-1")])
    result = orch_subscribe.apply_subscription_policy(
        "hscc-orch", profile_dir=pdir)
    assert result["action"] == "unexpected-kanban-shape"
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        assert yaml.safe_load(fh)["kanban"] == "operator-scalar"


def test_registry_callable_is_the_injectable_snapshot():
    seen = []

    def snapshot(profile_home):
        seen.append(profile_home)
        return [_lease("cli", "pane-session-1")]

    decision = orch_subscribe.subscription_decision(
        "hscc-orch", registry=snapshot, hermes_home="/tmp/unused-hermes")
    assert decision["pane_backed"] is True
    assert seen and seen[0].endswith(os.path.join("profiles", "hscc-orch"))


# --------------------------------------------------------------------------- #
# Bootstrap integration: ensure_orchestrator resolves an existing live pane
# --------------------------------------------------------------------------- #

def _isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("HSCC_REGISTRY", raising=False)
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "hermes" / "profiles"))
    return str(tmp_path / "hermes" / "profiles")


def _registry(tmp_path):
    doc = {"projects": [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ]}
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(path)


def test_ensure_orchestrator_turns_the_gate_off_for_a_live_pane(tmp_path, monkeypatch):
    profiles = _isolated_profiles(tmp_path, monkeypatch)
    reg = _registry(tmp_path)
    first = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=reg)
    assert first["subscription"]["action"] == "kept"
    pdir = os.path.join(profiles, "hscc-orch")
    _registry_file(pdir, [_lease("cli", "pane-session-1")])

    second = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=reg)
    assert second["subscription"]["allowed"] is False
    assert second["subscription"]["action"] == "written"
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    assert cfg["kanban"]["auto_subscribe_on_create"] is False


def test_ensure_orchestrator_leaves_a_paneless_profile_untouched(tmp_path, monkeypatch):
    profiles = _isolated_profiles(tmp_path, monkeypatch)
    reg = _registry(tmp_path)
    orchestrators.ensure_orchestrator("hscc", base_identity="BASE", path=reg)
    pdir = os.path.join(profiles, "hscc-orch")
    _registry_file(pdir, [_lease("gateway", "local-gateway-session")])
    second = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=reg)
    assert second["subscription"]["allowed"] is True
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    assert (cfg.get("kanban") or {}).get("auto_subscribe_on_create") is not False
