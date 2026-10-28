"""t_1c545ebb — the multiplex profile roster must be Hermes' roster, not "all dirs".

`hscc verify`'s multiplex check and the daemon's gateway stream each built
their own "which profiles must be served" list by scanning
``~/.hermes/profiles``. Two on-disk leftovers made that permanent-red:

* ``profiles/.deleted/`` — Hermes' tombstone directory
  (``hermes_constants._DELETED_PROFILES_DIR``), holding one 8-byte file per
  deleted profile. ``verify`` scanned it as a directory and so reported a
  profile literally named ``.deleted`` as unserved;
* ``profiles/ecofire-bc/`` — a stub holding only ``workspaces/``, with none of
  Hermes' profile identity markers, counted as an expected-served profile.

The gateway builds ``served_profiles`` from Hermes' own enumeration
(``hermes_cli.profiles._iter_named_profile_dirs``), which requires an identity
marker and skips dot-dirs, so it can never serve either of them. Demanding
them was unachievable by construction.

Every case below runs against BOTH roster branches — Hermes' predicates and
the self-contained mirror used when ``hermes_constants`` is not importable
(the daemon deploys under ``~/.hermes/plugins`` where it may not be) — so a
stale mirror cannot drift into a false-fail unnoticed.
"""

import json
import os
import re

import pytest

from hscc_daemon import health
from hscc_daemon import verify

from hscc_daemon.tests.profile_roster_sandbox import (roster_fallback,
                                                      roster_with)

# The real Hermes grammar/predicates, replicated here so the injected branch is
# pinned even on an interpreter that lacks hermes_constants.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json",
           "state.db")


def _has_identity(home):
    return any(os.path.isfile(os.path.join(home, m)) or
               os.path.islink(os.path.join(home, m)) for m in MARKERS)


def _is_deleted(home):
    return os.path.exists(
        os.path.join(os.path.dirname(home), ".deleted", os.path.basename(home)))


def _is_standalone(home):
    """Mirror of ``hermes_cli.profiles.profile_is_standalone`` for the
    injected branch: only ``gateway.standalone`` counts, no top-level alias."""
    cfg = os.path.join(str(home), "config.yaml")
    if not os.path.isfile(cfg):
        return False
    try:
        import yaml
        data = yaml.safe_load(open(cfg, encoding="utf-8")) or {}
    except Exception:
        return False
    gateway = data.get("gateway") if isinstance(data, dict) else None
    if not isinstance(gateway, dict):
        return False
    value = gateway.get("standalone")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _both_branches(profiles_dir):
    """The roster from both the Hermes-predicate and the fallback branch.

    A mismatch is a real defect, not a test artefact: the deployed daemon may
    run under either branch (``hermes_constants`` is not guaranteed on its
    ``sys.path``), so the two must never disagree about who is expected to be
    served.
    """
    hermes = roster_with(str(profiles_dir), id_re=ID_RE,
                         has_identity=_has_identity, is_deleted=_is_deleted,
                         is_standalone=_is_standalone)
    mirror = roster_fallback(str(profiles_dir))
    assert hermes == mirror, (
        f"branches disagree: hermes={hermes} mirror={mirror}")
    return hermes


def _profile(profiles_dir, name, *, marker="config.yaml"):
    """Create ``profiles/<name>`` carrying one identity marker (a real profile)."""
    home = profiles_dir / name
    home.mkdir(parents=True, exist_ok=True)
    (home / marker).write_text("toolsets:\n  - hermes-cli\n")
    return home


def _stub(profiles_dir, name):
    """Create ``profiles/<name>`` with NO identity marker (Hermes: not a profile)."""
    home = profiles_dir / name
    (home / "workspaces" / "t_deadbeef" / "scripts").mkdir(parents=True)
    return home


