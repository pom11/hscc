"""`hscc_cli/__init__.py` candidate resolution — brew libexec support (t_bc5a3fee).

The wrapper lives in its OWN pip package (hscc-cli/) with no tests dir of its
own (a tests dir there would need a new run_tests.sh DIRS entry — census-guarded),
so its resolution logic is tested here by loading the REAL module source from a
FABRICATED location: the module body resolves candidates relative to its own
``__file__``, so copying it into a fake brew layout and executing it under a
fresh module name exercises exactly the shipped code, and the fabricated tree
carries a fabricated `hscc_daemon/hscc.py` whose `main` proves WHICH tree won.

The daemon suite imports the REAL `hscc_daemon`, so `sys.modules` is purged of
it around each probe (and restored after) — otherwise `from hscc_daemon.hscc
import main` would silently return the cached real module and every "which
tree won" assertion would be a lie.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INIT = REPO / "hscc-cli" / "hscc_cli" / "__init__.py"

_COUNTER = [0]


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """Loader + teardown: purge + restore `hscc_daemon*` modules, and drop any
    sys.path entries a probe inserted, so one probe can't poison another."""
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == "hscc_daemon" or k.startswith("hscc_daemon.")}

    def load(pkg_dir: Path):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))  # neutralise ~/.hermes
        target = pkg_dir / "__init__.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(INIT.read_text())
        _COUNTER[0] += 1
        name = f"_hscc_cli_probe_{_COUNTER[0]}"
        for k in [k for k in sys.modules
                  if k == "hscc_daemon" or k.startswith("hscc_daemon.")]:
            del sys.modules[k]
        spec = importlib.util.spec_from_file_location(
            name, str(target), submodule_search_locations=[str(pkg_dir)])
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules.pop(name, None)
        spec.loader.exec_module(mod)
        return mod

    yield load

    for k in [k for k in sys.modules
              if k == "hscc_daemon" or k.startswith("hscc_daemon.")]:
        del sys.modules[k]
    sys.modules.update(saved_mods)
    del sys.path[:]
    sys.path.extend(saved_path)


def _fabricate_tree(prefix: Path, version: str):
    ver = prefix / "libexec" / f"hscc-{version}"
    (ver / "hscc_daemon").mkdir(parents=True)
    (ver / "hscc_daemon" / "__init__.py").write_text("")
    (ver / "hscc_daemon" / "hscc.py").write_text(
        "def main():\n    return %r\n" % version)
    return ver


def test_wrapper_resolves_payload_under_brew_libexec(tmp_path, probe):
    """The brew scenario: hscc_cli sits in libexec/venv/.../site-packages; the
    payload is libexec/hscc-<ver>/hscc_daemon. The wrapper must insert the
    versioned dir on sys.path and import ITS main."""
    prefix = tmp_path / "opt"
    ver = _fabricate_tree(prefix, "2.12.0")
    site = prefix / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    mod = probe(site / "hscc_cli")
    assert mod.main() == "2.12.0"
    assert str(ver.resolve()) in sys.path


def test_wrapper_prefers_newest_libexec_version(tmp_path, probe):
    prefix = tmp_path / "opt"
    _fabricate_tree(prefix, "2.11.0")
    _fabricate_tree(prefix, "2.12.0")
    site = prefix / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    mod = probe(site / "hscc_cli")
    assert mod.main() == "2.12.0"


def test_wrapper_version_order_is_numeric_not_lexical(tmp_path, probe):
    """hscc-2.9.0 must NOT outrank hscc-2.10.0 (lexicographic sort would)."""
    prefix = tmp_path / "opt"
    _fabricate_tree(prefix, "2.9.0")
    _fabricate_tree(prefix, "2.10.0")
    site = prefix / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    mod = probe(site / "hscc_cli")
    assert mod.main() == "2.10.0"


def test_wrapper_ignores_libexec_dirs_without_daemon(tmp_path, probe):
    """A libexec/hscc-* without hscc_daemon is not a payload root: the
    ancestor is consumed (break) but the empty dir must not enter sys.path,
    and resolution falls through harmlessly (nothing fabricated is inserted)."""
    prefix = tmp_path / "opt"
    (prefix / "libexec" / "hscc-empty").mkdir(parents=True)
    site = prefix / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    probe(site / "hscc_cli")
    assert str((prefix / "libexec" / "hscc-empty").resolve()) not in sys.path


def test_wrapper_non_brew_layout_unchanged(tmp_path, probe):
    """Deployed sibling-plugins layout still resolves via candidate #1 — the
    brew support is a pure no-op there. Candidate #1 is `<pkg-parent>/plugins`
    (a `plugins` dir next to the package dir inside the same root)."""
    root = tmp_path / "deployed"
    (root / "plugins" / "hscc_daemon").mkdir(parents=True)
    (root / "plugins" / "hscc_daemon" / "__init__.py").write_text("")
    (root / "plugins" / "hscc_daemon" / "hscc.py").write_text(
        "def main():\n    return 'deployed'\n")
    mod = probe(root / "hscc_cli")
    assert mod.main() == "deployed"
    assert str((root / "plugins").resolve()) in sys.path
