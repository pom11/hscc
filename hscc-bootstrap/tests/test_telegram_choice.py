"""Tests for telegram_choice — bootstrap's optional Telegram decision.

These exercise resolve_telegram directly with tmp dirs and injected fakes;
they never run bootstrap.sh against the live ~/.hermes.
"""

import json
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import telegram_choice


# ── Interactive prompt ───────────────────────────────────────────────────────
def test_prompt_yes_enables():
    """Answering y at the prompt enables Telegram."""
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "y")
    assert r == {"telegram": "yes", "source": "prompt"}


def test_prompt_full_yes_enables():
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "yes")
    assert r == {"telegram": "yes", "source": "prompt"}


def test_prompt_capital_yes_enables():
    """Y / YES (any case) is accepted — `input` is case-insensitive."""
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "Y")
    assert r == {"telegram": "yes", "source": "prompt"}


def test_prompt_default_is_no():
    """Empty answer (just Enter) declines — default No."""
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "")
    assert r == {"telegram": "no", "source": "prompt"}


def test_prompt_no_declines():
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "n")
    assert r == {"telegram": "no", "source": "prompt"}


def test_prompt_anything_else_declines():
    """Garbage input never enables — anything not y/yes/true/1 is No."""
    r = telegram_choice.resolve_telegram(env={}, assume_yes=False,
                                         prompt=lambda _p: "maybe")
    assert r == {"telegram": "no", "source": "prompt"}


# ── Non-interactive: --yes must not block ───────────────────────────────────
def test_yes_mode_uses_documented_no_default_without_prompting():
    """Under --yes the decision is 'no' and the prompt is never called."""
    called = []

    def raising_prompt(_p):
        called.append(_p)
        raise AssertionError("--yes must not prompt")

    r = telegram_choice.resolve_telegram(env={}, assume_yes=True,
                                         prompt=raising_prompt)
    assert r == {"telegram": "no", "source": "yes-default"}
    assert called == []


# ── Explicit env override (unattended force yes/no) ─────────────────────────
def test_env_yes_forces_enable():
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": "yes"},
                                         assume_yes=False,
                                         prompt=lambda _p: (_ for _ in ()).throw(AssertionError()))
    assert r == {"telegram": "yes", "source": "env"}


def test_env_no_forces_decline():
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": "no"},
                                         assume_yes=False,
                                         prompt=lambda _p: (_ for _ in ()).throw(AssertionError()))
    assert r == {"telegram": "no", "source": "env"}


def test_env_case_insensitive():
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": "YES"},
                                         assume_yes=False, prompt=None)
    assert r == {"telegram": "yes", "source": "env"}


def test_env_synonyms():
    for v in ("y", "true", "1", "YES", "True"):
        r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": v},
                                             assume_yes=False, prompt=None)
        assert r["telegram"] == "yes", v
    for v in ("n", "false", "0", "No"):
        r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": v},
                                             assume_yes=False, prompt=None)
        assert r["telegram"] == "no", v


def test_env_invalid_defaults_to_no():
    """An unrecognized HSCC_TELEGRAM value declines (safe) and is flagged."""
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": "banana"},
                                         assume_yes=False, prompt=None)
    assert r == {"telegram": "no", "source": "env-invalid"}


def test_env_empty_is_ignored():
    """HSCC_TELEGRAM='' is not an override; falls through to the prompt."""
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": ""},
                                         assume_yes=False,
                                         prompt=lambda _p: "y")
    assert r == {"telegram": "yes", "source": "prompt"}


def test_env_beats_yes_flag():
    """An explicit env override wins over --yes' documented no default."""
    r = telegram_choice.resolve_telegram(env={"HSCC_TELEGRAM": "yes"},
                                         assume_yes=True,
                                         prompt=lambda _p: (_ for _ in ()).throw(AssertionError()))
    assert r == {"telegram": "yes", "source": "env"}


# ── __main__ block ───────────────────────────────────────────────────────────
def test_main_block_prints_json():
    """Running `python telegram_choice.py` prints {telegram, source} JSON."""
    import subprocess
    env = dict(os.environ)
    env["HSCC_TELEGRAM"] = "no"
    out = subprocess.run(
        [sys.executable, telegram_choice.__file__],
        capture_output=True, text=True, env=env, check=True).stdout
    assert json.loads(out) == {"telegram": "no", "source": "env"}


# ── The REAL invocation path (regression for the --yes wiring gap) ──────────
# The unit tests above call resolve_telegram() directly, which passes even when
# bootstrap.sh never forwards --yes — giving false confidence on exactly the
# "unattended run must not hang" property. These drive the SCRIPT the way
# bootstrap.sh actually does.

import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys

_SCRIPT = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                        "telegram_choice.py")


def _run_script(args=(), env_extra=None, stdin_text=None, timeout=10):
    env = dict(_os.environ)
    env.pop("HSCC_TELEGRAM", None)
    env.update(env_extra or {})
    return _subprocess.run(
        [_sys.executable, _SCRIPT, *args],
        input=stdin_text if stdin_text is not None else "",
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_script_with_yes_flag_does_not_prompt_and_declines():
    """`--yes` must resolve without reading stdin. Regression: bootstrap.sh
    previously never forwarded --yes, so assume_yes was dead code and an
    unattended run on a TTY blocked forever at the prompt."""
    proc = _run_script(["--yes"], stdin_text="")
    assert proc.returncode == 0, proc.stderr
    out = _json.loads(proc.stdout)
    assert out["telegram"] == "no"
    assert out["source"] == "yes-default"      # took the assume_yes branch


def test_script_with_closed_stdin_declines_cleanly_not_traceback():
    """No stdin (CI) must yield valid JSON declining, not an EOFError traceback
    that bootstrap then silently swallows."""
    proc = _run_script([], stdin_text="")
    assert proc.returncode == 0, proc.stderr
    out = _json.loads(proc.stdout)
    assert out["telegram"] == "no"


def test_script_env_override_still_wins_over_yes():
    proc = _run_script(["--yes"], env_extra={"HSCC_TELEGRAM": "yes"})
    assert proc.returncode == 0, proc.stderr
    out = _json.loads(proc.stdout)
    assert out["telegram"] == "yes"
    assert out["source"] == "env"


def test_script_stdout_is_pure_json_prompt_goes_to_stderr():
    """REGRESSION: the prompt used to be written to stdout, which bootstrap.sh
    captures with $(...) and json.load()s. That polluted the JSON, the parse
    failed, and the shell fell back to "no" — so answering 'y' interactively
    was silently discarded and the operator never even saw the prompt (it was
    swallowed by the capture). stdout must be ONLY the JSON."""
    proc = _run_script([], stdin_text="y\n")
    assert proc.returncode == 0, proc.stderr
    out = _json.loads(proc.stdout)          # must parse with no stripping
    assert out["telegram"] == "yes"         # the 'y' actually took effect
    assert "Add Telegram" in proc.stderr    # prompt shown, on stderr
    assert "Add Telegram" not in proc.stdout
