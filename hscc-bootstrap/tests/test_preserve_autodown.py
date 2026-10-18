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
import builtins
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


# ── Unreadable existing file fails loud (never destroys operator state) ─


def test_read_oserror_fails_loud_without_touching_file(tmp_path, monkeypatch):
    """A read OSError (permission denied / transient I/O — an ENVIRONMENT
    fault, not proof the content is bad) must NOT be treated as a reason to
    seed a fresh config over the operator's existing autodown.json.

    Prior bug: an OSError on read collapsed into the corrupt/absent branch,
    which overwrote the operator's real `enabled: true` config with the
    DISABLED default and reported success (`action=seeded, ok=True`) — an
    environment fault rendered as a data fact, destroying an operator safety
    decision behind a green checkmark. Now it must fail loud (ok=False +
    error) and leave the file byte-identical.
    """
    target = _path(tmp_path)
    original = {"enabled": True, "idle_minutes": 60}
    with open(target, "w") as f:
        json.dump(original, f)

    real_open = builtins.open
    def read_only_permdenied(name, *a, **k):
        if name == target:
            raise PermissionError(13, "Permission denied")
        return real_open(name, *a, **k)
    monkeypatch.setattr("builtins.open", read_only_permdenied)

    result = PA.ensure_autodown(autodown_path=target)

    assert result["ok"] is False
    assert "error" in result and "Permission denied" in result["error"]
    assert result["action"] == "error"
    # Operator's file is untouched (the config is preserved verbatim).
    # Read back via the REAL open (the mock denies reads of `target`).
    with real_open(target) as f:
        assert json.load(f) == original


# ── __main__ prints valid JSON ────────────────────────────────────────


def test_main_prints_json(capsys, tmp_path, monkeypatch):
    """The __main__ block prints valid JSON and writes under a TMP HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # ensure_autodown prefers HSCC_DIR over HOME; if the invoking environment
    # exports it, patching HOME alone would seed the REAL state dir. Delenv so
    # HOME is authoritative here too (same leak class the live guard checks).
    monkeypatch.delenv("HSCC_DIR", raising=False)

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

# Env marker for the guard's own nested subprocess run (see below).
_NESTED_ENV = "HSCC_PRESERVE_AUTODOWN_NESTED"


def _operator_keys(path):
    """Return (enabled, idle_minutes) as stored at `path`, or None when the
    file is absent/unreadable. These are the OPERATOR-DECISION keys: the live
    daemon's autodown tick never rewrites them (only the `hscc autodown
    enable|disable` CLI does), so comparing them is race-free against the
    30s heartbeat."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return (data.get("enabled"), data.get("idle_minutes"))


def test_live_hscc_autodown_unchanged_by_suite(tmp_path):
    """REGRESSION GUARD: running this test module against a sandboxed HOME must
    never write the operator's autodown state. Mirrors the daemon's
    test_no_live_hscc_leak guard for this helper — a future edit that leaks a
    write into the real config trips this loudly.

    Why this does NOT byte-hash the live ~/.hscc/autodown.json (the original
    design flaked the merged-tree sweep on 2026-10-18): the live daemon's
    autodown loop (hscc_daemon/daemon_ops.py::run_autodown_loop, interval=30s)
    rewrites that file EVERY tick via record_activity()/save_config() —
    advancing last_activity_iso/wake_source — so a byte-identical assert
    races a tick and fails false-positive whenever one lands inside the read
    window (the daemon rewrites the file atomically via os.replace, so the
    hash is a moving target; measured live: md5 changed within 33 s). The
    daemon-side guard documents the same reasoning for ~/.hscc/state/*.json.

    Two race-free layers instead:
      1. Detective sandbox check — the module re-runs with HOME pointed at a
         sandbox holding a PLANTED ~/.hscc/autodown.json. The planted file is
         static (the live daemon only ever writes the operator's real home,
         never this tmp one), so any suite leak that resolves through HOME
         changes it → fails loudly, not flakily.
      2. Semantic live check — the real file's OPERATOR keys (enabled,
         idle_minutes) must be invariant across the nested run. A tick can
         move the volatile keys (last_activity_iso, wake_source, state …) but
         can never flip the operator's decision; only a suite that actually
         rewrote the operator's config could change them.

    The sandbox is only hermetic if HOME is authoritative: preserve_autodown
    prefers HSCC_DIR over HOME (preserve_autodown.ensure_autodown), so an
    exported HSCC_DIR would let the nested run leak straight past the fake
    HOME into the real state dir. It is stripped from the nested env.
    """
    if os.environ.get(_NESTED_ENV) == "1":
        # This IS the guard's nested re-run; the outer process performs both
        # checks. Without this marker the planted sandbox would keep the
        # nested guard alive and recursion would never terminate.
        return

    live = os.path.expanduser("~/.hscc/autodown.json")
    live_existed = os.path.exists(live)
    before_operator = _operator_keys(live)

    # Layer 1: planted sandbox home (deterministic, works on every host).
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True)
    seed = home / ".hscc" / "autodown.json"
    planted = json.dumps({
        "enabled": True, "idle_minutes": 60, "state": "up",
        "last_activity_iso": "2020-01-01T00:00:00+00:00",
    }, indent=2) + "\n"
    seed.write_text(planted)
    before_seed = seed.read_bytes()

    # Re-run this module's own tests inside the sandboxed tmp HOME. If any
    # test path (e.g. __main__ without proper tmp isolation) were to reach
    # the real ~/.hscc or write its HOME-resolved config, one of the two
    # checks below trips.
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("HSCC_DIR", None)   # keep HOME authoritative — see docstring
    env[_NESTED_ENV] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", __file__, "-p", "no:cacheprovider"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert seed.read_bytes() == before_seed, (
        "TEST SUITE WROTE TO ~/.hscc/autodown.json UNDER THE SANDBOX HOME — "
        "a code path resolved the config through something other than the "
        "sandboxed HOME (check ensure_autodown's HSCC_DIR/HSCC_DIR-relative "
        "resolution and any new expanduser call)."
    )

    # Layer 2: live operator keys must be untouched by the suite. A live tick
    # rewrites volatile keys (even the whole file, atomically) — comparing
    # only (enabled, idle_minutes) tolerates every tick and still catches the
    # one mutation that matters: the suite flipping the operator's decision.
    if live_existed:
        assert os.path.exists(live), (
            "TEST SUITE DELETED the live ~/.hscc/autodown.json — operator "
            "state must never be removed by a test run."
        )
        after_operator = _operator_keys(live)
        if before_operator is not None and after_operator is not None:
            assert after_operator == before_operator, (
                "TEST SUITE FLIPPED OPERATOR KEYS in live "
                f"~/.hscc/autodown.json: enabled/idle_minutes went "
                f"{before_operator} -> {after_operator} (the daemon tick "
                "never rewrites these; only a leaking suite could)"
            )
