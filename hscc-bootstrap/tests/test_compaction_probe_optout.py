"""The compaction-probe opt-out (HSCC_SKIP_COMPACT_PROBE) — t_ecb84764.

Pins three things, each of which a later "cleanup" could silently break:

  1. the opt-out is OFF by default, so bootstrap on a real machine still probes;
  2. when it is ON, the probe performs NO network call at all (the whole point:
     66 enable() cases x 5.04 s of dead connect-timeout was 48 % of the
     hscc-bootstrap leg) and still reports "nothing to warn about";
  3. when it is OFF the probe really does reach urlopen — otherwise 1 and 2
     would pass equally well against a probe that had been deleted.
"""
import io
import json
import urllib.request

import pytest

import enable_plugins


# ── 1. the knob itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    (" on ", True),
    (None, False), ("", False), ("0", False), ("false", False),
    ("no", False), ("probe", False),   # unrecognised => keep the operator default
])
def test_optout_is_opt_in_and_case_insensitive(value, expected):
    env = {} if value is None else {"HSCC_SKIP_COMPACT_PROBE": value}
    assert enable_plugins._skip_compaction_probe(env) is expected


def test_optout_defaults_off_with_live_environment(monkeypatch):
    """No inherited environment value may flip the operator's probe off."""
    monkeypatch.delenv("HSCC_SKIP_COMPACT_PROBE", raising=False)
    assert enable_plugins._skip_compaction_probe() is False


# ── 2. opted out => no socket is opened ──────────────────────────────────────

def test_optout_makes_no_network_call(monkeypatch):
    calls = []

    def _never(*a, **kw):
        calls.append(a)
        raise AssertionError("opted-out probe must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", _never)
    monkeypatch.setenv("HSCC_SKIP_COMPACT_PROBE", "1")
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "http://192.0.2.1:9/v1")

    assert enable_plugins._probe_compaction_endpoint() is True
    assert calls == []


# ── 3. not opted out => the probe still runs ─────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def test_probe_still_runs_when_not_opted_out(monkeypatch):
    """The knob off => the probe runs, and the seam that runs it is the live one.

    Since t_8bf34130 the probe also has an in-suite FENCE in front of its default
    transport, so "not opted out" alone no longer reaches the network inside a
    pytest process — that is the point of the fence, and a child that forgot the
    opt-out used to dial the operator's orch and get a free "model present" verdict.
    So this is split into the two facts that used to be conflated here:

      * an injected transport is honoured, once, with the probe's own URL — this is
        what a rig uses;
      * the DEFAULT transport is a real ``urlopen`` with the 5 s budget the opt-out
        exists to avoid, which is what makes points 1 and 2 of the module docstring
        meaningful (a deleted probe would still pass them). Tested by calling the
        default transport directly: it is the live seam, and it is deliberately NOT
        behind the fence.
    """
    seen = []

    def _fake(models_url):
        seen.append(models_url)
        return {"data": [{"id": "orchestrator-model"}]}

    monkeypatch.delenv("HSCC_SKIP_COMPACT_PROBE", raising=False)
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "http://192.0.2.1:9/v1")
    monkeypatch.setattr(enable_plugins, "COMPACT_MODEL", "orchestrator-model")

    assert enable_plugins._probe_compaction_endpoint(_fetch=_fake) is True
    assert seen == ["http://192.0.2.1:9/v1/models"]


def test_default_transport_reaches_urlopen_with_the_5s_budget(monkeypatch):
    """The live seam itself: one Request, ``timeout=5``, the /v1/models form."""
    seen = []

    def _fake(req, timeout=None):
        seen.append((getattr(req, "full_url", str(req)), timeout))
        return _Resp(json.dumps({"data": [{"id": "orchestrator-model"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    data = enable_plugins._compact_probe_default_fetch("http://192.0.2.1:9/v1/models")
    assert data == {"data": [{"id": "orchestrator-model"}]}
    # one call, at the /v1/models form, and it still carries the 5 s budget the
    # opt-out exists to avoid — the timeout is the thing that cost the leg.
    assert seen == [("http://192.0.2.1:9/v1/models", 5)]


def test_enable_skips_the_probe_when_opted_out(monkeypatch, tmp_path):
    """The measured case: enable() is the function the suite calls ~66 times.

    With the knob on, enable() must reach the same result with zero network
    calls. Guarded by a urlopen that raises, so a regression that re-adds the
    probe (or a knob that stopped being read) fails here rather than just being
    5 s slower.
    """
    def _never(*a, **kw):
        raise AssertionError("enable() probed the network with the opt-out set")

    monkeypatch.setattr(urllib.request, "urlopen", _never)
    monkeypatch.setenv("HSCC_SKIP_COMPACT_PROBE", "1")
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "http://192.0.2.1:9/v1")
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(tmp_path / "hooks"))

    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  enabled: []\ntoolsets: []\n")
    res = enable_plugins.enable(str(cfg))
    assert res["plugins"], "enable() must still report what it wired"
