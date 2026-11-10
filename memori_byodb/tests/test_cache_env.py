"""The fastembed model cache must be pinned to ONE shared dir (t_317a0f30).

The memori SDK vendors the Rust ``fastembed`` crate, whose default cache
directory is the *relative* literal ``.fastembed_cache`` — so any process that
loads the embedding model drops an 87 MB copy of
``Qdrant/all-MiniLM-L6-v2-onnx`` into its own cwd. Measured before the fix: nine
such dirs on this host (docs/audits/fastembed-cache-dir-t_317a0f30.md).
``_cache_env.ensure_shared_cache_env()`` is the cure, called at provider import.

Six claims, each pinned so a regression names itself:

  1. the default is ``$HOME/.cache/fastembed`` and is NOT cwd-relative;
  2. an explicit ``FASTEMBED_CACHE_DIR`` wins over the default — paired with a
     non-vacuity assertion that the two paths differ, so the check cannot pass
     by both resolving to the same thing;
  3. ``ensure_shared_cache_env()`` creates the dir, exports it, and is
     idempotent — and never rewrites an operator's value;
  4. it FAILS OPEN: an uncreatable or relative target leaves the environment
     byte-for-byte unchanged and returns ``None`` instead of raising, because a
     cache optimisation must not be able to break the memory provider;
  5. importing the provider the way Hermes does actually performs the pin (a
     child process, so it is the real import path), and the pin's call site sits
     above the import that can reach the SDK;
  6. the cloud provider's copy of the helper (`memori_cloud/_cache_env.py`) has not
     drifted from this one — they are two plugins with no shared module path.

Offline and hermetic: ``$HOME`` and ``FASTEMBED_CACHE_DIR`` are the helper's only
inputs, every path is a tmp dir, and nothing here loads the model (that
end-to-end assertion — a real embed from a scratch cwd — is
``scripts/verify_fastembed_cache_dir.py``, whose measured output is recorded in
the changelog fragment).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _cache_env
from _cache_env import DEFAULT_CACHE_SUBPATH, FASTEMBED_CACHE_DIR_ENV

#: What fastembed writes when nothing pins it — the thing a clean cwd must not gain.
CWD_CACHE_DIRNAME = ".fastembed_cache"

#: An ``~``-prefixed value, i.e. how an operator actually sets the variable.
TILDE_VALUE = "~/explicit-fastembed-cache"


def _home(tmp_path: Path, monkeypatch) -> Path:
    """Point the helper's home at tmp_path (both POSIX and Windows lookups)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(FASTEMBED_CACHE_DIR_ENV, raising=False)
    return tmp_path


# ── 1. the default is host-global, not cwd-relative ──────────────────────────


