"""provision-check tests: per-profile EFFECTIVE memory/compaction verification.

The ROADMAP ``profile-provisioning`` item 3 (make provisioning verifiable)
adds the ``provision-check`` command. A profile's config does NOT inherit the
flat ``~/.hermes/config.yaml`` — Hermes resolves effective settings only via
``HERMES_HOME=<profile-dir> load_config()``. So a value can read correct on
disk while the profile SILENTLY RUNS the default (e.g. the memory block
falling back to 2200 chars). These tests are hermetic: they build temp profile
dirs with known config.yaml, inject a KNOWN effective dict at the
``hscc._load_effective_config`` seam (the point that spawns Hermes' real
loader), and assert the command's output reports those effective values and
flags a discrepancy whenever the injected effective value differs from the raw
yaml. They never touch live /Users/desac/.hermes profiles.

Invariants pinned (mirroring test_hscc_theme.py):
- --json stdout stays PURE JSON: raw print(json.dumps), never ANSI, never
  themed, byte-comparable to a manual json.dumps of the same payload.
- On a non-TTY stdout the human (table) view contains zero escape bytes.
"""

import contextlib
import io
import json
import os

import pytest

import hscc
import rolelib

_ESC = "\x1b"


def _setup(tmp_path, monkeypatch, profiles):
    """Point hscc-roles at an isolated tmp HERMES_HOME with the given profile
    names as real profile dirs (so config.yaml / missing-dir cases both exist).
    Returns the profiles dir path."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    for name in profiles:
        (profiles_dir / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(hscc, "_base_identity", lambda: "BASE\n")
    return profiles_dir


def _json_out(fn):
    """Capture stdout of ``fn`` and return (ret, parsed_json_or_raw_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = fn()
    return ret, buf.getvalue()


def _known_effective(provider="mem0", char_limit=4000, threshold=60000,
                     base_url="http://100.64.0.1:8000/v1"):
    return {
        "memory_provider": provider,
        "memory_char_limit": char_limit,
        "threshold_tokens": threshold,
        "summarization_base_url": base_url,
    }


# --------------------------------------------------------------------------- #
# Populated profile: effective == raw -> reported, no discrepancy, exit 0.
# --------------------------------------------------------------------------- #

def test_populated_profile_reports_effective_no_discrepancy(tmp_path, monkeypatch):
    """A fully-pinned profile reports its EFFECTIVE values (from the injected
    load_config result) and flags no discrepancy when they match the raw yaml
    on disk. Exit 0 (nothing wrong)."""
    _setup(tmp_path, monkeypatch, ["acme-orch"])
    cfg = tmp_path / "profiles" / "acme-orch" / "config.yaml"
    cfg.write_text(
        "memory:\n  provider: mem0\n  memory_char_limit: 4000\n"
        "compression:\n  threshold_tokens: 60000\n"
        "auxiliary:\n  compression:\n    base_url: http://100.64.0.1:8000/v1\n"
    )
    monkeypatch.setattr(hscc, "_load_effective_config",
                        lambda profile_dir: _known_effective())
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["acme-orch"])

    ret, stdout = _json_out(lambda: hscc.cmd_provision_check(["--json"]))
    assert ret == 0
    assert _ESC not in stdout
    report = json.loads(stdout)

    acme = [p for p in report if p["profile"] == "acme-orch"][0]
    assert acme["discrepancy"] is False
    fields = acme["fields"]
    assert fields["memory_provider"]["effective"] == "mem0"
    assert fields["memory_provider"]["raw"] == "mem0"
    assert fields["memory_provider"]["discrepancy"] is False
    assert fields["memory_char_limit"]["effective"] == 4000
    assert fields["threshold_tokens"]["effective"] == 60000
    assert fields["summarization_base_url"]["effective"] == "http://100.64.0.1:8000/v1"
    assert fields["summarization_base_url"]["discrepancy"] is False

    # Byte-identity: the whole report equals a manual re-dump of the same data.
    expected = [{
        "profile": "acme-orch",
        "config": str(cfg),
        "fields": {
            "memory_provider": {"raw": "mem0", "effective": "mem0",
                                "discrepancy": False},
            "memory_char_limit": {"raw": 4000, "effective": 4000,
                                  "discrepancy": False},
            "threshold_tokens": {"raw": 60000, "effective": 60000,
                                 "discrepancy": False},
            "summarization_base_url": {"raw": "http://100.64.0.1:8000/v1",
                                       "effective": "http://100.64.0.1:8000/v1",
                                       "discrepancy": False},
        },
        "discrepancy": False,
    }]
    assert report == expected
    assert stdout == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Discrepancy: effective differs from the raw yaml -> flagged, exit 1.
# --------------------------------------------------------------------------- #

