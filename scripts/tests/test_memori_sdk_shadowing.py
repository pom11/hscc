"""The repo/deployed ``memori/`` provider must not shadow the installed ``memori`` SDK.

Why this file exists (measurements in ``docs/audits/memori-sdk-shadowing-t_2ab0eda1.md``):
this repo stages the Memori *cloud* Hermes provider as a top-level package named
``memori`` — the same import name as the third-party SDK that
``memori/client.py:30`` and ``memori_byodb/_client.py:90`` both need
(``from memori import Memori``). ``sys.path`` entries from ``PYTHONPATH`` and the
interpreter's own cwd entry are prepended ahead of site-packages, so any process
whose path contains a directory holding ``memori/`` resolves ``memori`` to the
provider and cannot import the SDK *at all* — the SDK's own ``__init__`` performs
absolute intra-package imports, so a partial import is not even available (95 SDK
files do; measured).

Three groups, and the split is deliberate:

  1. GREEN + HERMETIC — the mechanism reproduced in a tmp fixture (a fake provider
     package + a fake SDK on a later path entry) and the shipped check's verdicts
     proven on both fixtures. These run anywhere, with or without the SDK
     installed, and they are what prove the check can tell the two situations apart.
  2. GREEN — the shim defence the suite already depends on
     (``_provider_shim_dir``): a child that imports the provider gets a directory
     holding ONE symlink, never the repo root. If someone "simplifies" that back to
     the repo root, that child dies with this exact shadowing, so it is pinned.
  3. ``xfail(strict=True)`` — the repo/deploy state itself. Today the check exits 1
     (``python3 scripts/verify_memori_sdk_shadowing.py`` is the executable form of
     this card's acceptance criterion). The layout fix — rename the provider dir, or
     stop putting those roots on ``sys.path`` — is owned by whoever owns the plugin
     layout (card t_2ab0eda1 defers it explicitly), so these stay strict-xfail: the
     suite stays green while the decision is open, and the day the fix lands they
     XPASS and turn the leg RED. That red is the signal to DELETE THE MARKER, not to
     delete the test.

Nothing here writes outside a tmp dir, and nothing imports the provider in process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
PROBE = SCRIPTS / "verify_memori_sdk_shadowing.py"

#: ``scripts/tests/conftest.py`` redirects HOME to a tmp dir for every test (its
#: autouse hermeticity fixture), so ``Path.home()`` INSIDE a test is not the
#: operator's home. The deployed-provider case has to look at the real one, so it
#: is captured here, at import, which runs during collection — before any fixture.
OPERATOR_HOME = Path.home()

#: The SDK symbols the two providers import — the same pair the check uses.
SDK_SYMBOLS = ("Memori", "embed_texts")

#: A stand-in for the installed SDK: the two public symbols, plus one submodule,
#: so an absolute intra-package import is exercised in the fixture too.
FAKE_SDK_INIT = '''"""Stand-in for the installed memori SDK (guard fixture)."""


class Memori:
    def __init__(self, conn=None):
        self.conn = conn


def embed_texts(texts, model="fixture-model"):
    return [[0.0, 0.0] for _ in texts]
'''

FAKE_SDK_SUB = '''from memori import Memori

DRIVER = Memori
'''

#: A stand-in for the provider: a package named ``memori`` that does NOT carry the
#: SDK's symbols — exactly the shape of ``<repo root>/memori/``.
FAKE_PROVIDER_INIT = '''"""Stand-in for hscc's cloud provider package (guard fixture)."""

PLUGIN_NAME = "memori"


def register(ctx):
    return None
