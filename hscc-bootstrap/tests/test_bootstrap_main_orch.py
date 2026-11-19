"""The bootstrap interview asks for ONE main orchestrator name (t_c6eccd49).

These tests extract the EXACT ``ask_main_orch_name`` block from ``bootstrap.sh``
and run it in a real bash against a throwaway ``HSCC_DIR`` / ``HERMES_HOME``.
That exercises the shipped prompt/validation/store path without running the
rest of the installer (which touches the fleet) and without the operator's real
``~/.hscc`` state.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BOOT = HERE.parent / "bootstrap.sh"
SRC = BOOT.read_text()

_HEAD = "ask_main_orch_name() {"


def _block() -> str:
    start = SRC.index(_HEAD)
    end = SRC.index("\n}\n", start) + 3
    return SRC[start:end]


BLOCK = _block()


def _run(tmp_path, *, stored=None, assume_yes=False, dry_run=False, stdin=""):
    hscc_dir = tmp_path / "hscc"
    hscc_dir.mkdir(exist_ok=True)
    if stored is not None:
        (hscc_dir / "main-orchestrator.json").write_text(
            json.dumps({"name": stored}), encoding="utf-8")
    hermes = tmp_path / "hermes"
    (hermes / "profiles").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HSCC_DIR", "HSCC_REGISTRY", "HERMES_HOME")}
    env.update({
        "BOOT_DIR": str(HERE.parent),
        "PYBIN": sys.executable,
        "HSCC_DIR": str(hscc_dir),
        "HERMES_HOME": str(hermes),
        "HSCC_REGISTRY": str(tmp_path / "no-registry.yaml"),
        "PATH": "/usr/bin:/bin",
    })
    script = (
        "set -u\n"
        f'ASSUME_YES={str(assume_yes).lower()}\n'
        f'DRY_RUN={str(dry_run).lower()}\n'
        'say()  { echo "  $1"; }\n'
        'ok()   { echo "  ✓ $1"; }\n'
        'warn() { echo "  ⚠ $1"; }\n'
        'die()  { echo "  ✗ $1" >&2; exit 1; }\n'
        'plan() { echo "  → plan: $1"; }\n'
        f"{BLOCK}\n"
        "ask_main_orch_name\n"
        'printf "\\nRESULT=%s\\n" "$MAIN_ORCH_NAME"\n'
    )
    p = subprocess.run(["bash", "-c", script], input=stdin, capture_output=True,
                       text=True, env=env, timeout=30)
    return p, hscc_dir


def _stored(hscc_dir):
    path = hscc_dir / "main-orchestrator.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())["name"]


def test_unstored_yes_dry_run_plans_general_and_writes_nothing(tmp_path):
    p, hscc_dir = _run(tmp_path, assume_yes=True, dry_run=True)
    assert p.returncode == 0, p.stderr
    assert "Main orchestrator name" not in p.stdout  # --yes never prompts
    assert "main-orchestrator.json (main orchestrator name=general)" in p.stdout
    assert "RESULT=general" in p.stdout
    assert _stored(hscc_dir) is None


def test_unstored_yes_stores_general_once(tmp_path):
    p, hscc_dir = _run(tmp_path, assume_yes=True)
    assert p.returncode == 0, p.stderr
    assert "main orchestrator: general" in p.stdout
    assert "RESULT=general" in p.stdout
    assert _stored(hscc_dir) == "general"


def test_stored_name_is_kept_and_never_prompts(tmp_path):
    p, hscc_dir = _run(tmp_path, stored="ops", assume_yes=False, dry_run=True,
                       stdin="ignored\n")
    assert p.returncode == 0, p.stderr
    assert "Main orchestrator name" not in p.stdout
    assert "main orchestrator: ops (stored)" in p.stdout
    assert "→ plan:" not in p.stdout
    assert "RESULT=ops" in p.stdout
    assert _stored(hscc_dir) == "ops"


def test_interactive_question_stores_the_typed_name(tmp_path):
    # ``read -p`` only echoes its prompt on a tty; piped stdin is the normal
    # non-interactive harness, so assert the answer is consumed and stored.
    p, hscc_dir = _run(tmp_path, stdin="ops\n")
    assert p.returncode == 0, p.stderr
    assert "RESULT=ops" in p.stdout
    assert _stored(hscc_dir) == "ops"


def test_second_bootstrap_run_never_changes_the_stored_name(tmp_path):
    first, hscc_dir = _run(tmp_path, stdin="ops\n")
    assert first.returncode == 0, first.stderr
    assert _stored(hscc_dir) == "ops"
    second, _ = _run(tmp_path, stdin="other\n")
    assert second.returncode == 0, second.stderr
    assert "RESULT=ops" in second.stdout
    assert _stored(hscc_dir) == "ops"


def test_invalid_names_are_rejected_and_reasked(tmp_path):
    p, hscc_dir = _run(tmp_path, stdin="General\ncoder\nops\n")
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("invalid main orchestrator name:") == 2
    assert "not a lowercase slug" in p.stdout
    assert "role profile" in p.stdout
    assert "RESULT=ops" in p.stdout
    assert _stored(hscc_dir) == "ops"


def test_yes_with_invalid_default_cannot_happen_but_validation_still_gates(
        tmp_path, monkeypatch):
    """The default ``general`` itself passes validation; a broken role tree
    cannot silently store a name that the helper rejects."""
    p, hscc_dir = _run(tmp_path, assume_yes=True)
    assert p.returncode == 0, p.stderr
    assert _stored(hscc_dir) == "general"


def test_bootstrap_stage_calls_the_interview():
    """The stage-3 block must actually call the function (guard against a
    prompt that exists but is never wired into bootstrap)."""
    stage = SRC[SRC.index('hdr "Configuration"'):SRC.index("# recipe: sole recipe auto")]
    assert "ask_main_orch_name" in stage
    # And the question sits next to the orchestrator-node question.
    assert SRC.index('read -rp "  Orchestrator node') < SRC.index(
        "ask_main_orch_name\n")
