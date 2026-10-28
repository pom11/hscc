"""The bootstrap node-recovery stage's inline summariser, exercised for real.

bootstrap.sh embeds a python summariser for install_autoreboot.py's JSON. Like
the installer itself, nothing in the Python suite executed it — until
bootstrap.sh shipped a broken heredoc around it. This test extracts the EXACT
embedded body from bootstrap.sh and execs it against fabricated summaries, so
a syntax error, a key drift, or a false-green render fails the suite instead
of an operator's install.

Honest-signal pins (fleet rule — an env fault must never present as a data
fact): any node failure, or zero nodes armed, must lead with WARN, never OK.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BOOT = HERE.parent / "bootstrap.sh"

_MARKER_HEAD = "AR_STATUS=$(\"$PYBIN\" -c '"
_MARKER_TAIL = "' \"$AR_JSON\" 2>/dev/null)"


def _summariser_body() -> str:
    text = BOOT.read_text()
    start = text.index(_MARKER_HEAD) + len(_MARKER_HEAD) + 1
    end = text.index(_MARKER_TAIL, start)
    return text[start:end]


BODY = _summariser_body()


def run(payload: str) -> str:
    p = subprocess.run([sys.executable, "-c", BODY, payload],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"summariser crashed on {payload[:60]}: {p.stderr}"
    return p.stdout


def test_stage_exists_and_flag_wired():
    text = BOOT.read_text()
    assert "install_autoreboot.py" in text
    assert "--skip-node-recovery) SKIP_NODE_RECOVERY=true" in text
    assert "if $SKIP_NODE_RECOVERY; then" in text


def test_all_armed_renders_ok():
    out = run('{"results": [{"host": "10.0.0.1", "action": "installed"},'
              ' {"host": "10.0.0.2", "action": "unchanged"}],'
              ' "ok": true, "skipped_reason": null}')
    assert out.startswith("OK|node-recovery sysctls armed+read-back-verified on 2/2")


def test_failures_never_render_as_green():
    out = run('{"results": [{"host": "10.0.0.1", "action": "failed",'
              ' "error": "read-back mismatch — kernel.panic: expected \'10\' got \'0\'"},'
              ' {"host": "10.0.0.2", "action": "drift", "note": "left untouched"}],'
              ' "ok": false, "skipped_reason": null}')
    assert out.startswith("WARN|"), f"failures rendered as OK: {out}"
    assert "WARN|10.0.0.1: read-back mismatch" in out
    assert "WARN|10.0.0.2: left untouched" in out
    assert "some nodes FAILED" in out


def test_optout_renders_ok_skip():
    out = run('{"results": [], "ok": true,'
              ' "skipped_reason": "node_recovery.autoreboot is false"}')
    assert out.startswith("OK|node-recovery sysctls skipped")


def test_stdout_noise_cannot_hide_the_json():
    payload = ('{"results": [{"host": "10.0.0.1", "action": "installed"}],'
               ' "ok": true, "skipped_reason": null}')
    out = run("some stderr chatter\n" + payload)
    assert out.startswith("OK|")


def test_garbage_output_warns_with_a_recipe():
    out = run("definitely not json")
    assert out.startswith("WARN|unparseable")
    assert "install_autoreboot.py --hosts" in out


def test_empty_results_flag_config_not_fleet():
    out = run('{"results": [], "ok": true, "skipped_reason": null}')
    assert "no nodes were checked" in out
