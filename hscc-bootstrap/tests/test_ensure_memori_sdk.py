"""ensure_memori_sdk: the memori SDK must exist in every Hermes-managed env (t_4b0e7956).

Hermetic: fake environment trees under tmp_path and a fake runner, so no
interpreter is probed and nothing is installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ensure_memori_sdk as ems  # noqa: E402


def _env(home: Path, install: str, env: str) -> Path:
    py = home / "installs" / install / "environments" / env / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    return py


class FakeRunner:
    def __init__(self, have: set[str], fail_install: bool = False):
        self.have = set(have)
        self.fail_install = fail_install
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[-2:] == ["-c", "import memori.embeddings"]:
            return subprocess.CompletedProcess(argv, 0 if argv[0] in self.have else 1, "", "")
        if "install" in argv:
            if self.fail_install:
                return subprocess.CompletedProcess(argv, 1, "", "resolver said no")
            self.have.add(argv[argv.index("--python") + 1] if "--python" in argv else argv[0])
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def test_finds_every_managed_environment(tmp_path):
    a = _env(tmp_path, "i1", "e1")
    b = _env(tmp_path, "i1", "e2")
    (tmp_path / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    assert ems.environments(tmp_path) == [a, b]


def test_skips_hermes_test_installs(tmp_path):
    live = _env(tmp_path, "live", "e1")
    _env(tmp_path, "scratch", "e1")
    (tmp_path / "installs" / "scratch" / "test-environment").mkdir()
    assert ems.environments(tmp_path) == [live]


def test_report_only_never_installs(tmp_path):
    py = _env(tmp_path, "i1", "e1")
    run = FakeRunner(have=set())
    report = ems.ensure(tmp_path, apply=False, run=run, uv="uv")
    assert report["environments"] == [{"python": str(py), "status": "missing"}]
    assert not any("install" in c for c in run.calls)


def test_apply_installs_pinned_sdk_without_deps(tmp_path):
    present = _env(tmp_path, "i1", "e1")
    missing = _env(tmp_path, "i1", "e2")
    run = FakeRunner(have={str(present)})
    report = ems.ensure(tmp_path, apply=True, run=run, uv="uv")
    assert [e["status"] for e in report["environments"]] == ["present", "installed"]
    installs = [c for c in run.calls if "install" in c]
    assert installs == [["uv", "pip", "install", "--quiet", "--python", str(missing), "--no-deps", *ems.PINS]]
    assert report["ok"] is True


def test_failed_install_is_reported_not_hidden(tmp_path):
    _env(tmp_path, "i1", "e1")
    report = ems.ensure(tmp_path, apply=True, run=FakeRunner(have=set(), fail_install=True), uv="uv")
    assert report["ok"] is False
    assert report["environments"][0]["status"] == "failed"
    assert "resolver said no" in report["environments"][0]["error"]


def test_pins_keep_protobuf_out():
    """protobuf<6 (memori's pin) would downgrade Hermes's protobuf 6 and break
    google-api-core; the local BYODB path never imports it."""
    assert not any(p.startswith("protobuf") for p in ems.PINS)
    assert "memori==3.3.6" in ems.PINS