def test_discrepancy_flagged_when_effective_differs_from_raw(tmp_path, monkeypatch):
    """The silently-defaulted case: the raw yaml pins memory.provider and
    memory_char_limit, but the injected EFFECTIVE values are the Hermes
    defaults (empty provider, 2200 chars) — so a value read correct on disk
    while the profile would run the default. Both discrepancy SITES are flagged
    and the command exits non-zero (provisioning is not verified)."""
    _setup(tmp_path, monkeypatch, ["acme-orch"])
    cfg = tmp_path / "profiles" / "acme-orch" / "config.yaml"
    cfg.write_text(
        "memory:\n  provider: mem0\n  memory_char_limit: 4000\n"
        "compression:\n  threshold_tokens: 60000\n"
    )
    # The effective loader resolves differently from the on-disk yaml for the
    # two memory fields — exactly the silent-default trap.
    monkeypatch.setattr(hscc, "_load_effective_config",
                        lambda profile_dir: _known_effective(provider="", char_limit=2200))
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["acme-orch"])

    ret, stdout = _json_out(lambda: hscc.cmd_provision_check(["--json"]))
    assert ret == 1, "a discrepancy must produce a non-zero exit"
    acme = [p for p in json.loads(stdout) if p["profile"] == "acme-orch"][0]
    assert acme["discrepancy"] is True
    mp = acme["fields"]["memory_provider"]
    assert mp["raw"] == "mem0" and mp["effective"] == ""
    assert mp["discrepancy"] is True
    cl = acme["fields"]["memory_char_limit"]
    assert cl["raw"] == 4000 and cl["effective"] == 2200
    assert cl["discrepancy"] is True
    # Compact threshold matched -> no discrepancy there.
    assert acme["fields"]["threshold_tokens"]["discrepancy"] is False


# --------------------------------------------------------------------------- #
# Missing config: profile dir exists but no config.yaml -> reported, not skipped.
# --------------------------------------------------------------------------- #

def test_missing_config_reported_explicitly(tmp_path, monkeypatch):
    """A profile dir without config.yaml still resolves effective values (all
    Hermes defaults via the injected loader) and is REPORTED with raw=unset per
    field, not skipped. Not a discrepancy: there is no raw value to have failed
    to take effect — the profile simply runs the default."""
    _setup(tmp_path, monkeypatch, ["naked-orch"])
    monkeypatch.setattr(hscc, "_load_effective_config",
                        lambda profile_dir: _known_effective(provider="", char_limit=2200))
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["naked-orch"])

    ret, stdout = _json_out(lambda: hscc.cmd_provision_check(["--json"]))
    assert ret == 0, "no discrepancy, no error -> clean exit"
    entry = json.loads(stdout)[0]
    assert entry["profile"] == "naked-orch"
    assert entry["config"] is None  # no config.yaml on disk
    assert "error" not in entry
    assert entry["discrepancy"] is False
    # Effective defaults still surfaced so a silently-defaulted profile shows.
    assert entry["fields"]["memory_char_limit"]["effective"] == 2200
    assert entry["fields"]["memory_provider"]["effective"] == ""
    # No raw value on disk.
    assert entry["fields"]["memory_char_limit"]["raw"] is None


# --------------------------------------------------------------------------- #
# Missing profile dir -> reported explicitly.
# --------------------------------------------------------------------------- #

def test_missing_profile_dir_reported(tmp_path, monkeypatch):
    """A profile in the provisioning set with NO profile dir at all is reported
    explicitly (error), not silently dropped — and exits non-zero."""
    _setup(tmp_path, monkeypatch, [])
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["ghost-orch"])

    ret, stdout = _json_out(lambda: hscc.cmd_provision_check(["--json"]))
    assert ret == 1
    entry = json.loads(stdout)[0]
    assert entry["profile"] == "ghost-orch"
    assert entry["error"] == "missing profile dir"
    assert "fields" not in entry


def test_unresolvable_effective_config_error(tmp_path, monkeypatch):
    """If Hermes' loader cannot resolve a profile (injected to raise), the
    profile is reported explicitly with the error, never skipped, exit 1."""
    _setup(tmp_path, monkeypatch, ["acme-orch"])
    (tmp_path / "profiles" / "acme-orch" / "config.yaml").write_text(
        "memory:\n  provider: mem0\n")

    def _boom(profile_dir):
        raise hscc.EffectiveConfigError("Hermes loader failed: boom")

    monkeypatch.setattr(hscc, "_load_effective_config", _boom)
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["acme-orch"])

    ret, stdout = _json_out(lambda: hscc.cmd_provision_check(["--json"]))
    assert ret == 1
    entry = json.loads(stdout)[0]
    assert entry["profile"] == "acme-orch"
    assert "Hermes loader failed: boom" in entry["error"]


# --------------------------------------------------------------------------- #
# Human view: plain (no ANSI) on a pipe, with per-profile tables.
# --------------------------------------------------------------------------- #

def test_human_view_is_plain_and_shows_discrepancy(tmp_path, monkeypatch):
    """The non-json human view renders a per-profile table, PLAIN (no ANSI)
    when stdout is redirected, and labels a discrepant field DISCREPANCY."""
    _setup(tmp_path, monkeypatch, ["acme-orch"])
    (tmp_path / "profiles" / "acme-orch" / "config.yaml").write_text(
        "memory:\n  provider: mem0\n  memory_char_limit: 4000\n")
    monkeypatch.setattr(hscc, "_load_effective_config",
                        lambda profile_dir: _known_effective(provider="", char_limit=2200))
    monkeypatch.setattr(hscc, "_provision_profile_names",
                        lambda registry=None: ["acme-orch"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = hscc.cmd_provision_check([])
    out = buf.getvalue()
    assert ret == 1
    assert _ESC not in out
    assert "acme-orch" in out
    assert "memory.provider" in out
    assert "DISCREPANCY" in out
    assert "2200" in out  # the effective default is surfaced
