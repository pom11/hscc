"""Tests for preserve_autodown — bootstrap must never clobber operator state.

~/.hscc/autodown.json is OPERATOR state: it decides whether the daemon may tear
down the whole fleet. Bootstrap (and this helper) must:
  - preserve ``enabled`` / ``idle_minutes`` EXACTLY when the file exists,
  - seed a DISABLED default only on a fresh install (opt-in per C5),
  - add newly-introduced schema keys without flipping existing values,
  - and never touch the REAL ``~/.hscc`` — tests run against a tmp HOME.
"""
import json
import os
import subprocess
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import preserve_autodown as PA


def _path(tmp_path):
    return str(tmp_path / "autodown.json")


def _read(p):
    with open(p) as f:
        return json.load(f)


# ── Existing operator config is PRESERVED ─────────────────────────────


def test_existing_enabled_true_60_unchanged(tmp_path):
    """The bootstrap step must not alter an existing enabled=true/60 config."""
    target = _path(tmp_path)
    with open(target, "w") as f:
        json.dump({"enabled": True, "idle_minutes": 60}, f)

    result = PA.ensure_autodown(autodown_path=target)

    assert result["action"] == "preserved"
    assert result["enabled"] is True
    assert result["idle_minutes"] == 60
    assert result["ok"] is True

    data = _read(target)
    assert data["enabled"] is True
    assert data["idle_minutes"] == 60


def test_existing_enabled_false_stays_false(tmp_path):
    """The exact live failure: a deliberately-disarmed autodown must stay off."""
    target = _path(tmp_path)
    with open(target, "w") as f:
        json.dump({"enabled": False, "idle_minutes": 10}, f)

    result = PA.ensure_autodown(autodown_path=target)

    assert result["action"] == "preserved"
    assert result["enabled"] is False
    assert result["idle_minutes"] == 10

    data = _read(target)
    assert data["enabled"] is False
    assert data["idle_minutes"] == 10


def test_existing_other_values_preserved_verbatim(tmp_path):
    """Every existing value survives untouched — only missing keys are added."""
    target = _path(tmp_path)
    existing = {
        "enabled": True,
        "idle_minutes": 45,
        "state": "down",
        "down_since": "2026-08-25T07:59:01Z",
        "reason": "operator note",
    }
    with open(target, "w") as f:
        json.dump(existing, f)

    PA.ensure_autodown(autodown_path=target)

    data = _read(target)
    for k, v in existing.items():
        assert data[k] == v, f"{k} changed: {data[k]} != {v}"


# ── Absent file is seeded DISABLED (opt-in) ───────────────────────────


def test_absent_file_seeds_disabled(tmp_path):
    """A fresh install (no autodown.json) must seed enabled=false (C5 opt-in)."""
    target = _path(tmp_path)
    assert not os.path.exists(target)

    result = PA.ensure_autodown(autodown_path=target)

    assert result["action"] == "seeded"
    assert result["enabled"] is False
    assert result["idle_minutes"] == 10

    data = _read(target)
    assert data["enabled"] is False
    assert data["idle_minutes"] == 10
    # Full §7 schema is present on a fresh seed.
    for key in PA.DEFAULT_CONFIG:
        assert key in data, f"fresh seed missing key {key}"


# ── Forward compatibility: add new keys, keep existing values ─────────


def test_missing_new_key_added_existing_untouched(tmp_path):
    """A config missing a newly-introduced key gets it added at default, with
    the existing operator values still preserved."""
    target = _path(tmp_path)
    with open(target, "w") as f:
        json.dump({"enabled": True, "idle_minutes": 60}, f)  # old §7 schema

    result = PA.ensure_autodown(autodown_path=target)

    assert result["action"] == "preserved"
    assert result["enabled"] is True
    assert result["idle_minutes"] == 60  # operator's value untouched

    data = _read(target)
    assert data["enabled"] is True
    assert data["idle_minutes"] == 60
    # Newly-introduced keys present at default, added WITHOUT flipping others.
    for key, default in PA.DEFAULT_CONFIG.items():
        assert key in data
        if key not in ("enabled", "idle_minutes", "state"):
            assert data[key] == default, f"new key {key} not at default"


# ── Idempotency ───────────────────────────────────────────────────────


def test_second_run_preserves(tmp_path):
    """Re-running on an already-processed file is a no-op (still preserved)."""
    target = _path(tmp_path)
    with open(target, "w") as f:
        json.dump({"enabled": True, "idle_minutes": 60}, f)

    PA.ensure_autodown(autodown_path=target)
    r2 = PA.ensure_autodown(autodown_path=target)

    assert r2["action"] == "preserved"
    assert r2["enabled"] is True
    assert r2["idle_minutes"] == 60


# ── Corruption fails safe ─────────────────────────────────────────────


def test_corrupt_existing_rewritten_disabled_not_enabled(tmp_path):
    """A present-but-corrupt autodown.json must be rewritten to a DISABLED
    default — never left in a state that could re-arm autodown. Fails closed
    (§8 config corrupt): a corrupt config can never flip `enabled` ON."""
    target = _path(tmp_path)
    with open(target, "w") as f:
        f.write("{ this is not valid json")

    result = PA.ensure_autodown(autodown_path=target)

    assert result["enabled"] is False
    assert result["idle_minutes"] == 10
    data = _read(target)
    assert data["enabled"] is False
    assert data["idle_minutes"] == 10


# ── __main__ prints valid JSON ────────────────────────────────────────


def test_main_prints_json(capsys, tmp_path, monkeypatch):
    """The __main__ block prints valid JSON and writes under a TMP HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))

    import importlib
    mod = importlib.reload(PA)
    result = mod.ensure_autodown()
    assert result["action"] == "seeded"
    print(json.dumps(result))

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["action"] == "seeded"
    assert parsed["enabled"] is False
    # The file really landed under the tmp HOME, not the real ~/.hscc
    assert os.path.exists(os.path.join(str(tmp_path), ".hscc", "autodown.json"))


# ── LIVE-LEAK GUARD: the suite must never touch real ~/.hscc ──────────


def test_live_hscc_autodown_unchanged_by_suite(tmp_path):
    """REGRESSION GUARD: running this test module against a tmp HOME must leave
    the REAL ~/.hscc/autodown.json byte-identical. Mirrors the daemon's
    test_no_live_hscc_leak guard for this helper — a future edit that leaks a
    write into the operator's real config trips this loudly."""
    live = os.path.expanduser("~/.hscc/autodown.json")
    if not os.path.exists(live):
        # No live config on this host; the guard has nothing to protect and is
        # trivially satisfied. (On the real cluster it exists.)
        return

    before = open(live, "rb").read()

    # Re-run this module's own tests inside a sandboxed tmp HOME. If any test
    # path (e.g. __main__ without proper tmp isolation) were to reach the real
    # ~/.hscc, the sandbox run's own checks would write live — fail loudly.
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", __file__, "-p", "no:cacheprovider"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = open(live, "rb").read()
    assert before == after, (
        "TEST SUITE WROTE TO LIVE ~/.hscc/autodown.json! "
        f"before={before!r} after={after!r}"
    )
