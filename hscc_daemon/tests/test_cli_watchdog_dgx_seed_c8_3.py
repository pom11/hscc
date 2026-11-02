"""CLI edge for proxy-stall c8-3: `hscc check watchdog` must still be a real check.

The live daemon's watchdog reads the published DGX verdict instead of running
`check_dgx` (see test_dgx_single_caller_c8_3.py). A CLI process has no dgx stream
thread to publish one, so `cli._cli_watchdog_with_dgx` seeds the verdict first.
These tests pin the two properties that make that wrapper worth having:

  1. it runs the DGX check exactly once when nothing has run yet (the operator
     gets the DGX evidence they asked for, not a neutral pass);
  2. it does NOT re-run the sweep when a verdict is already published, which is
     what makes `hscc check all` cheaper than it was (the dgx entry and the
     watchdog entry used to run two full sweeps back to back).

Property 2 is the one a careless "always run it" implementation would pass 1 on
and fail here, so both are asserted against call counts rather than log text.
"""

import pytest

from hscc_daemon import cli, health, lifecycle


@pytest.fixture
def calls(monkeypatch):
    """Count check_dgx / pipeline_watchdog invocations, run neither for real.

    Both are patched at the MODULE the wrapper imports from — the wrapper does
    `from .health import check_dgx` INSIDE its body, so patching the attribute on
    the module at call time is what it actually binds. A patch applied to the
    wrapper's own globals would be silently ignored.
    """
    seen = {"dgx": 0, "watchdog": 0}

    def fake_check_dgx():
        seen["dgx"] += 1
        # Publish like the real check does, so the cycle can read it.
        health.publish_dgx_verdict(True, True)
        return True

    def fake_watchdog(*a, **kw):
        seen["watchdog"] += 1
        return True

    monkeypatch.setattr(health, "check_dgx", fake_check_dgx)
    monkeypatch.setattr(lifecycle, "pipeline_watchdog", fake_watchdog)
    return seen


class TestCliWatchdogSeed:
    def test_seeds_when_no_verdict_exists(self, calls):
        health.reset_dgx_verdict()
        assert cli._cli_watchdog_with_dgx() is True
        assert calls == {"dgx": 1, "watchdog": 1}

    def test_reuses_an_already_published_verdict(self, calls):
        """`check all` runs dgx first — the watchdog entry must not pay for a
        second sweep of the same thing."""
        health.publish_dgx_verdict(False, False)
        assert cli._cli_watchdog_with_dgx() is True
        assert calls["dgx"] == 0, "watchdog entry re-swept a verdict it could read"
        assert calls["watchdog"] == 1

    def test_stale_verdict_is_not_re_run_either(self, calls):
        """The seed gate is `a verdict exists`, not `a verdict is fresh`.
        Re-running on staleness would put a full sweep back on the CLI path for
        no gain: the cycle's own staleness handling (carry + WARN) is the
        intended behaviour, and the operator already got a fresh read from the
        dgx entry when `all` ran it."""
        import time
        health.publish_dgx_verdict(True, True, at=time.monotonic() - 10_000)
        assert cli._cli_watchdog_with_dgx() is True
        assert calls["dgx"] == 0
        assert calls["watchdog"] == 1

    def test_watchdog_stream_entry_is_the_wrapper(self):
        """The map entry must BE the wrapper. If it silently reverts to the bare
        `pipeline_watchdog`, `hscc check watchdog` goes back to a no-DGX-evidence
        neutral pass with no test complaining — this is the pin that stops that."""
        import inspect
        from hscc_daemon import cli as cli_mod
        src = inspect.getsource(cli_mod._cmd_check_impl)
        assert '"watchdog": _cli_watchdog_with_dgx' in src, src
        assert '"watchdog": pipeline_watchdog' not in src, src
