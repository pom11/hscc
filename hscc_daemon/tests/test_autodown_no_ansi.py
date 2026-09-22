"""No-ANSI regression for the `autodown` command group (card 4/5).

The autodown status/enable/disable/wake/cancel commands were migrated to
themed Rich views (hscc_daemon/autodown_cli.py -> cli_theme.make_panel /
make_status_panel). This pins the invariant the migration must not break: on a
NON-tty stdout (pipe/redirect) Rich must degrade to PLAIN text with NO ANSI
escape codes — so scripts that capture output still get clean text. It also
re-checks that the machine ``--json`` path stays a byte-exact raw json.dumps
dump for every command that carries ``--json``, and that no literal ``[ok]`` /
``[warn]`` markup ever leaks to a piped consumer.

Only presentation is exercised here; gating/enable/disable logic is covered by
test_autodown_cli.py. The computation modules (autodown state, autoup) are
stubbed; the CLI is the only rendering layer under test.
"""

import json

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon import lifecycle
from hscc_daemon.autodown_cli import cmd_autodown

NO_ANSI = "\x1b["


# ── fixtures (mirror test_autodown_cli.py so behaviour matches) ─────────────

@pytest.fixture(autouse=True)
def _no_active_crons(monkeypatch):
    """Default: NO active Hermes cron jobs, so enable/status arm normally."""
    monkeypatch.setattr(ad, "list_active_cron_jobs", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_active_prci(monkeypatch):
    """Default: PR/CI interlock CLEAR, so status is hermetic."""
    monkeypatch.setattr(ad, "_has_active_pr_ci", lambda *a, **k: False)
    monkeypatch.setattr(ad, "prci_blocking_signal", lambda: None)
    monkeypatch.setattr(ad, "prci_check_state", lambda: {
        "ok": True, "reason": "", "blocking": None})


@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    return path


@pytest.fixture
def block_file(tmp_path, monkeypatch):
    path = str(tmp_path / "hscc" / "watchdog-block.json")
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", path)
    return path


@pytest.fixture
def closed_env(monkeypatch):
    monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)


# ── status ──────────────────────────────────────────────────────────────────

