"""Doctor coverage for the role-SOUL identity check (t_c823ebc9).

Two layers are tested here:

1. The BRIDGE (doctor_role_soul): it must resolve the hscc-roles sibling in both
   layouts, must APPEND its path (so bootstrap's own same-named modules stay
   authoritative), and must come back an honest ERROR when the sibling is not
   importable — never an all-clear, never a crash.

2. The SEAM inside run_doctor(): the check's real target is SOUL.md under
   rolelib.PROFILES_DIR, which in the operator's env is the LIVE profile root.
   Same fence as the node-recovery check's _ssh_factory: inside a pytest process
   the scan must be injected, or the check renders as a refusal. A doctor test
   that silently audited the operator's 25 real profiles would be the exact
   environment-dependent failure this card exists to avoid.

Fixtures are tmp dirs; the offending fixture's `## Role:` header DISAGREES with
its dir name, so the suite cannot pass vacuously.
"""

import json
import os
import sys

import yaml

import doctor
import doctor_role_soul as drs

WORKER_OPS = ("## Operational\n\nYou run as the **{name}** role on a worker GPU "
              "node of the cluster, executing a single kanban task in your own "
              "git worktree.\n")
BASE_MD = "## Shared value one\n\nbody\n"


def _soul(role_header):
    return (f"# base\n\n## Shared value one\n\nbody\n\n## Role: {role_header}\n\n"
            f"identity text\n\n{WORKER_OPS.format(name=role_header)}")


def _fixture_world(tmp_path, *, soul_for, spec_names):
    """Build tmp profiles/ + roles/ + base-identity.md under tmp_path."""
    proot = tmp_path / "profiles"
    rroot = tmp_path / "roles"
    proot.mkdir(parents=True, exist_ok=True)
    rroot.mkdir(parents=True, exist_ok=True)
    for name, header in soul_for.items():
        d = proot / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SOUL.md").write_text(_soul(header), encoding="utf-8")
    for stem, spec_name in spec_names.items():
        (rroot / f"{stem}.yaml").write_text(
            yaml.safe_dump({"name": spec_name, "identity": "text.\n",
                            "routing_description": "r."}), encoding="utf-8")
    base = tmp_path / "base-identity.md"
    base.write_text(BASE_MD, encoding="utf-8")
    return str(proot), str(rroot), str(base)


def _scan(tmp_path, *, soul_for, spec_names):
    proot, rroot, base = _fixture_world(tmp_path, soul_for=soul_for,
                                        spec_names=spec_names)

    def run():
        drs._roles_dir()          # same resolution the bridge uses
        import role_soul_check as rsc
        return rsc.check(proot, roles_dir=rroot, base_path=base)
    return run


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["10.0.0.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    return home


FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["10.0.0.1"]}])


# ── the bridge ─────────────────────────────────────────────────────────────

def test_check_name_matches_the_check_module():
    """doctor's checklist is keyed by name; bridge and check must agree.

    Resolved through the bridge's own loader, not a bare ``import
    role_soul_check``: hscc-roles is not on sys.path until ``_roles_dir()``
    appends it, so a bare import here would fail in a clean process (and a
    module-level import would make the whole file depend on collection order).
    Going through _module() also pins that the bridge really does resolve the
    sibling, which is the thing the name-agreement rule rests on.
    """
    rsc = drs._module()
    assert rsc is not None, "hscc-roles sibling must be resolvable from the bridge"
    assert drs.check_name() == rsc.CHECK_NAME   # bridge follows the module
    assert drs.CHECK_NAME == rsc.CHECK_NAME     # fallback literal has not drifted


def test_roles_dir_resolves_the_sibling_in_this_layout():
    """The work-repo layout: hscc-roles sits beside hscc-bootstrap."""
    resolved = drs._roles_dir()
    assert os.path.isdir(resolved)
    assert os.path.basename(resolved) == "hscc-roles"
    assert os.path.isfile(os.path.join(resolved, "role_soul_check.py"))
    assert resolved in sys.path


def test_sibling_path_is_appended_so_bootstrap_theme_still_wins():
    """hscc-bootstrap has its own _theme.py and so does hscc-roles. Appending
    keeps bootstrap's copy authoritative — a prepend would silently swap it."""
    before = [p for p in sys.path]
    drs._roles_dir()
    roles = drs._roles_dir()
    assert sys.path.count(roles) == 1, "must not duplicate on repeat calls"
    theme_boot = os.path.abspath(os.path.join(os.path.dirname(drs.__file__),
                                              "_theme.py"))
    theme_roles = os.path.abspath(os.path.join(roles, "_theme.py"))
    assert sys.path.index(os.path.dirname(theme_boot)) \
        < sys.path.index(roles), "roles dir must come AFTER bootstrap's dir"
    assert os.path.isfile(theme_roles)  # the collision this rule is about


def test_bridge_check_shape_matches_the_doctor_contract():
    """run_doctor pulls exactly five keys out of the dict; a missing one is a
    KeyError at preflight time, so pin the shape here."""
    res = drs.check()
    for key in ("name", "ok", "detail", "fix", "fatal"):
        assert key in res


def test_bridge_reports_error_when_sibling_unimportable(monkeypatch):
    """Honest signal: no hscc-roles next to us must be a red ERROR naming the
    missing plugin, never an ok and never a silent skip."""
    monkeypatch.setattr(drs, "_module", lambda: None)
    res = drs.check()
    assert res["ok"] is False
    assert res["fatal"] is False
    assert "hscc-roles" in res["detail"]
    assert "cannot verify" in res["detail"]