'''

#: The child program, with the same shape as the check's own child: report where
#: ``memori`` resolved, try the SDK's public symbols, and try one absolute
#: intra-package import (the one that kills any "load it under a private name"
#: workaround — audit §M4). ``__SYMBOLS__`` is substituted by the caller; there is
#: deliberately no ``%``-formatting here, because the body carries ``%s`` literals.
_CHILD_SRC = """
import json, memori
out = {'resolved': getattr(memori, '__file__', None)}
for name in __SYMBOLS__:
    try:
        exec('from memori import ' + name, {})
    except Exception as exc:
        out[name] = '%s: %s' % (type(exc).__name__, exc)
    else:
        out[name] = 'OK'
try:
    import memori._driver
    out['submodule'] = 'OK'
except Exception as exc:
    out['submodule'] = '%s: %s' % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture_sdk_root(tmp_path: Path) -> Path:
    """A path root whose ``memori`` IS a working stand-in SDK."""
    root = tmp_path / "fake-sdk-root"
    _write(root / "memori" / "__init__.py", FAKE_SDK_INIT)
    _write(root / "memori" / "_driver.py", FAKE_SDK_SUB)
    return root


def _fixture_shadow_root(tmp_path: Path) -> Path:
    """A path root whose ``memori`` is a provider lookalike without SDK symbols."""
    root = tmp_path / "fake-shadow-root"
    _write(root / "memori" / "__init__.py", FAKE_PROVIDER_INIT)
    return root


def _child(pythonpath: list[str], tmp_path: Path) -> dict:
    """Run the same kind of child the check runs, on an explicit path list."""
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": os.pathsep.join(pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for var in ("VIRTUAL_ENV", "SYSTEMROOT"):
        if var in os.environ:
            env[var] = os.environ[var]
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SRC.replace("__SYMBOLS__", repr(list(SDK_SYMBOLS)))],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── 1. the mechanism, proven hermetically ────────────────────────────────────


def test_a_path_entry_holding_memori_shadows_a_later_sdk_root(tmp_path):
    """The whole hazard in one assertion, with no real repo and no real SDK.

    ``PYTHONPATH=<provider root>:<sdk root>`` — the provider root comes first, so
    the name ``memori`` is its package and the SDK root further along the path is
    unreachable. The paired control (SDK root alone) is the non-vacuity: the same
    child, the same fixture SDK, one path entry shorter, imports fine.
    """
    shadow, sdk = _fixture_shadow_root(tmp_path), _fixture_sdk_root(tmp_path)

    bad = _child([str(shadow), str(sdk)], tmp_path)
    assert bad["resolved"] == str(shadow / "memori" / "__init__.py"), bad
    for symbol in SDK_SYMBOLS:
        assert bad[symbol] != "OK", (
            "the fixture did not shadow — the child reached the SDK anyway: %s" % bad)
    # The SDK cannot even be entered: its own absolute imports resolve through the
    # name the provider owns. This is why "load it under a private name" is not a
    # fix (audit §M4).
    assert bad["submodule"] != "OK", bad

    good = _child([str(sdk)], tmp_path)
    assert good["resolved"] == str(sdk / "memori" / "__init__.py"), good
    for symbol in SDK_SYMBOLS:
        assert good[symbol] == "OK", good
    assert good["submodule"] == "OK", good


def test_check_reports_shadowing_for_a_fixture_root(tmp_path):
    """The shipped check's detection, driven over a fixture — green either way."""
    expected = _fixture_shadow_root(tmp_path) / "memori" / "__init__.py"
    res = _probe(["--skip-control", "--path", str(expected.parent.parent)])
    assert res["verdict"] == "FAIL", res
    case = res["cases"][0]
    assert case["verdict"] == "FAIL" and case["shadowed"] is True, case
    assert Path(case["resolved"]).samefile(str(expected)), case


def test_check_passes_for_a_root_that_holds_the_sdk(tmp_path):
    """The other direction: a root that legitimately holds the SDK must PASS.

    Without this pair the check would be a "did the file come from that dir" guess
    and could report PASS by never having seen a passing shape at all.
    """
    res = _probe(["--skip-control", "--path", str(_fixture_sdk_root(tmp_path))])
    assert res["verdict"] == "PASS", res
    case = res["cases"][0]
    assert case["verdict"] == "PASS", case
    assert all(case["symbols"][s] == "OK" for s in SDK_SYMBOLS), case


# ── 2. the defence the suite already depends on ─────────────────────────────


def _shim_dir(probe_root: Path, repo_root: Path) -> Path:
    """A ``sys.path`` entry exposing ONLY ``memori_byodb``, as one symlink.

    The recipe is ``memori_byodb/tests/test_cache_env.py::_provider_shim_dir`` —
    this is the shape a provider child must get instead of the repo root.
    """
    shim = probe_root / "provider-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "memori_byodb"
    if not link.exists():
        link.symlink_to(repo_root / "memori_byodb", target_is_directory=True)
    return shim


def test_shim_exposes_the_provider_without_owning_the_sdk_name(tmp_path):
    """A shim dir is the safe root: it exposes the provider, never ``memori``."""
    shim = _shim_dir(tmp_path, REPO_ROOT)
    assert (shim / "memori_byodb" / "__init__.py").is_file(), list(shim.iterdir())
    assert not (shim / "memori").exists(), (
        "the shim must not expose a `memori` package — that is the shadow")


@pytest.mark.parametrize("consumer", [
    "scripts/verify_fastembed_cache_dir.py",
    "memori_byodb/tests/test_cache_env.py",
])
def test_provider_children_still_get_a_shim_not_the_repo_root(consumer):
    """The two shipped provider-children must keep using the shim.

    Text-pinned on purpose: a behavioural test cannot see a future edit that
    "simplifies" the child's path back to the repo root, and that edit does not
    delete any assertion — it only makes the child die with this card's ImportError
    while every other case in the file still looks meaningful.
    """
    text = (REPO_ROOT / consumer).read_text(encoding="utf-8")
    assert "_provider_shim_dir" in text, (
        "%s no longer routes its provider child through the shim dir" % consumer)
    for offender in ("PYTHONPATH\": str(repo_root)", "pythonpath=[str(repo_root)]",
                     "pythonpath=[repo_root]"):
        assert offender not in text, (
            "%s puts the repo root on a child's path (%s) — that child then "
            "resolves `memori` to the cloud provider" % (consumer, offender))


# ── 3. the repo/deploy state itself: red until the layout decision lands ────


def _probe(argv: list[str]) -> dict:
    proc = subprocess.run([sys.executable, str(PROBE), "--json"] + argv,
                          capture_output=True, text=True, timeout=600,
                          cwd=str(REPO_ROOT))
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    payload["_rc"] = proc.returncode
    return payload


def _require_decidable(res: dict) -> None:
    if res["verdict"] == "UNDECIDED":
        pytest.skip("the memori SDK is not importable in a clean process here "
                    "(%s) — shadowing cannot be judged"
                    % res.get("sdk_resolved_in_control"))


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "the repo root holds a package named `memori`, so `python -m pytest` from the "
    "repo root (scripts/run_tests.sh:247) cannot import the memori SDK in any leg; "
    "fix = rename the provider dir or stop putting that root on sys.path — "
    "t_2ab0eda1, docs/audits/memori-sdk-shadowing-t_2ab0eda1.md. When this XPASSes, "
    "delete the marker and keep the test."))
def test_repo_root_on_path_must_not_block_the_sdk():
    res = _probe(["--cases", "repo-root,leg-shape"])
    _require_decidable(res)
    assert res["verdict"] == "PASS", res
    assert res["_rc"] == 0, res


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "the DEPLOYED plugins dir holds a package named `memori` (it ships via "
    "install_payload.DEFAULT_PAYLOAD), so any process with ~/.hermes/plugins on "
    "sys.path — scripts/dep_pr_watcher.py:49, scripts/escalate_watcher_run.py:45, "
    "hscc-cluster/fleet.py:12 — cannot import the memori SDK. t_2ab0eda1."))
def test_deployed_plugins_dir_must_not_block_the_sdk():
    deployed = OPERATOR_HOME / ".hermes" / "plugins"
    if not (deployed / "memori" / "__init__.py").is_file():
        pytest.skip("no deployed memori provider at %s on this host" % deployed)
    res = _probe(["--cases", "deployed-plugins", "--home", str(OPERATOR_HOME)])
    _require_decidable(res)
    assert res["verdict"] == "PASS", res
    assert res["_rc"] == 0, res


def test_check_itself_is_honest_about_a_shadowed_repo():
    """The acceptance criterion of t_2ab0eda1, as an executable that fails today.

    This test asserts the CHECK bites — not that the repo is clean. It is green now
    and stays green after the fix, because the check is supposed to report the truth
    in both states; the state assertions are the two xfail cases above.
    """
    res = _probe(["--cases", "repo-root"])
    _require_decidable(res)
    case = [c for c in res["cases"] if c["case"] == "repo-root"][0]
    if case["verdict"] == "PASS":
        # Post-fix: the check must now report PASS, and the xfail case above is the
        # thing that goes red to ask for its marker back.
        assert res["verdict"] == "PASS", res
        return
    assert case["shadowed"] is True, case
    assert res["verdict"] == "FAIL" and res["_rc"] == 1, res