def test_status_no_ansi_on_non_tty(capsys, autodown_file):
    autodown_file.write_text(json.dumps({
        "enabled": True, "idle_minutes": 10, "state": "up",
        "last_activity_iso": "2026-08-25T07:59:01+00:00",
    }))
    rc = cmd_autodown(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    # a themed Panel titled "autodown" with labelled rows
    assert "╭─ autodown " in out
    assert "autodown: ENABLED" in out
    assert "state:" in out and "up" in out
    assert '"enabled"' not in out  # not a raw JSON blob of the status dict


def test_status_disabled_no_ansi_on_non_tty(capsys, autodown_file):
    autodown_file.write_text(json.dumps({
        "enabled": False, "idle_minutes": 10, "state": "up",
        "last_activity_iso": None,
    }))
    rc = cmd_autodown(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "DISABLED" in out and "(never enabled)" in out


def test_status_json_byte_exact(capsys, autodown_file):
    data = {
        "enabled": True, "idle_minutes": 10, "state": "up",
        "last_activity_iso": "2026-08-25T07:59:01+00:00",
    }
    autodown_file.write_text(json.dumps(data))
    rc = cmd_autodown(["status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out
    # --json is the machine path: valid JSON, no Rich markup, and the raw dict
    # (enriched with derived fields) rather than a themed Panel.
    parsed = json.loads(out)
    assert parsed["enabled"] is True and parsed["state"] == "up"
    assert parsed["idle_minutes"] == 10
    # derived fields are present, not a bare rendering of the 4 config keys
    for key in ("down_since", "watchdog_blocked", "blocked_by"):
        assert key in parsed


# ── enable ──────────────────────────────────────────────────────────────────

def test_enable_no_ansi_on_non_tty(capsys, autodown_file, block_file):
    rc = cmd_autodown(["enable", "--idle-minutes", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"enable leaked ANSI: {out!r}"
    assert "autodown: ENABLED" in out
    assert "(idle_minutes=5)" in out
    # status-panel glyph present in the human heading
    assert "OK" in out


def test_enable_json_byte_exact(capsys, autodown_file, block_file):
    rc = cmd_autodown(["enable", "--idle-minutes", "9", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out
    data = json.loads(out)  # valid JSON, not rich text
    assert data["enabled"] is True and data["idle_minutes"] == 9


# ── disable ─────────────────────────────────────────────────────────────────

def test_disable_no_ansi_on_non_tty(capsys, autodown_file, block_file):
    autodown_file.write_text(json.dumps({
        "enabled": True, "idle_minutes": 10, "state": "down",
        "last_activity_iso": "2026-08-25T07:59:01+00:00",
    }))
    rc = cmd_autodown(["disable"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"disable leaked ANSI: {out!r}"
    assert "DISABLED" in out
    assert "wake" in out  # hint to bring the down layer back up


def test_disable_json_byte_exact(capsys, autodown_file, block_file):
    autodown_file.write_text(json.dumps({
        "enabled": True, "idle_minutes": 10, "state": "down",
        "last_activity_iso": "2026-08-25T07:59:01+00:00",
    }))
    rc = cmd_autodown(["disable", "--json"])
    assert rc == 0
    assert capsys.readouterr().out == json.dumps(
        {"enabled": False, "state": "down"}) + "\n"


# ── wake ────────────────────────────────────────────────────────────────────

def test_wake_no_ansi_on_non_tty(capsys, autodown_file, block_file,
                                 monkeypatch, closed_env):
    monkeypatch.setattr(ad, "autoup",
                        lambda **k: {"result": "up", "started": [], "ready": []})
    rc = cmd_autodown(["wake"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"wake leaked ANSI: {out!r}"
    assert "autodown: serving layer is UP" in out
    assert "wake complete" in out


def test_wake_json_byte_exact(capsys, autodown_file, block_file, monkeypatch,
                              closed_env):
    monkeypatch.setattr(ad, "autoup",
                        lambda **k: {"result": "up", "started": [], "ready": []})
    rc = cmd_autodown(["wake", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out
    data = json.loads(out)
    assert data == {"result": "up", "state": "waking", "wake_source": "cli"}


# ── cancel ──────────────────────────────────────────────────────────────────

def test_cancel_no_ansi_on_non_tty(capsys, autodown_file, block_file):
    rc = cmd_autodown(["cancel"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"cancel leaked ANSI: {out!r}"
    assert "cancel requested" in out


def test_cancel_json_byte_exact(capsys, autodown_file, block_file):
    rc = cmd_autodown(["cancel", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out
    assert out == json.dumps({"cancel_requested": True}) + "\n"


# ── help text ───────────────────────────────────────────────────────────────

def test_help_no_ansi_on_non_tty(capsys):
    rc = cmd_autodown([])
    assert rc == 0
    out = capsys.readouterr().out
    assert NO_ANSI not in out, f"help leaked ANSI: {out!r}"
    for kw in ("status", "enable", "disable", "wake", "cancel"):
        assert kw in out


# ── markup integrity: piped output must never leak literal Rich markup ──────

def test_no_raw_markup_leak(capsys, autodown_file, block_file, monkeypatch,
                            closed_env):
    autodown_file.write_text(json.dumps({
        "enabled": True, "idle_minutes": 10, "state": "up",
        "last_activity_iso": "2026-08-25T07:59:01+00:00",
    }))
    monkeypatch.setattr(ad, "autoup",
                        lambda **k: {"result": "up", "started": [], "ready": []})
    outs = []
    # each command's human view must consume its markup (no leak when piped)
    for argv in (["status"], ["enable"], ["disable"], ["wake"], ["cancel"]):
        cmd_autodown(argv)
        outs.append(capsys.readouterr().out)
    for text in outs:
        for tag in ("[ok]", "[/ok]", "[warn]", "[/warn]",
                    "[error]", "[/error]"):
            assert tag not in text, f"markup leak {tag!r} in {text!r}"
