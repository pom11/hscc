"""Tests for install_cli.py — the durable `hscc` CLI entry-point installer.

These use a FAKE venv/bin/hscc console script and a mocked subprocess runner, so
no real interpreter or site-packages are touched. The runner records the commands
it saw and updates the fake entry script's shebang the way a real uv/pip would,
letting us exercise the verify/repair decision tree deterministically.

Every "correct" shebang is derived from the FAKE venv python's own path so it
matches install_cli's expected value by construction. A literal
`#!/fake/venv/bin/python` would never equal the resolved tmp path and would send
every test down the repair path, so we never hardcode it.
"""

from pathlib import Path

import pytest

import install_cli


class FakeRunner:
    """Mimics `uv pip install`: on success writes the fake console script.

    * plain install pops the next shebang off ``shebangs`` (if any) and writes
      the entry — a FRESH install. With ``shebangs == ()`` it is a NO-OP, the
      same as a plain install of an already-satisfied requirement.
    * ``--reinstall`` with ``force_correct=True`` (re)writes the entry with the
      CORRECT venv shebang, as a real uv/pip repair would.
    """

    def __init__(self, root, shebangs=(), rc=0, force_correct=False):
        self.fake_root = Path(root) / "fake"
        self.cli_dir = self.fake_root / "hscc-cli"
        (self.cli_dir / "hscc_cli").mkdir(parents=True, exist_ok=True)
        (self.cli_dir / "pyproject.toml").write_text(
            "[project.scripts]\nhscc = \"hscc_cli:main\"\n")
        self.venv_python = self.fake_root / "venv" / "bin" / "python"
        self.venv_python.parent.mkdir(parents=True, exist_ok=True)
        self.entry = self.venv_python.parent / "hscc"
        self.shebangs = list(shebangs)
        self.rc = rc
        self.force_correct = force_correct
        self.calls = []
        self.written = []

    @property
    def correct(self):
        return f"#{'!'}{self.venv_python}"

    def __call__(self, cmd, env=None):
        self.calls.append(cmd)
        if self.rc != 0:
            return self.rc, "simulated failure"
        if self.shebangs:
            shebang = self.shebangs.pop(0)
            self.entry.write_text(shebang + "\nimport sys\nfrom hscc_cli import main\n")
            self.written.append(shebang)
        elif "--reinstall" in cmd and self.force_correct:
            self.entry.write_text(self.correct + "\nimport sys\nfrom hscc_cli import main\n")
            self.written.append(self.correct)
        return 0, ""


@pytest.fixture
def runner(tmp_path):
    return FakeRunner(tmp_path)


def test_fresh_install_writes_venv_shebang_is_installed(runner):
    # first-ever install: the runner writes the correct shebang on the plain call
    runner.shebangs = [runner.correct]
    res = install_cli.install_cli(runner.venv_python, runner.cli_dir, runner=runner)
    assert res["action"] == "installed"
    assert res["shebang"] == runner.correct
    assert res["expected"] == runner.correct
    assert res["entry"] == str(runner.entry)
    # fresh install must NOT force-reinstall → one plain call, no --reinstall
    assert len(runner.calls) == 1
    assert "--reinstall" not in runner.calls[0]


def test_already_installed_correct_is_verified(runner):
    # Entry already present with the correct shebang; plain install is a no-op
    # (writes nothing, preserves the shebang).
    runner.entry.write_text(runner.correct + "\nimport hscc_cli\n")
    res = install_cli.install_cli(runner.venv_python, runner.cli_dir, runner=runner)

    assert res["action"] == "verified"
    assert res["shebang"] == runner.correct
    # no repair → single plain call, no --reinstall
    assert len(runner.calls) == 1
    assert "--reinstall" not in runner.calls[0]


def test_drifted_shebang_is_repaired(runner):
    # Entry exists with the WRONG interpreter (p313). A plain install is a no-op
    # (does not rewrite the console script), so the repair must force-reinstall.
    runner.force_correct = True
    runner.entry.write_text("#!/tmp/miniconda/envs/p313/bin/python3.13\nimport hscc_cli\n")
    res = install_cli.install_cli(runner.venv_python, runner.cli_dir, runner=runner)

    assert res["action"] == "repaired"
    assert res["shebang"] == runner.correct
    # plain call first, then a --reinstall to repair
    assert len(runner.calls) == 2
    assert "--reinstall" in runner.calls[1]


def test_plain_install_failure_reported(runner):
    runner.rc = 1
    res = install_cli.install_cli(runner.venv_python, runner.cli_dir, runner=runner)

    assert res["action"] == "failed"
    assert "error" in res
    assert res["shebang"] is None


def test_repair_failure_reported(runner):
    # Plain install no-ops on a stale p313 entry; force-reinstall also fails to
    # fix the shebang → must report failed, not falsely claim success.
    runner.entry.write_text("#!/tmp/miniconda/envs/p313/bin/python3.13\nimport hscc_cli\n")
    # force-correct is False, so --reinstall writes nothing → shebang stays wrong
    runner.force_correct = False
    res = install_cli.install_cli(runner.venv_python, runner.cli_dir, runner=runner)

    assert res["action"] == "failed"
    assert res["shebang"] == "#!/tmp/miniconda/envs/p313/bin/python3.13"