def test_bridge_delegates_to_the_real_check(tmp_path):
    """The bridge must not reimplement (or shadow) the verdict — a fixture world
    with a mismatched header must come back red through the bridge."""
    proot, rroot, base = _fixture_world(
        tmp_path, soul_for={"pontaje-engineer": "bc-engineer"},
        spec_names={"pontaje-engineer": "pontaje-engineer"})
    res = drs.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    assert "pontaje-engineer" in res["detail"]


def test_bridge_cli_json_and_exit_code(tmp_path, capsys):
    proot, rroot, base = _fixture_world(
        tmp_path, soul_for={"a-engineer": "a-engineer"},
        spec_names={"a-engineer": "a-engineer"})
    rc = drs.main(["--profiles-dir", proot, "--roles-dir", rroot,
                   "--base-identity", base, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_bridge_cli_exits_nonzero_on_a_mismatch(tmp_path, capsys):
    proot, rroot, base = _fixture_world(
        tmp_path, soul_for={"a-engineer": "wrong-one"},
        spec_names={"a-engineer": "a-engineer"})
    rc = drs.main(["--profiles-dir", proot, "--roles-dir", rroot,
                   "--base-identity", base, "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ── the seam inside run_doctor ─────────────────────────────────────────────

def test_run_doctor_reports_the_check_when_the_scan_is_injected(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        _role_soul_scan=_scan(tmp_path, soul_for={"a-engineer": "a-engineer"},
                              spec_names={"a-engineer": "a-engineer"}),
        **_fleet_health())
    names = {c["name"]: c for c in res["checks"]}
    assert drs.CHECK_NAME in names
    assert names[drs.CHECK_NAME]["ok"] is True
    assert names[drs.CHECK_NAME]["fatal"] is False


def test_run_doctor_surfaces_a_real_mismatch(tmp_path, monkeypatch):
    """The card's rendering requirement, end to end through doctor: a mismatched
    header must appear in the check's detail, naming actual and expected."""
    home = _home(tmp_path, monkeypatch)
    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        _role_soul_scan=_scan(tmp_path, soul_for={"pontaje-engineer": "bc-engineer"},
                              spec_names={"pontaje-engineer": "pontaje-engineer"}),
        **_fleet_health())
    names = {c["name"]: c for c in res["checks"]}
    c = names[drs.CHECK_NAME]
    assert c["ok"] is False
    assert "pontaje-engineer" in c["detail"]
    assert "bc-engineer" in c["detail"]
    # A WARNING must never hard-stop preflight.
    assert res["ok"] is True


def test_run_doctor_inside_pytest_refuses_an_uninjected_scan(tmp_path, monkeypatch):
    """THE fence. fleet_checks=True with no _role_soul_scan must not read the
    operator's live profiles — the check comes back an ERROR naming the seam."""
    home = _home(tmp_path, monkeypatch)
    # Point rolelib at a dir that EXISTS and has a SOUL: if the fence leaked, the
    # check would find that profile and the assertion below would fail loudly.
    import rolelib
    live = tmp_path / "leak-probe" / "profiles"
    (live / "canary-engineer").mkdir(parents=True)
    (live / "canary-engineer" / "SOUL.md").write_text(
        _soul("canary-engineer"), encoding="utf-8")
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(live))

    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent", **_fleet_health())
    names = {c["name"]: c for c in res["checks"]}
    c = names[drs.CHECK_NAME]
    assert c["ok"] is False
    assert "refused" in c["detail"] and "pytest" in c["detail"]
    assert "canary-engineer" not in c["detail"]


def test_run_doctor_fleet_off_marks_the_check_skipped(tmp_path, monkeypatch):
    """Absence would read as an all-clear, so a skipped leg prints skipped."""
    home = _home(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER)
    names = {c["name"]: c for c in res["checks"]}
    assert names[drs.CHECK_NAME]["detail"] == "skipped (fleet checks off)"


def test_run_doctor_json_carries_the_check(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        _role_soul_scan=_scan(tmp_path, soul_for={"a-engineer": "wrong-a"},
                              spec_names={"a-engineer": "a-engineer"}),
        **_fleet_health())
    payload = json.loads(json.dumps(res))   # JSON-serialisable
    row = [c for c in payload["checks"] if c["name"] == drs.CHECK_NAME][0]
    assert set(row) == {"name", "ok", "detail", "fix", "fatal"}
    assert "\n" in row["detail"]


def test_doctor_json_output_carries_the_check(tmp_path, monkeypatch, capsys):
    """The CLI surface too: `doctor --json` must expose it to scripts."""
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "_run_cluster_list", lambda: FAKE_CLUSTER)
    monkeypatch.setattr(doctor, "_gateway_running",
                        lambda: doctor.Check("gateway", True, detail="running",
                                             fatal=False))
    monkeypatch.setattr(doctor, "_role_soul_scan_default", None, raising=False)
    import shutil as _sh
    monkeypatch.setattr(_sh, "which",
                        lambda n: "/usr/local/bin/sparkrun" if n == "sparkrun" else None)
    # The suite auto-skips fleet checks, so the skipped line is what --json
    # carries here; the injected-seam path is covered above.
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = doctor.main(["--json", "--home", str(home)])
    payload = json.loads(buf.getvalue())
    assert rc == 0
    assert any(c["name"] == drs.CHECK_NAME for c in payload["checks"])