class TestRosterGate:
    def test_dot_directory_is_never_a_profile_id(self, tmp_path):
        """(1) A dot-directory under profiles/ is never a profile id.

        ``profiles/.deleted/`` is Hermes' tombstone dir; its contents are 8-byte
        tombstone files, one per deleted profile. Scanned naively it becomes a
        profile named ``.deleted`` that no gateway will ever serve.
        """
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "probe-20261026-orch").write_text("deleted\n")

        assert _both_branches(profiles) == ["worker"]

    def test_marker_less_stub_dir_is_not_a_profile(self, tmp_path):
        """(2) A directory with no profile config is not counted as a profile.

        ``profiles/ecofire-bc/`` carried only ``workspaces/`` — a leftover from
        a task workspace, not a profile Hermes created or serves.
        """
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        _stub(profiles, "ecofire-bc")

        assert _both_branches(profiles) == ["worker"]

    def test_any_identity_marker_qualifies(self, tmp_path):
        """The gate is Hermes' marker SET, not `config.yaml` alone.

        A profile that has ``.env``/``state.db``/``SOUL.md`` but no
        ``config.yaml`` is real and IS served — counting only config.yaml would
        drop genuine profiles and silently weaken coverage.
        """
        profiles = tmp_path / "profiles"
        for i, marker in enumerate(MARKERS):
            _profile(profiles, f"p{i}", marker=marker)

        assert len(_both_branches(profiles)) == len(MARKERS)

    def test_default_subdir_is_not_a_named_profile(self, tmp_path):
        """Hermes excludes ``profiles/default`` from the NAMED roster (it is the
        root home, always served when multiplex is on), so it is never a gap."""
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        (profiles / "default").mkdir(parents=True)

        assert _both_branches(profiles) == ["worker"]

    def test_name_outside_profile_id_grammar_is_skipped(self, tmp_path):
        """Hermes gates named dirs on ``PROFILE_ID_RE``; a stray directory
        (``Backups``, ``a b``) is not a profile id and can never be served."""
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        for junk in ("Backups", "has space", "_leading", "x" * 80):
            (profiles / junk).mkdir(parents=True)
            (profiles / junk / "config.yaml").write_text("a: 1\n")

        assert _both_branches(profiles) == ["worker"]

    def test_tombstoned_profile_is_skipped(self, tmp_path):
        """A deleted profile whose directory is still on disk (marker in
        ``profiles/.deleted/<name>``) is not expected to be served."""
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        _profile(profiles, "gone")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "gone").write_text("deleted\n")

        assert _both_branches(profiles) == ["worker"]

    def test_parked_profile_is_skipped(self, tmp_path):
        """``hermes -p X gateway stop`` parks a profile; the host deliberately
        does not serve it, so demanding it would be a false-fail."""
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        parked = _profile(profiles, "resting")
        (parked / "gateway.parked").write_text("parked\n")

        assert _both_branches(profiles) == ["worker"]

    def test_standalone_profile_is_not_required(self, tmp_path):
        """``gateway.standalone: true`` opts a profile out of the host
        multiplexer (Hermes' ``profiles_to_serve`` skips it), so the host must
        not be red for it. Only the ``gateway:`` key counts — a top-level
        ``standalone`` is not an opt-out."""
        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        solo = _profile(profiles, "solo")
        (solo / "config.yaml").write_text("gateway:\n  standalone: true\n")
        _profile(profiles, "notreal")  # top-level `standalone:` is NOT an opt-out
        (profiles / "notreal" / "config.yaml").write_text("standalone: true\n")

        assert _both_branches(profiles) == ["notreal", "worker"]

    def test_mirror_matches_real_hermes_predicates(self, tmp_path):
        """Drift guard: when Hermes is importable, the fallback mirror must
        agree with Hermes' OWN predicates on one identical tree.

        The mirror exists because the deployed daemon may lack
        ``hermes_constants`` on its ``sys.path``; if it drifts from the real
        gate, hosts running the mirror get a different (and wrong) verdict.
        """
        hermes_constants = pytest.importorskip("hermes_constants")
        profile_is_standalone = pytest.importorskip(
            "hermes_cli.profiles").profile_is_standalone

        profiles = tmp_path / "profiles"
        _profile(profiles, "worker")
        _profile(profiles, "solo")
        (profiles / "solo" / "config.yaml").write_text(
            "gateway:\n  standalone: true\n")
        _stub(profiles, "ecofire-bc")                       # no marker
        (profiles / ".deleted").mkdir()                     # Hermes tombstone dir
        (profiles / ".deleted" / "gone").write_text("deleted\n")
        _profile(profiles, "gone")                          # tombstoned
        _profile(profiles, "resting")
        (profiles / "resting" / "gateway.parked").write_text("parked\n")
        (profiles / "Backups").mkdir()                      # bad id grammar
        (profiles / "Backups" / "config.yaml").write_text("a: 1\n")

        real = roster_with(
            str(profiles), id_re=hermes_constants.PROFILE_ID_RE,
            has_identity=hermes_constants.named_profile_has_identity,
            is_deleted=hermes_constants.named_profile_is_deleted,
            is_standalone=profile_is_standalone)
        mirror = roster_fallback(str(profiles))
        assert real == mirror == ["worker"]

    def test_missing_profiles_dir_is_empty_roster(self, tmp_path):
        assert roster_fallback(str(tmp_path / "nope")) == []

    def test_live_host_shape_is_clean(self, tmp_path):
        """Reproduce this host's exact leftover shape: tombstone dir + stub +
        real profiles, all served → the roster is exactly the real profiles."""
        profiles = tmp_path / "profiles"
        for name in ("backend-engineer", "hscc-orch", "worker"):
            _profile(profiles, name)
        _stub(profiles, "ecofire-bc")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "probe-20261026-f-orch").write_text("deleted\n")
        (profiles / ".deleted" / "probe-20261026-orch").write_text("deleted\n")

        assert _both_branches(profiles) == ["backend-engineer", "hscc-orch",
                                            "worker"]


