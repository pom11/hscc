"""B6 engine-side guard: ``cmd_stop`` refuses an id that cannot name a container.

Audit t_7b0f65c8 measured ``hscc cluster stop --help`` EXECUTING
``sparkrun stop --help`` — the help token was consumed as the CONTAINER ID and
forwarded to a mutating verb (docs/audits/CLI_HELP_2026-10-15.md row B6). The
unified CLI (hscc_daemon/hscc.py) now intercepts ``--help`` before dispatch
(card t_875c5019, tests in hscc_daemon/tests/test_cluster_cli_help.py), but
the standalone ``hscc-cluster stop <id>`` entry — back-compat, and the same
``cmd_stop`` — has no intercept in front of it. The refusal therefore lives in
the engine itself: a destructive verb runs only on an argument that can
actually name a container (the same F6 rule ``up``/``down`` apply to their
flags).

Nothing here may shell out: ``run_cmd`` is stubbed, and a call to it is a
failure for the refusal cases.
"""

import importlib.util
import os
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


def _load_engine():
    """Load the cluster engine under a private alias (same technique the
    daemon's _load_cluster_engine uses — the file is also named hscc.py)."""
    target = os.path.join(PLUGIN_DIR, "hscc.py")
    spec = importlib.util.spec_from_file_location(
        "hscc_cluster_engine_stopguard_test", target)
    assert spec is not None and spec.loader is not None, f"cannot load {target}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCmdStopRefusesNonContainerArgs:
    """Every flag-shaped or empty id is refused BEFORE any subprocess."""

    @pytest.fixture()
    def engine(self, monkeypatch):
        eng = _load_engine()
        calls = []
        monkeypatch.setattr(
            eng, "run_cmd",
            lambda *a, **k: calls.append((a, k)) or {"success": True})
        self.shell_calls = calls
        return eng

    @pytest.mark.parametrize("bad", ["--help", "-h", "--dry-run", "", "   ",
                                     None])
    def test_refused_without_shelling_out(self, engine, bad):
        result = engine.cmd_stop(bad)
        assert self.shell_calls == [], (
            f"cmd_stop({bad!r}) shelled to sparkrun: {self.shell_calls}")
        assert result["success"] is False
        assert "container id" in result["error"]
        # The refusal is a dead end no more: it names where to get a real id.
        assert "hscc cluster status" in result["usage"]

    def test_real_container_id_still_stops(self, engine):
        """The guard must not break the supported call — an over-tight guard
        turns a help fix into an outage."""
        result = engine.cmd_stop("294ec31919c6_f4de31453fdf")
        assert len(self.shell_calls) == 1, self.shell_calls
        argv = self.shell_calls[0][0][0]
        assert argv == ["sparkrun", "stop", "294ec31919c6_f4de31453fdf"]
        assert result.get("success") is True
