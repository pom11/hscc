# Root-level conftest: puts the repo root on sys.path so `import flightdeck`
# works without installing the package. Also injects a fake, non-secret
# Telegram group id into the telegram resolver so tests can exercise telegram
# operations against stubs without a config file or the operator's real group.

import os

import pytest

from flightdeck.core import telegram

# A clearly-fake, public-safe group id used by the test suite. This is NOT the
# operator's real group id — it exists only so the resolver returns a value
# during tests. Tests NEVER touch the network; telegram operations all stub
# the client, and _GROUP_ID is auto-restored to None after each test.
TEST_GROUP_ID = "-1000000000000"


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


@pytest.fixture(autouse=True)
def _inject_test_group(monkeypatch):
    """Give every test a resolvable (fake) Telegram group id.

    The resolver reads the injectable ``_GROUP_ID`` first, so this lets all
    existing telegram call sites run against stubs unchanged. A test that
    specifically exercises the "group not configured" path clears it itself.
    """
    monkeypatch.setattr(telegram, "_GROUP_ID", TEST_GROUP_ID)