# ── verify.check_multiplex ────────────────────────────────────────────────

class TestVerifyCheckMultiplex:
    def _fixture(self, tmp_path, served, *, extra_dirs=None):
        config = tmp_path / "config.yaml"
        config.write_text("multiplex_profiles: true\n")
        gw = tmp_path / "gateway_state.json"
        json.dump({"served_profiles": served}, open(gw, "w"))
        profiles = tmp_path / "profiles"
        for name in served:
            _profile(profiles, name)
        for name in (extra_dirs or ()):
            _stub(profiles, name)
        (profiles / ".deleted").mkdir(parents=True, exist_ok=True)
        (profiles / ".deleted" / "probe-20261026-orch").write_text("deleted\n")
        return {"config": str(config), "gateway_state": str(gw),
                "profiles_dir": str(profiles)}

    def test_leftovers_do_not_fail_the_check(self, tmp_path):
        """The card's AC: PASS with ``profiles/.deleted/`` and a profile-less
        stub present."""
        kwargs = self._fixture(tmp_path, ["backend-engineer", "worker"],
                               extra_dirs=["ecofire-bc"])
        result = verify.check_multiplex(**kwargs)
        assert result["ok"] is True, result
        assert ".deleted" not in result["detail"]
        assert "ecofire-bc" not in result["detail"]

    def test_genuinely_missing_profile_still_fails(self, tmp_path):
        """The fix must not weaken coverage: a real profile (carrying an
        identity marker) that the gateway does not serve is still a FAIL."""
        kwargs = self._fixture(tmp_path, ["backend-engineer"],
                               extra_dirs=["ecofire-bc"])
        _profile(tmp_path / "profiles", "researcher")  # real, unserved

        result = verify.check_multiplex(**kwargs)
        assert result["ok"] is False
        assert "researcher" in result["detail"]
        assert ".deleted" not in result["detail"]
        assert "ecofire-bc" not in result["detail"]

    def test_parked_profile_not_required(self, tmp_path):
        kwargs = self._fixture(tmp_path, ["backend-engineer"])
        parked = _profile(tmp_path / "profiles", "resting")
        (parked / "gateway.parked").write_text("parked\n")

        assert verify.check_multiplex(**kwargs)["ok"] is True


# ── health._check_multiplex_profiles (the daemon stream) ──────────────────

