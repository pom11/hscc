# Root-level conftest: puts the repo root on sys.path so `import flightdeck`
# works without installing the package.

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _sandbox_qa_home(tmp_path_factory):
    """Point HERMES_HOME at a per-session temp sandbox for the whole suite.

    qa.py's persistent-state helpers (notified set + manual-QA store) resolve
    their DEFAULT paths under ``qa_home()``, which honours HERMES_HOME when set.
    By pointing it at a fresh temp root here, every default-constructed qa call
    during the suite writes strictly under the sandbox — never the operator's
    real ``~/.flightdeck``. The env var is restored at session end.
    """
    root = str(tmp_path_factory.mktemp("qa-home"))
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = root
    yield root
    if old is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = old