def test_default_is_the_host_global_dir(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    deep = tmp_path / "somewhere" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    assert _cache_env.shared_cache_dir() == home / DEFAULT_CACHE_SUBPATH


def test_default_never_resolves_inside_the_cwd(tmp_path, monkeypatch):
    """The bug, stated as an assertion: the old default WAS the cwd.

    A relative default is what scattered 87 MB per workspace, so a default that
    happens to land under cwd must fail even if it looks like a sane path.
    """
    home = _home(tmp_path, monkeypatch)
    workspace = tmp_path / "kanban-workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    target = _cache_env.shared_cache_dir()
    assert target.is_absolute()
    assert workspace not in target.parents and target != workspace
    assert target == home / ".cache" / "fastembed"


# ── 2. the operator's setting outranks ours ──────────────────────────────────


def test_explicit_env_wins_over_the_default(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    operator_dir = tmp_path / "volumes" / "nas" / "fastembed"
    monkeypatch.setenv(FASTEMBED_CACHE_DIR_ENV, str(operator_dir))

    # Non-vacuity: the default we would have chosen is a DIFFERENT path, so
    # "explicit wins" cannot be satisfied by the two agreeing.
    assert operator_dir != tmp_path / DEFAULT_CACHE_SUBPATH
    assert _cache_env.shared_cache_dir() == operator_dir


def test_explicit_env_is_expanded_and_trimmed(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv(FASTEMBED_CACHE_DIR_ENV, f"  {TILDE_VALUE}  ")

    assert _cache_env.shared_cache_dir() == Path(TILDE_VALUE).expanduser()


# ── 3. ensure(): create, export, idempotent, never clobber ───────────────────


def test_ensure_creates_the_dir_and_exports_it(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    target = home / DEFAULT_CACHE_SUBPATH

    assert not target.exists()
    assert _cache_env.ensure_shared_cache_env() == target
    assert target.is_dir()
    assert os.environ[FASTEMBED_CACHE_DIR_ENV] == str(target)


def test_ensure_is_idempotent(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    first = _cache_env.ensure_shared_cache_env()

    assert _cache_env.ensure_shared_cache_env() == first
    assert os.environ[FASTEMBED_CACHE_DIR_ENV] == str(first)


def test_ensure_honours_and_creates_an_operator_value(tmp_path, monkeypatch):
    """An operator-set dir is honoured, not argued with — and is still created."""
    _home(tmp_path, monkeypatch)
    operator_dir = tmp_path / "srv" / "shared-model-cache"
    monkeypatch.setenv(FASTEMBED_CACHE_DIR_ENV, str(operator_dir))

    assert _cache_env.ensure_shared_cache_env() == operator_dir
    assert operator_dir.is_dir()
    assert os.environ[FASTEMBED_CACHE_DIR_ENV] == str(operator_dir)


# ── 4. fail open: never raise, never leave a broken pin behind ───────────────


def test_fail_open_when_the_dir_cannot_be_created(tmp_path, monkeypatch):
    """An unwritable target must not become the pin.

    Pointing fastembed at a path it cannot write is worse than the status quo:
    it would break the memory provider to save a cache copy.
    """
    _home(tmp_path, monkeypatch)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a regular file where a parent dir must be\n")
    doomed = blocker / "fastembed"
    monkeypatch.setenv(FASTEMBED_CACHE_DIR_ENV, str(doomed))
    before = os.environ.get(FASTEMBED_CACHE_DIR_ENV)

    assert _cache_env.ensure_shared_cache_env() is None
    assert os.environ.get(FASTEMBED_CACHE_DIR_ENV) == before  # untouched
    assert not doomed.exists()


def test_fail_open_on_a_relative_target(tmp_path, monkeypatch):
    """A relative pin is the original bug — refusing it is rule 3, not rule 1.

    ``Path.home()`` falls through to a literal ``~`` with neither HOME nor a
    passwd entry, which would hand fastembed a cwd-relative path again.
    """
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv(FASTEMBED_CACHE_DIR_ENV, "relative/cache")

    assert _cache_env.ensure_shared_cache_env() is None
    assert os.environ[FASTEMBED_CACHE_DIR_ENV] == "relative/cache"


# ── 5. the wiring: Hermes' import path performs the pin ──────────────────────

#: Runs in a child process. The value BEFORE importing the provider must be
#: empty — otherwise every assertion below is vacuous, because the parent's env
#: or a launcher could have set it — and the value AFTER must be the shared dir.
_WIRING_SNIPPET = """
import json, os
from pathlib import Path
before = os.environ.get("FASTEMBED_CACHE_DIR")
import memori_byodb  # noqa: F401  <- the import Hermes performs
after = os.environ.get("FASTEMBED_CACHE_DIR")
print(json.dumps({
    "before": before,
    "after": after,
    "cwd": os.getcwd(),
    "cwd_entries": sorted(p.name for p in Path(os.getcwd()).iterdir()),
    "exists": bool(after) and Path(after).is_dir(),
}))
"""


def _provider_shim_dir(probe_root: Path, repo_root: Path) -> Path:
    """A ``sys.path`` entry exposing ONLY ``memori_byodb``, as a symlink.

    The repo root must not go on the child's path: before t_2ab0eda1 the repo's
    top-level ``memori/`` (cloud provider) shadowed the installed ``memori`` SDK
    for any process whose path contained it — measured as ``ImportError: cannot
    import name 'embed_texts' from 'memori' (.../hscc/memori/__init__.py)``.
    The cloud dir is now ``memori_cloud/``, but the shim is still the honest
    shape: Hermes loads plugins by file location, and the child only needs this
    one provider, not the whole repo root.
    """
    shim = probe_root / "provider-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "memori_byodb"
    if not link.exists():
        link.symlink_to(repo_root / "memori_byodb", target_is_directory=True)
    return shim


def _child_env(tmp_path: Path, pythonpath: str) -> dict:
    """A minimal, honest agent env: its own HOME, no FASTEMBED_CACHE_DIR."""
    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PATH": os.defpath,
        "PYTHONPATH": pythonpath,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # A venv interpreter run by absolute path still wants these to resolve.
    for var in ("VIRTUAL_ENV", "SYSTEMROOT"):
        if var in os.environ:
            env[var] = os.environ[var]
    return env


def test_importing_the_provider_pins_the_cache_dir(tmp_path):
    """The contract is the *import*, not the function — so test the import.

    The child's HOME is a tmp dir and its cwd is a scratch workspace, i.e. the
    shape of a real agent process: provider import, then a cwd that must stay
    clean.
    """
    repo_root = Path(_cache_env.__file__).resolve().parents[1]
    workspace = tmp_path / "kanban-workspace"
    workspace.mkdir()
    shim = _provider_shim_dir(tmp_path, repo_root)

    proc = subprocess.run(
        [sys.executable, "-c", _WIRING_SNIPPET],
        cwd=workspace, env=_child_env(tmp_path, str(shim)),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr

    out = json.loads(proc.stdout.strip().splitlines()[-1])
    expected = str(tmp_path / DEFAULT_CACHE_SUBPATH)

    # Non-vacuity: nothing outside the provider set it.
    assert out["before"] is None, "FASTEMBED_CACHE_DIR leaked from the parent env"
    assert out["after"] == expected, out
    assert out["exists"] is True
    assert CWD_CACHE_DIRNAME not in out["cwd_entries"], (
        f"provider import created {CWD_CACHE_DIRNAME} in the cwd: {out}")


def test_pin_call_site_precedes_the_import_that_can_reach_the_sdk():
    """Order inside ``__init__.py`` is load-bearing, so it is pinned as text.

    A behavioural test cannot see this: the pin lands during the same import
    whichever side of ``._client`` it sits on. What has to hold is that it runs
    *first*, so a future module-level ``from memori import ...`` placed above it
    cannot re-open the bug without failing here.
    """
    init = Path(_cache_env.__file__).with_name("__init__.py").read_text()
    pin = init.index("ensure_shared_cache_env()")
    reaches_sdk = min(
        init.index(marker)
        for marker in ("from ._client import", "from memori", "import memori")
        if marker in init
    )
    assert pin < reaches_sdk, (
        "ensure_shared_cache_env() must run before the import that can reach "
        "the memori SDK (see the comment at the call site)")


# ── 6. the cloud provider's copy has not drifted ─────────────────────────────

#: `memori_cloud/` (the cloud provider) ships its own copy of this helper: the two
#: plugins deploy to separate dirs and share no importable module path, so the
#: fix cannot live in one file both can reach. Identical code, different
#: docstrings — hence an AST comparison with the module docstring dropped, not a
#: hash. In the deployed layout (`~/.hermes/plugins`) the sibling exists but is
#: a third-party-shaped tree, so this only asserts anything where the repo's
#: layout is present.
SIBLING = Path(_cache_env.__file__).resolve().parents[1] / "memori_cloud" / "_cache_env.py"


def test_the_two_provider_copies_have_not_drifted():
    if not SIBLING.is_file():
        pytest.skip("not the repo layout — no sibling provider to compare")

    def code(path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            tree.body.pop(0)  # the docstrings differ on purpose
        return ast.dump(tree)

    assert code(Path(_cache_env.__file__)) == code(SIBLING), (
        f"{SIBLING} has drifted from memori_byodb/_cache_env.py — the two "
        "providers must pin the cache the same way (see the header note)")