class TestDaemonStream:
    def _fixture(self, tmp_path, monkeypatch, served):
        hermes = tmp_path / "hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text("multiplex_profiles: true\n")
        json.dump({"served_profiles": served},
                  open(hermes / "gateway_state.json", "w"))
        profiles = hermes / "profiles"
        for name in served:
            _profile(profiles, name)
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML",
                            str(hermes / "config.yaml"))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE",
                            str(hermes / "gateway_state.json"))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(profiles))
        return profiles

    def test_leftovers_do_not_fail_the_stream(self, tmp_path, monkeypatch):
        profiles = self._fixture(tmp_path, monkeypatch,
                                 ["backend-engineer", "worker"])
        _stub(profiles, "ecofire-bc")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "probe-20261026-orch").write_text("deleted\n")

        result = health._check_multiplex_profiles()
        assert result["ok"] is True, result

    def test_genuinely_missing_profile_still_fails(self, tmp_path, monkeypatch):
        profiles = self._fixture(tmp_path, monkeypatch, ["backend-engineer"])
        _profile(profiles, "researcher")

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "researcher" in result["message"]


# ── the two surfaces must agree (the card's AC-3) ─────────────────────────

class TestSurfacesAgree:
    def test_verify_and_daemon_stream_agree(self, tmp_path, monkeypatch):
        """``hscc verify`` must not contradict the stream feeding gateway.json.

        Pre-fix they disagreed by construction: the health scanner skipped
        dot-dirs and the verify scanner did not, so gateway.json said "1
        profile(s) not served" while verify named two.
        """
        served = ["backend-engineer", "worker"]
        hermes = tmp_path / "hermes"
        (hermes / "profiles").mkdir(parents=True)
        (hermes / "config.yaml").write_text("multiplex_profiles: true\n")
        json.dump({"served_profiles": served},
                  open(hermes / "gateway_state.json", "w"))
        profiles = hermes / "profiles"
        for name in served:
            _profile(profiles, name)
        _stub(profiles, "ecofire-bc")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "probe").write_text("deleted\n")
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML",
                            str(hermes / "config.yaml"))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE",
                            str(hermes / "gateway_state.json"))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(profiles))

        stream = health._check_multiplex_profiles()
        check = verify.check_multiplex(
            config=str(hermes / "config.yaml"),
            gateway_state=str(hermes / "gateway_state.json"),
            profiles_dir=str(profiles))
        assert stream["ok"] is True and check["ok"] is True

        # A real gap makes BOTH red, naming the same profile.
        _profile(profiles, "researcher")
        stream = health._check_multiplex_profiles()
        check = verify.check_multiplex(
            config=str(hermes / "config.yaml"),
            gateway_state=str(hermes / "gateway_state.json"),
            profiles_dir=str(profiles))
        assert stream["ok"] is False and check["ok"] is False
        assert "researcher" in stream["message"]
        assert "researcher" in check["detail"]

    def test_check_gateway_no_longer_holds_gateway_hostage(self, tmp_path,
                                                           monkeypatch):
        """The knock-on that made watchdog.json read "Degraded — gateway not
        reachable" while the gateway job AND vLLM were both healthy."""
        profiles = self._fixture = None
        served = ["backend-engineer", "worker"]
        hermes = tmp_path / "hermes"
        (hermes / "profiles").mkdir(parents=True)
        (hermes / "config.yaml").write_text("multiplex_profiles: true\n")
        json.dump({"served_profiles": served},
                  open(hermes / "gateway_state.json", "w"))
        profiles = hermes / "profiles"
        for name in served:
            _profile(profiles, name)
        _stub(profiles, "ecofire-bc")
        (profiles / ".deleted").mkdir(parents=True)
        (profiles / ".deleted" / "probe").write_text("deleted\n")
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML",
                            str(hermes / "config.yaml"))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE",
                            str(hermes / "gateway_state.json"))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(profiles))

        monkeypatch.setattr(health, "_gateway_job_alive", lambda: True)
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": True})
        monkeypatch.setattr(health, "log", lambda *a, **k: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        health.check_gateway()
        written = json.load(open(state_dir / "gateway.json"))
        assert written["multiplex_ok"] is True
        assert written["ok"] is True
