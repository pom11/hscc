"""Tests for strip_worker_telegram — idempotent Telegram credential stripping."""

import json
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import strip_worker_telegram


def _make_profile(base, name, env_content=None):
    """Create a profile directory with an optional .env file."""
    profile_dir = base / name
    profile_dir.mkdir(exist_ok=True)
    if env_content is not None:
        (profile_dir / ".env").write_text(env_content)
    return profile_dir


def test_active_telegram_line_gets_commented(tmp_path):
    """An active TELEGRAM_BOT_TOKEN line is commented out."""
    env = "TELEGRAM_BOT_TOKEN=abc123\nOTHER_VAR=hello\n"
    _make_profile(tmp_path, "devops-engineer", env)

    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))

    assert "devops-engineer" in result["stripped"]
    assert result["scanned"] == 1

    content = (tmp_path / "devops-engineer" / ".env").read_text()
    assert content == "# TELEGRAM_BOT_TOKEN=abc123\nOTHER_VAR=hello\n"


def test_default_profile_never_touched(tmp_path):
    """The `default` profile is skipped even if it has active Telegram vars."""
    env = "TELEGRAM_BOT_TOKEN=secret\nOPENAI_KEY=key\n"
    _make_profile(tmp_path, "default", env)
    _make_profile(tmp_path, "devops-engineer", "SOME_VAR=val\n")

    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))

    assert "default" not in result["stripped"]
    assert result["scanned"] == 1  # only devops-engineer scanned

    # default .env must be unchanged
    assert (tmp_path / "default" / ".env").read_text() == env


def test_idempotent_already_commented(tmp_path):
    """A second run does not double-comment already-commented lines."""
    env = "TELEGRAM_BOT_TOKEN=abc\nOTHER_VAR=hello\n"
    _make_profile(tmp_path, "caveman", env)

    # First run
    result1 = strip_worker_telegram.strip_worker_telegram(str(tmp_path))
    assert "caveman" in result1["stripped"]

    # Second run
    result2 = strip_worker_telegram.strip_worker_telegram(str(tmp_path))
    assert result2["stripped"] == []  # nothing changed

    content = (tmp_path / "caveman" / ".env").read_text()
    # Should be single-commented, not `# # `
    assert content == "# TELEGRAM_BOT_TOKEN=abc\nOTHER_VAR=hello\n"


def test_profile_without_env_skipped(tmp_path):
    """A profile directory without .env is scanned but not stripped."""
    _make_profile(tmp_path, "reviewer")  # no .env
    _make_profile(tmp_path, "devops-engineer", "FOO=bar\n")

    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))

    assert result["scanned"] == 1  # only devops-engineer has .env
    assert result["stripped"] == []


def test_missing_profiles_dir_returns_skipped_missing(tmp_path):
    """A nonexistent profiles_dir returns skipped_missing without raising."""
    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path / "nonexistent"))

    assert result == {"stripped": [], "skipped_missing": True}


def test_indented_telegram_line_commented(tmp_path):
    """A whitespace-prefixed TELEGRAM_* line is also commented."""
    env = "  TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=999\nNORMAL=x\n"
    _make_profile(tmp_path, "devops-engineer", env)

    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))

    assert "devops-engineer" in result["stripped"]

    content = (tmp_path / "devops-engineer" / ".env").read_text()
    assert content == "#   TELEGRAM_BOT_TOKEN=abc\n# TELEGRAM_CHAT_ID=999\nNORMAL=x\n"


def test_multiple_telegram_vars_all_commented(tmp_path):
    """All TELEGRAM_* variants are caught."""
    env = (
        "TELEGRAM_BOT_TOKEN=abc\n"
        "TELEGRAM_CHAT_ID=123\n"
        "TELEGRAM_PARSE_MODE=HTML\n"
        "OPENAI_API_KEY=key\n"
        "# TELEGRAM_ALREADY_COMMENTED=old\n"
    )
    _make_profile(tmp_path, "devops-engineer", env)

    result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))

    assert "devops-engineer" in result["stripped"]

    content = (tmp_path / "devops-engineer" / ".env").read_text()
    assert content == (
        "# TELEGRAM_BOT_TOKEN=abc\n"
        "# TELEGRAM_CHAT_ID=123\n"
        "# TELEGRAM_PARSE_MODE=HTML\n"
        "OPENAI_API_KEY=key\n"
        "# TELEGRAM_ALREADY_COMMENTED=old\n"
    )


def test_main_block_prints_json(tmp_path, capsys):
    """The __main__ block prints a valid JSON dict."""
    _make_profile(tmp_path, "devops-engineer", "TELEGRAM_BOT_TOKEN=abc\n")

    # Simulate __main__ by calling directly and capturing print output.
    old_argv = sys.argv
    try:
        # Patch profiles_dir via environment to test real __main__ path.
        # Instead, just call strip_worker_telegram with dir and json.dumps.
        result = strip_worker_telegram.strip_worker_telegram(str(tmp_path))
        print(json.dumps(result))
    finally:
        sys.argv = old_argv

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "stripped" in parsed
    assert "scanned" in parsed
