"""Tests for hscc_daemon.verify — each check's ok and fail paths plus run_all."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

# Canonical hscc-commands payload every synthetic-fleet test plants (both the
# repo stub and the installed plugin use the same bytes, so a content-addressed
# plugin_payload comparison passes).
_CANON_PLUGIN = ('register("workers-up")\n'
                 'register("cluster-restart")\n'
                 'register("template")\n')


def _hermetic_new_checks_overrides(tmp_path, healthy=True):
    """Hermetic overrides for the api_routes / chat_roundtrip / plugin_payload
    checks added to hscc verify.

    Without these, run_all() reaches REAL external state: api_route_sweep.py
    against the live API, a real chat round-trip to a served model, and the
    real repo + real ~/.hermes/plugins install. The three checks introduced in
    this task must therefore be pinned to tmp-only state so the run_all suite
    stays hermetic. ``healthy`` selects a passing vs a failing stub.

    Returns override keys consumed by run_all's param matching: ``script`` and
    ``python`` (api_routes), ``probe`` (chat_roundtrip), ``repo_root`` and
    ``names`` (plugin_payload).
    """
    # api_routes — point at a tiny stub script that exits 0 (healthy) or 1.
    sweep = tmp_path / "scripts" / "api_route_sweep.py"
    sweep.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_text("import sys\n" + ("sys.exit(0)\n" if healthy else "sys.exit(1)\n"))

    # chat_roundtrip — fake probe returning (or withholding) generated text.
    def _ok_probe(node, port, timeout=None, max_tokens=None):
        return {"ok": True, "text": "ok", "status": 200}

    def _bad_probe(node, port, timeout=None, max_tokens=None):
        return {"ok": False, "error": "no text", "status": None}

    # plugin_payload — hermetic repo root whose hscc-commands payload matches
    # the installed plugin bytes planted next to it.
    repo_hscc = tmp_path / "repo" / "hscc-commands"
    repo_hscc.mkdir(parents=True)
    (repo_hscc / "__init__.py").write_text(_CANON_PLUGIN)

    return {
        "script": str(sweep),
        "python": sys.executable,
        "probe": _ok_probe if healthy else _bad_probe,
        "repo_root": str(tmp_path / "repo"),
        "names": ["hscc-commands"],
    }


class TestCheckPlugins:
    """check_plugins — ok when core commands found, fail when missing, skip when unreadable."""

    def test_ok_all_commands_found(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        plugin_dir = tmp_path / "plugins" / "hscc-commands"
        plugin_dir.mkdir(parents=True)
        init = plugin_dir / "__init__.py"
        init.write_text('register("workers-up")\nregister("cluster-restart")\nregister("template")\n')
        result = check_plugins(str(tmp_path / "plugins"))
        assert result["name"] == "plugins"
        assert result["ok"] is True
        assert "all core commands found" in result["detail"]

    def test_fail_missing_commands(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        plugin_dir = tmp_path / "plugins" / "hscc-commands"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text('register("workers-up")\n')
        result = check_plugins(str(tmp_path / "plugins"))
        assert result["ok"] is False
        assert "cluster-restart" in result["detail"]
        assert "template" in result["detail"]

    def test_skip_no_plugin_dir(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        result = check_plugins(str(tmp_path / "nonexistent"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]

    def test_skip_no_init_file(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        plugin_dir = tmp_path / "plugins" / "hscc-commands"
        plugin_dir.mkdir(parents=True)
        result = check_plugins(str(tmp_path / "plugins"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]


class TestCheckMultiplex:
    """check_multiplex — ok when enabled + served, fail on gaps, skip when absent."""

    def _write_config(self, path, multiplex=True, kanban=None, toolsets=None):
        import yaml
        cfg = {"multiplex_profiles": multiplex}
        if kanban is not None:
            cfg["kanban"] = kanban
        if toolsets is not None:
            cfg["toolsets"] = toolsets
        path.write_text(yaml.dump(cfg))
        return cfg

    def test_ok_all_profiles_served(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        gw_state = tmp_path / "gateway_state.json"
        profiles = tmp_path / "profiles"

        self._write_config(config, multiplex=True)
        json.dump({"served_profiles": ["backend-engineer", "writer"]},
                  open(gw_state, "w"))

        (profiles / "backend-engineer").mkdir(parents=True)
        (profiles / "writer").mkdir(parents=True)

        result = check_multiplex(
            gateway_state=str(gw_state),
            config=str(config),
            profiles_dir=str(profiles),
        )
        assert result["name"] == "multiplex"
        assert result["ok"] is True

    def test_fail_profiles_not_served(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        gw_state = tmp_path / "gateway_state.json"
        profiles = tmp_path / "profiles"

        self._write_config(config, multiplex=True)
        json.dump({"served_profiles": ["backend-engineer"]},
                  open(gw_state, "w"))

        (profiles / "backend-engineer").mkdir(parents=True)
        (profiles / "writer").mkdir(parents=True)

        result = check_multiplex(
            gateway_state=str(gw_state),
            config=str(config),
            profiles_dir=str(profiles),
        )
        assert result["ok"] is False
        assert "writer" in result["detail"]

    def test_fail_empty_served(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        gw_state = tmp_path / "gateway_state.json"
        profiles = tmp_path / "profiles"

        self._write_config(config, multiplex=True)
        json.dump({"served_profiles": []}, open(gw_state, "w"))
        (profiles / "backend-engineer").mkdir(parents=True)

        result = check_multiplex(
            gateway_state=str(gw_state),
            config=str(config),
            profiles_dir=str(profiles),
        )
        assert result["ok"] is False
        assert "empty" in result["detail"]

    def test_skip_multiplex_disabled(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        self._write_config(config, multiplex=False)
        result = check_multiplex(config=str(config))
        assert result["ok"] is True
        assert "multiplex disabled" in result["detail"]

    def test_skip_config_missing(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        result = check_multiplex(config=str(tmp_path / "nope.yaml"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]

    def test_skip_gateway_state_missing(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        self._write_config(config, multiplex=True)
        result = check_multiplex(config=str(config), gateway_state=str(tmp_path / "no.json"))
        assert result["ok"] is None
        assert "unverified" in result["detail"]

    def test_skip_gateway_state_corrupt(self, tmp_path):
        from hscc_daemon.verify import check_multiplex
        config = tmp_path / "config.yaml"
        gw_state = tmp_path / "gateway_state.json"
        self._write_config(config, multiplex=True)
        gw_state.write_text("not-json{{{")
        result = check_multiplex(config=str(config), gateway_state=str(gw_state))
        assert result["ok"] is None
        assert "unverified" in result["detail"]


class TestCheckDaemonStreams:
    """check_daemon_streams — ok when healthy/recent, fail on stale/failed, skip on missing."""

    def test_ok_all_healthy(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        now = datetime.now(timezone.utc).isoformat()
        for name in ("dgx", "dgx-a"):
            data = {"ok": True, "timestamp": now, "last_check": now, "stream": name}
            (state_dir / f"{name}.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert result["name"] == "daemon_streams"

    def test_fail_stream_ok_false(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        now = datetime.now(timezone.utc).isoformat()
        data = {"ok": False, "timestamp": now, "last_check": now, "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "ok=False" in result["detail"]

    def test_fail_stale_stream(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        data = {"ok": True, "timestamp": old, "last_check": old, "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir), max_age_s=600)
        assert result["ok"] is False
        assert "stale" in result["detail"]

    def test_nas_stream_healthy_cadence_not_stale(self, tmp_path):
        """The NAS stream only refreshes every 900s on the live daemon
        (daemon_ops.PERIODIC_INTERVALS). A NAS state 700s old must NOT be
        flagged stale — under the old flat 600s window it was, which is the
        "cries wolf on a healthy cluster" bug (verify.py:206 max_age_s=600 <
        nas:900). The per-stream limit widens to ~2*900+90=1890s, so a healthy
        NAS is never caught between ticks."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # 700s old — well within the 900s cadence's per-stream window.
        old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        data = {"ok": True, "timestamp": old, "last_check": old, "stream": "nas"}
        (state_dir / "nas.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True, result["detail"]

    def test_nas_stream_truly_dead_still_stale(self, tmp_path):
        """A genuinely dead NAS — silent longer than ~2 full cycles (>1890s for
        the 900s cadence) — MUST still be flagged stale. Widening the window
        must not let a real failure slide."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # 2000s old — beyond even the widened 1890s NAS limit.
        old = (datetime.now(timezone.utc) - timedelta(seconds=2000)).isoformat()
        data = {"ok": True, "timestamp": old, "last_check": old, "stream": "nas"}
        (state_dir / "nas.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "nas.json: stale" in result["detail"]

    def test_fast_stream_tolerated_within_flat_window(self, tmp_path):
        """A fast stream (dgx, 60s) is NOT widened by the per-stream logic:
        its limit stays the 600s flat window, so a genuinely-dead dgx (700s
        old) still fails — fast streams stay tight."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        data = {"ok": True, "timestamp": old, "last_check": old, "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: stale" in result["detail"]

    def test_skip_missing_dir(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        result = check_daemon_streams(state_dir=str(tmp_path / "no_such_dir"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]

    def test_skip_empty_dir(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert "no state files" in result["detail"]

    def test_fail_unparseable_timestamp(self, tmp_path):
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        data = {"ok": True, "timestamp": "not-a-date", "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "unparseable timestamp" in result["detail"]


class TestCheckDaemonStreamsIntentional:
    """An intentional autodown must NOT make check_daemon_streams fail, while
    real faults still do. Each excused stream is gated on the intentional block
    being latched AND autodown state confirmed down (classify()==expected_down).

    The excuse is a two-condition AND: the stream itself carries
    ``intentional == "autodown"`` (the writer tags it is down because of the
    intentional teardown) AND an intentional autodown is confirmed in effect.
    Either missing ⇒ the ``ok: False`` is a genuine failure, exactly as before.
    """

    def _arm_intentional(self, tmp_hfcc_dir, monkeypatch, state="down"):
        """Latched intentional watchdog block + autodown config at state."""
        from hscc_daemon import lifecycle as _lc
        from hscc_daemon import autodown as _ad
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
        _lc.save_watchdog_block({
            "blocked": True, "intentional": "autodown",
            "reason": "autodown: intentional idle teardown"})
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                         "state": state, "down_since": "2026-01-01T00:00:00+00:00"})

    def _write_stream(self, state_dir, name, ok, **extra):
        from hscc_daemon.state import now_iso
        data = {"ok": ok, "timestamp": now_iso(), "last_check": now_iso(),
                "stream": name, **extra}
        (state_dir / f"{name}.json").write_text(json.dumps(data))

    def test_intentional_block_fleet_down_passes_and_names_reason(
            self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon.verify import check_daemon_streams
        self._arm_intentional(tmp_hfcc_dir, monkeypatch)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        # Every serving stream truthfully reports down WITH the intentional
        # marker — the exact healthy power-save state.
        for name in ("watchdog", "dgx", "gateway", "proxy", "workers"):
            self._write_stream(state_dir, name, False,
                               intentional="autodown",
                               message=f"intentional autodown ({name})")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        # The human output still SAYS the cluster is intentionally down.
        assert "intentionally down by autodown" in result["detail"]

    def test_intentional_block_plus_real_unrelated_failure_still_fails(
            self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon.verify import check_daemon_streams
        self._arm_intentional(tmp_hfcc_dir, monkeypatch)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        # One intentionally-down stream (excused) ...
        self._write_stream(state_dir, "watchdog", False,
                           intentional="autodown",
                           message="intentional autodown (watchdog)")
        # ... plus a REAL unrelated failure in a stream NOT tagged intentional
        # (heartbeat) => still fails, and the real fault stays visible.
        self._write_stream(state_dir, "heartbeat", False)
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "heartbeat" in result["detail"]     # real fault still visible
        assert "REAL FAILURES" in result["detail"]
        assert "intentionally down by autodown" in result["detail"]

    def test_no_intentional_block_watchdog_unhealthy_still_fails(
            self, tmp_hfcc_dir, monkeypatch):
        # Negative control: NO intentional block ⇒ an unhealthy watchdog stream
        # (even one carrying an intentional-ish message) is a REAL failure.
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        self._write_stream(state_dir, "watchdog", False)
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "watchdog.json: ok=False" in result["detail"]

    def test_intentional_stream_excused_only_when_block_confirmed_down(
            self, tmp_hfcc_dir, monkeypatch):
        """Per-stream gating: a stream carrying the intentional marker is
        excused ONLY when classify()==expected_down (block latched AND autodown
        down). With the block latched but autodown only 'up' (should_be_up —
        the layer should be coming up, not parked), the marker must NOT excuse
        it: a genuine failure while the layer should be up still fails."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        self._write_stream(state_dir, "watchdog", False,
                           intentional="autodown",
                           message="intentional autodown (watchdog)")
        # Intentional block, but autodown NOT down (state=up => should_be_up).
        self._arm_intentional(tmp_hfcc_dir, monkeypatch, state="up")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "watchdog.json: ok=False" in result["detail"]

    def test_writer_check_dgx_tags_stream_only_when_intentional_down(
            self, tmp_hfcc_dir, monkeypatch):
        """Writer-level gating: check_dgx adds the intentional marker to the
        dgx stream ONLY when an intentional autodown is confirmed down. With no
        block a failing check writes a plain ok:False (a real fault); with the
        intentional block confirmed down it tags the stream so verify excuses
        it."""
        from hscc_daemon import health
        from hscc_daemon import lifecycle as _lc
        from hscc_daemon import autodown as _ad
        monkeypatch.setattr(health, "ssh_cmd",
                            lambda *a, **k: {"ok": False, "output": ""})
        monkeypatch.setattr(health, "http_check",
                            lambda *a, **k: {"ok": False})
        monkeypatch.setattr(health, "_sparkrun_workloads", lambda: [])
        monkeypatch.setattr(health.serving, "PRIMARY_NODE", "10.0.0.2")
        monkeypatch.setattr(health.serving, "VLLM_HEALTH_URL",
                            "http://10.0.0.2/health")
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        state_dir = tmp_hfcc_dir / "state"

        # No intentional block ⇒ plain failure, NO marker (genuine fault).
        health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["ok"] is False
        assert entry.get("intentional") != "autodown"

        # Intentional block + autodown down ⇒ tagged intentional.
        _lc.save_watchdog_block({
            "blocked": True, "intentional": "autodown",
            "reason": "autodown: intentional idle teardown"})
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                         "state": "down"})
        health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["ok"] is False
        assert entry.get("intentional") == "autodown"
        assert "intentional autodown" in entry.get("message", "")


class TestCheckProxy:
    """check_proxy — ok on 200 + data, fail on errors (monkeypatched urllib)."""

    def test_ok_with_models(self):
        from hscc_daemon.verify import check_proxy
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "model1"}]}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is True
        assert "1 models" in result["detail"]

    def test_fail_connection_refused(self):
        from hscc_daemon.verify import check_proxy
        import urllib.error
        import urllib.request
        err = urllib.error.URLError("Connection refused")
        with patch.object(urllib.request, "urlopen", side_effect=err):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert "error" in result["detail"]

    def test_fail_http_error(self):
        from hscc_daemon.verify import check_proxy
        import urllib.error
        import urllib.request
        from http.client import HTTPMessage
        err = urllib.error.HTTPError(
            "http://localhost:4000/v1/models",
            503,
            "Service Unavailable",
            HTTPMessage(),
            None,
        )
        with patch.object(urllib.request, "urlopen", side_effect=err):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert "503" in result["detail"]

    def test_fail_empty_data(self):
        from hscc_daemon.verify import check_proxy
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": []}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert "no models" in result["detail"]


class TestCheckConfigWiring:
    """check_config_wiring — ok when wired, fail on missing, skip on absent."""

    def test_ok_all_wired(self, tmp_path):
        from hscc_daemon.verify import check_config_wiring
        import yaml
        config = tmp_path / "config.yaml"
        cfg = {
            "multiplex_profiles": True,
            "kanban": {"max_in_progress": 3},
            "toolsets": ["hscc-cluster", "web"],
        }
        config.write_text(yaml.dump(cfg))

        result = check_config_wiring(config=str(config))
        assert result["ok"] is True
        assert result["name"] == "config_wiring"

    def test_ok_toolsets_as_json_string(self, tmp_path):
        from hscc_daemon.verify import check_config_wiring
        import yaml
        config = tmp_path / "config.yaml"
        cfg = {
            "multiplex_profiles": True,
            "kanban": {"max_in_progress": 2},
            "toolsets": '["hscc-cluster"]',
        }
        config.write_text(yaml.dump(cfg))

        result = check_config_wiring(config=str(config))
        assert result["ok"] is True

    def test_fail_missing_items(self, tmp_path):
        from hscc_daemon.verify import check_config_wiring
        import yaml
        config = tmp_path / "config.yaml"
        cfg = {
            "multiplex_profiles": False,
            "kanban": {"max_in_progress": "high"},
            "toolsets": ["web"],
        }
        config.write_text(yaml.dump(cfg))

        result = check_config_wiring(config=str(config))
        assert result["ok"] is False
        assert "multiplex_profiles" in result["detail"]
        assert "max_in_progress" in result["detail"]
        assert "hscc-cluster" in result["detail"]

    def test_skip_config_missing(self, tmp_path):
        from hscc_daemon.verify import check_config_wiring
        result = check_config_wiring(config=str(tmp_path / "no.yaml"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]


class TestCheckProfileEndpoints:
    """Hermetic profile_endpoints guard — all expectations derived from the
    fixture serving.json/profiles, never from live ~/.hscc or ~/.hermes.

    A profile base_url must resolve to an origin the fleet actually serves:
    the orchestrator endpoint (nodes[0] of the first orchestrator unit + its
    port, from serving.json), the worker proxy (LiteLLM, default
    localhost:4000), or an allow-listed loopback host (any port).
    """

    ORCH = "10.99.99.99"
    ORCH_PORT = 8123
    ORCH_EP = f"http://{ORCH}:{ORCH_PORT}/v1"

    def _serving(self, tmp_path):
        """Fixture serving.json: one orchestrator unit on a synthetic head."""
        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": [self.ORCH],
                       "recipe": "r", "model": "m", "port": self.ORCH_PORT}],
        }))
        return serving

    def _write_profile(self, profiles_dir, name, cfg):
        pdir = profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "config.yaml").write_text(yaml.safe_dump(cfg))

    def _check(self, tmp_path, profiles_cfg, **kw):
        from hscc_daemon.verify import check_profile_endpoints
        serving = self._serving(tmp_path)
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        for name, cfg in profiles_cfg.items():
            self._write_profile(profiles_dir, name, cfg)
        return check_profile_endpoints(
            serving_path=str(serving),
            profiles_dir=str(profiles_dir),
            **kw,
        )

    def test_all_good(self, tmp_path):
        """orchestrator, worker proxy, and loopback base_urls all pass."""
        result = self._check(tmp_path, {
            "orch": {"model": {"base_url": self.ORCH_EP}},
            "worker": {"model": {"base_url": "http://localhost:4000/v1"}},
            "tooling": {"model": {"base_url": "http://127.0.0.1:9999"}},
        })
        assert result["ok"] is True
        assert "profiles served" in result["detail"]

    def test_bad_orchestrator_profile(self, tmp_path):
        """A profile aimed at a host the fleet does not serve is a finding."""
        result = self._check(tmp_path, {
            "orch": {"model": {"base_url": self.ORCH_EP}},
            "stale": {"model": {"base_url": "http://10.99.99.98:8000/v1"}},
        })
        assert result["ok"] is False
        assert "stale" in result["detail"]
        assert "model.base_url" in result["detail"]
        assert "10.99.99.98" in result["detail"]

    def test_bad_auxiliary_block(self, tmp_path):
        """A wrong endpoint nested in an auxiliary block is caught (not just
        the top-level model base_url) and reports its dotted key path."""
        result = self._check(tmp_path, {
            "orch": {"model": {"base_url": self.ORCH_EP},
                     "auxiliary": {"compression": {
                         "base_url": "http://192.0.2.5:9000/v1"}}},
        })
        assert result["ok"] is False
        assert "auxiliary.compression.base_url" in result["detail"]
        assert "192.0.2.5" in result["detail"]

    def test_compact_and_strong_blocks_scanned(self, tmp_path):
        """base_urls under compact/strong payload blocks are checked too."""
        result = self._check(tmp_path, {
            "orch": {"model": {"base_url": self.ORCH_EP}},
            "bad": {"compact": {"base_url": "http://198.51.100.7:7777"},
                    "strong": {"base_url": "http://203.0.113.9:8888"}},
        })
        assert result["ok"] is False
        assert "compact.base_url" in result["detail"]
        assert "strong.base_url" in result["detail"]

    def test_loopback_any_port_allowed(self, tmp_path):
        """Allow-listed loopback hosts pass regardless of port."""
        result = self._check(tmp_path, {
            "orch": {"model": {"base_url": self.ORCH_EP}},
            "extra_loop": {"model": {"base_url": "http://localhost:54321/v1"},
                           "cache": {"base_url": "http://127.0.0.1:1"}},
        })
        assert result["ok"] is True

    def test_missing_serving_is_unverified_not_fail(self, tmp_path):
        """Missing serving.json -> ok None (can't derive the endpoint)."""
        from hscc_daemon.verify import check_profile_endpoints
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        self._write_profile(profiles_dir, "orch",
                            {"model": {"base_url": self.ORCH_EP}})
        result = check_profile_endpoints(
            serving_path=str(tmp_path / "nope.json"),
            profiles_dir=str(profiles_dir))
        assert result["ok"] is None
        assert "unverified" in result["detail"]

    def test_profiles_dir_missing_is_skipped(self, tmp_path):
        """No profiles dir -> skipped (ok True), not a failure."""
        from hscc_daemon.verify import check_profile_endpoints
        result = check_profile_endpoints(
            serving_path=str(self._serving(tmp_path)),
            profiles_dir=str(tmp_path / "missing"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]


class TestRunAll:
    """run_all — aggregates all checks and reports overall ok."""

    def test_run_all_ok(self, tmp_path):
        from hscc_daemon.verify import run_all
        # Set up so all checks pass
        plugins_dir = tmp_path / "plugins" / "hscc-commands"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "__init__.py").write_text(
            'register("workers-up")\nregister("cluster-restart")\nregister("template")\n'
        )

        config = tmp_path / "config.yaml"
        import yaml
        cfg = {
            "multiplex_profiles": True,
            "kanban": {"max_in_progress": 3},
            "toolsets": ["hscc-cluster"],
        }
        config.write_text(yaml.dump(cfg))

        gw_state = tmp_path / "gw.json"
        json.dump({"served_profiles": ["backend-engineer"]}, open(gw_state, "w"))

        profiles = tmp_path / "profiles" / "backend-engineer"
        profiles.mkdir(parents=True)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        now = datetime.now(timezone.utc).isoformat()
        data = {"ok": True, "timestamp": now, "last_check": now, "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": ["10.0.0.1"],
                       "recipe": "r", "model": "m", "port": 8000}],
        }))

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "plugins"),
                config=str(config),
                gateway_state=str(gw_state),
                profiles_dir=str(tmp_path / "profiles"),
                state_dir=str(state_dir),
                url="http://localhost:4000/v1/models",
                serving_path=str(serving),
                **_hermetic_new_checks_overrides(tmp_path),
            )

        assert result["ok"] is True
        assert len(result["checks"]) == 9
        assert all(c["ok"] for c in result["checks"])

    def test_run_all_fail_aggregation(self, tmp_path):
        from hscc_daemon.verify import run_all
        # Point plugins_dir at nonexistent -> skipped (ok=True), but proxy fails
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": ["10.0.0.1"],
                       "recipe": "r", "model": "m", "port": 8000}],
        }))
        empty_profiles = tmp_path / "profiles"
        empty_profiles.mkdir()

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "missing"),
                config=str(tmp_path / "missing.yaml"),
                state_dir=str(tmp_path / "missing_state"),
                profiles_dir=str(empty_profiles),
                serving_path=str(serving),
                **_hermetic_new_checks_overrides(tmp_path),
            )

        assert len(result["checks"]) == 9
        # At least the proxy check passes here — overall ok depends on individual checks
        names = [c["name"] for c in result["checks"]]
        assert "plugins" in names
        assert "proxy" in names
        assert "profile_endpoints" in names
        assert "ok" in result

    def test_run_all_overrides_selective(self, tmp_path):
        from hscc_daemon.verify import run_all
        # Only override some checks; the new checks get hermetic stubs via the
        # helper so nothing reaches real network/repo/install state.
        plugins_dir = tmp_path / "plugins" / "hscc-commands"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "__init__.py").write_text(_CANON_PLUGIN)

        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": ["10.0.0.1"],
                       "recipe": "r", "model": "m", "port": 8000}],
        }))
        empty_profiles = tmp_path / "profiles"
        empty_profiles.mkdir()

        result = run_all(plugins_dir=str(tmp_path / "plugins"),
                         profiles_dir=str(empty_profiles),
                         serving_path=str(serving),
                         **_hermetic_new_checks_overrides(tmp_path))
        assert len(result["checks"]) == 9

    def test_run_all_ok_none_is_not_pass(self, tmp_path):
        """When multiplex is enabled but gateway state is missing (ok=None),
        run_all must NOT report overall ok=True."""
        from hscc_daemon.verify import run_all
        import yaml

        # Config with multiplex enabled -> triggers the gateway state path
        config = tmp_path / "config.yaml"
        cfg = {"multiplex_profiles": True}
        config.write_text(yaml.dump(cfg))

        # No gateway_state.json — will yield ok=None
        plugins_dir = tmp_path / "plugins" / "hscc-commands"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "__init__.py").write_text('register("workers-up")\nregister("cluster-restart")\nregister("template")\n')

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        now = datetime.now(timezone.utc).isoformat()
        data = {"ok": True, "timestamp": now, "last_check": now, "stream": "dgx"}
        (state_dir / "dgx.json").write_text(json.dumps(data))

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": ["10.0.0.1"],
                       "recipe": "r", "model": "m", "port": 8000}],
        }))

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "plugins"),
                config=str(config),
                gateway_state=str(tmp_path / "no_gateway.json"),
                profiles_dir=str(tmp_path / "profiles"),
                state_dir=str(state_dir),
                url="http://localhost:4000/v1/models",
                serving_path=str(serving),
                **_hermetic_new_checks_overrides(tmp_path),
            )

        assert result["ok"] is False
        multiplex_check = [c for c in result["checks"] if c["name"] == "multiplex"][0]
        assert multiplex_check["ok"] is None


class TestRunAllFullVerifyIntentionalAutodown:
    """FULL `hscc verify` check-set round trip against a simulated
    intentional-down fleet. This is the end-to-end prove-it test the previous
    two attempts lacked: it runs the ENTIRE run_all() check list (not one
    side of a reader/writer pair) against real state files in a tmp dir with
    the intentional block set, and asserts the OVERALL result is PASS. The
    negative control reuses the IDENTICAL state with the intentional block
    removed and asserts overall FAIL.

    run_all() forwards keyed overrides to each check's matching parameter, so
    the whole synthetic fleet surface is built through overrides (plugins
    dir, config, gateway state, profiles, daemon state dir, proxy url) while
    the intentional block is armed via the REAL watchdog-block.json /
    autodown.json in the tmp dir — no live ~/.hscc or ~/.hermes is touched.
    """

    def _arm_intentional(self, tmp_hfcc_dir, monkeypatch, state="down"):
        """Latched intentional watchdog block + autodown config at ``state``
        (default "down"), exactly as TestCheckDaemonStreamsIntentional does.
        Pass ``state="waking"`` to simulate the wake window (block latched,
        models still loading)."""
        from hscc_daemon import lifecycle as _lc
        from hscc_daemon import autodown as _ad
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
        _lc.save_watchdog_block({
            "blocked": True, "intentional": "autodown",
            "reason": "autodown: intentional idle teardown"})
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                         "state": state, "down_since": "2026-01-01T00:00:00+00:00"})

    def _build_fleet(self, tmp_path):
        """Plant the whole synthetic fleet surface; return run_all overrides.

        The state dir holds the serving streams truthfully down WITH the
        intentional marker (the exact healthy power-save state); the proxy is
        patched to report no models (it is part of the torn-down serving
        layer); config/gateway/plugins are a healthy, fully-wired install.
        """
        import yaml

        from hscc_daemon.state import now_iso

        # plugins — fully wired.
        plugins_dir = tmp_path / "plugins" / "hscc-commands"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "__init__.py").write_text(
            'register("workers-up")\nregister("cluster-restart")\n'
            'register("template")\n')

        # config — full HSCC wiring.
        config = tmp_path / "config.yaml"
        config.write_text(yaml.dump({
            "multiplex_profiles": True,
            "kanban": {"max_in_progress": 3},
            "toolsets": ["hscc-cluster"],
        }))

        # gateway state serving every profile dir, and the profile dirs.
        gw_state = tmp_path / "gateway_state.json"
        profiles = tmp_path / "profiles"
        (profiles / "backend-engineer").mkdir(parents=True)
        (profiles / "writer").mkdir(parents=True)
        json.dump({"served_profiles": ["backend-engineer", "writer"]},
                  open(gw_state, "w"))

        # daemon state — every serving stream truthfully down WITH the
        # intentional marker, fresh timestamps (healthy power-save).
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        for name in ("watchdog", "dgx", "gateway", "proxy", "workers"):
            data = {"ok": False, "timestamp": now_iso(), "last_check": now_iso(),
                    "stream": name, "intentional": "autodown",
                    "message": f"intentional autodown ({name})"}
            (state_dir / f"{name}.json").write_text(json.dumps(data))

        # serving.json fixture (hermetic — no live ~/.hscc is read) with an
        # orchestrator unit so the profile_endpoints check can derive a
        # served endpoint; the profiles below carry no base_urls, so the
        # check is vacuously-pass/ok=True.
        serving = tmp_path / "serving.json"
        serving.write_text(json.dumps({
            "units": [{"role": "orchestrator", "nodes": ["10.0.0.1"],
                       "recipe": "r", "model": "m", "port": 8000}],
        }))

        return {
            "plugins_dir": str(tmp_path / "plugins"),
            "config": str(config),
            "gateway_state": str(gw_state),
            "profiles_dir": str(profiles),
            "state_dir": str(state_dir),
            "url": "http://localhost:4000/v1/models",
            "serving_path": str(serving),
            **_hermetic_new_checks_overrides(tmp_path),
        }

    def _proxy_no_models(self):
        """Patch the proxy to list NO models (fleet torn down)."""
        import urllib.request
        from unittest.mock import patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": []}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None
        return patch("hscc_daemon.verify.urllib.request.urlopen",
                     return_value=mock_resp)

    def test_full_verify_round_trip_passes_intentional(self, tmp_path,
                                                       tmp_hfcc_dir,
                                                       monkeypatch):
        """Intentional-down fleet + intentional block ⇒ OVERALL PASS."""
        from hscc_daemon.verify import run_all
        self._arm_intentional(tmp_hfcc_dir, monkeypatch)
        overrides = self._build_fleet(tmp_path)

        with self._proxy_no_models():
            result = run_all(**overrides)

        assert result["ok"] is True, (
            "full verify must PASS during an intentional autodown; got: "
            + repr(result["checks"]))
        by_name = {c["name"]: c for c in result["checks"]}
        # The excused proxy explicitly says WHY it is not failing.
        assert "intentionally down by autodown" in by_name["proxy"]["detail"]
        assert by_name["proxy"]["ok"] is True
        # daemon_streams keeps its intent-labelling (not regressed).
        assert "intentionally down by autodown" in \
            by_name["daemon_streams"]["detail"]

    def test_full_verify_round_trip_fails_without_intentional_block(
            self, tmp_path, tmp_hfcc_dir, monkeypatch):
        """Negative control: SAME fleet state (streams tagged intentional,
        proxy with no models) but NO intentional block ⇒ OVERALL FAIL.

        The only difference from the passing test is that the watchdog block
        / autodown config are not armed, so classify() is 'healthy' — the
        intentional markers and the empty proxy are then genuine failures.
        """
        from hscc_daemon.verify import run_all
        # NOTE: intentionally do NOT call _arm_intentional.
        overrides = self._build_fleet(tmp_path)

        with self._proxy_no_models():
            result = run_all(**overrides)

        assert result["ok"] is False, (
            "full verify must FAIL with no intentional block; got: "
            + repr(result["checks"]))
        by_name = {c["name"]: c for c in result["checks"]}
        assert by_name["proxy"]["ok"] is False
        assert "no models" in by_name["proxy"]["detail"]
        assert by_name["daemon_streams"]["ok"] is False

    def test_full_verify_round_trip_passes_waking(self, tmp_path,
                                                  tmp_hfcc_dir, monkeypatch):
        """Wake window (+ intentional block) ⇒ OVERALL PASS.

        The serving layer is coming up (models still loading), so every
        serving stream legitimately reports unhealthy WITH the intentional
        marker and the proxy lists no models — the exact healthy in-wake
        state. The gate must now excuse this window, and the wording must say
        "waking" (coming back), not "intentionally down" (off on purpose).
        """
        from hscc_daemon.verify import run_all
        self._arm_intentional(tmp_hfcc_dir, monkeypatch, state="waking")
        overrides = self._build_fleet(tmp_path)

        with self._proxy_no_models():
            result = run_all(**overrides)

        assert result["ok"] is True, (
            "full verify must PASS during the waking window; got: "
            + repr(result["checks"]))
        by_name = {c["name"]: c for c in result["checks"]}
        # The proxy explicitly says it is waking, not off by design.
        assert "waking from autodown" in by_name["proxy"]["detail"]
        assert by_name["proxy"]["ok"] is True
        # daemon_streams names the waking window and excused the streams.
        assert "waking from autodown" in \
            by_name["daemon_streams"]["detail"]
        assert by_name["daemon_streams"]["ok"] is True

    def test_full_verify_waking_without_intentional_block_fails(
            self, tmp_path, tmp_hfcc_dir, monkeypatch):
        """Negative control: same in-wake fleet state but NO intentional block
        ⇒ OVERALL FAIL. With autodown state waking but no latched block the
        intentional markers and empty proxy are genuine failures (the layer
        should be up — nothing excuses it)."""
        from hscc_daemon.verify import run_all
        # Enable autodown at waking WITHOUT a block: NOT armed via
        # _arm_intentional, so classify() stays 'healthy' and nothing is
        # excused — even though the streams carry the intentional marker.
        from hscc_daemon import autodown as _ad
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                         "state": "waking",
                         "down_since": "2026-01-01T00:00:00+00:00"})
        overrides = self._build_fleet(tmp_path)

        with self._proxy_no_models():
            result = run_all(**overrides)

        assert result["ok"] is False, (
            "full verify must FAIL waking with no intentional block; got: "
            + repr(result["checks"]))
        by_name = {c["name"]: c for c in result["checks"]}
        assert by_name["proxy"]["ok"] is False
        assert by_name["daemon_streams"]["ok"] is False

    def test_full_verify_real_fault_during_waking_fails(
            self, tmp_path, tmp_hfcc_dir, monkeypatch):
        """Negative control: waking with the intentional block armed, PLUS an
        UNRELATED real fault (a non-serving stream untagged, e.g. heartbeat)
        ⇒ OVERALL FAIL.

        The serving streams are excused (they are down because of the wake),
        but the unrelated stream is NOT carrying the intentional marker and is
        NOT excused — a genuine fault during a wake must still surface.
        """
        from hscc_daemon.verify import run_all
        from hscc_daemon.state import now_iso
        self._arm_intentional(tmp_hfcc_dir, monkeypatch, state="waking")
        overrides = self._build_fleet(tmp_path)

        # Inject an unrelated real fault: a non-serving stream, ok=False, NO
        # intentional marker (heartbeat is not part of the serving layer).
        state_dir = tmp_path / "state"
        (state_dir / "heartbeat.json").write_text(json.dumps({
            "ok": False, "timestamp": now_iso(), "last_check": now_iso(),
            "stream": "heartbeat"}))

        with self._proxy_no_models():
            result = run_all(**overrides)

        assert result["ok"] is False, (
            "a real fault during waking must still FAIL; got: "
            + repr(result["checks"]))
        by_name = {c["name"]: c for c in result["checks"]}
        assert by_name["daemon_streams"]["ok"] is False
        assert "heartbeat" in by_name["daemon_streams"]["detail"]


class TestCheckApiRoutes:
    """check_api_routes — runs scripts/api_route_sweep.py and interprets exits."""

    def _script(self, tmp_path, body):
        s = tmp_path / "scripts" / "api_route_sweep.py"
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(body)
        return str(s)

    def test_ok_exit_zero(self, tmp_path):
        from hscc_daemon.verify import check_api_routes
        r = check_api_routes(script=self._script(tmp_path, "import sys; sys.exit(0)\n"),
                             python=sys.executable)
        assert r["ok"] is True

    def test_fail_exit_one(self, tmp_path):
        from hscc_daemon.verify import check_api_routes
        # The real script emits --json on exit 1; the check must parse it to
        # name the failing route + status as the cause.
        script = self._script(
            tmp_path,
            "import json; print(json.dumps({'routes': ["
            "{'route': '/v1/chat', 'status': 404, 'ok': False}]})); "
            "import sys; sys.exit(1)\n")
        r = check_api_routes(script=script, python=sys.executable)
        assert r["ok"] is False
        assert "/v1/chat (404)" in r["detail"]
        assert r["next_step"]

    def test_fail_exit_two_no_api(self, tmp_path):
        from hscc_daemon.verify import check_api_routes
        script = self._script(
            tmp_path, "import sys; print('no HSCC_API_HOST'); sys.exit(2)\n")
        r = check_api_routes(script=script, python=sys.executable)
        assert r["ok"] is False
        assert "API not reachable" in r["detail"]

    def test_unverified_script_missing(self, tmp_path):
        from hscc_daemon.verify import check_api_routes
        r = check_api_routes(script=str(tmp_path / "nope.py"), python=sys.executable)
        assert r["ok"] is None
        assert "unverified" in r["detail"]


class TestCheckChatRoundtrip:
    """check_chat_roundtrip — real chat round-trip via the engine-wedge probe."""

    def _serving(self, tmp_path, units):
        p = tmp_path / "serving.json"
        p.write_text(json.dumps({"units": units}))
        return str(p)

    def test_ok_all_units_answer(self, tmp_path, monkeypatch):
        from hscc_daemon.verify import check_chat_roundtrip

        def probe(node, port, timeout=None, max_tokens=None):
            return {"ok": True, "text": "ok", "status": 200}

        monkeypatch.setattr("hscc_daemon.verify._intentional_window_verdict",
                            lambda: None)
        serving = self._serving(tmp_path, [{"role": "orchestrator",
                                            "nodes": ["10.0.0.1"], "port": 8000}])
        r = check_chat_roundtrip(serving_path=serving, probe=probe)
        assert r["ok"] is True
        assert "1 serving unit" in r["detail"]

    def test_fail_unit_does_not_answer(self, tmp_path, monkeypatch):
        from hscc_daemon.verify import check_chat_roundtrip

        def probe(node, port, timeout=None, max_tokens=None):
            return {"ok": False, "error": "no text", "status": None}

        monkeypatch.setattr("hscc_daemon.verify._intentional_window_verdict",
                            lambda: None)
        serving = self._serving(tmp_path, [{"role": "orchestrator",
                                            "nodes": ["10.0.0.1"], "port": 8000}])
        r = check_chat_roundtrip(serving_path=serving, probe=probe)
        assert r["ok"] is False
        assert "did not answer" in r["detail"]
        assert r["next_step"]

    def test_no_serving_file_unverified(self, tmp_path):
        from hscc_daemon.verify import check_chat_roundtrip

        def probe(node, port, timeout=None, max_tokens=None):
            return {"ok": True, "text": "ok"}

        r = check_chat_roundtrip(serving_path=str(tmp_path / "missing.json"),
                                 probe=probe)
        assert r["ok"] is None
        assert "unverified" in r["detail"]


class TestCheckPluginPayload:
    """check_plugin_payload — installed payload must match what the repo deploys."""

    def _tree(self, root, plugin, files):
        base = root / plugin
        base.mkdir(parents=True)
        for rel, content in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return str(root)

    def test_ok_installed_matches_repo(self, tmp_path):
        from hscc_daemon.verify import check_plugin_payload
        repo = self._tree(tmp_path / "repo", "hscc-commands",
                          {"__init__.py": "register('x')\n"})
        inst = self._tree(tmp_path / "plugins", "hscc-commands",
                          {"__init__.py": "register('x')\n"})
        r = check_plugin_payload(repo_root=repo, plugins_dir=inst,
                                 names=["hscc-commands"])
        assert r["ok"] is True

    def test_fail_repo_not_deployed(self, tmp_path):
        from hscc_daemon.verify import check_plugin_payload
        repo = self._tree(tmp_path / "repo", "hscc-roles",
                          {"__init__.py": "register('x')\n"})
        inst_dir = str(tmp_path / "plugins")  # empty — nothing installed
        r = check_plugin_payload(repo_root=repo, plugins_dir=inst_dir,
                                 names=["hscc-roles"])
        assert r["ok"] is False
        assert "merged but not deployed" in r["detail"]
        assert r["next_step"]

    def test_fail_installed_differs_from_repo(self, tmp_path):
        from hscc_daemon.verify import check_plugin_payload
        repo = self._tree(tmp_path / "repo", "hscc-commands",
                          {"__init__.py": "register('newcmd')\n"})
        inst = self._tree(tmp_path / "plugins", "hscc-commands",
                          {"__init__.py": "register('oldcmd')\n"})
        r = check_plugin_payload(repo_root=repo, plugins_dir=inst,
                                 names=["hscc-commands"])
        assert r["ok"] is False
        assert "__init__.py" in r["detail"]

    def test_fail_test_files_ignored(self, tmp_path):
        """tests/ and __pycache__ are NOT payload — they must not cause a diff."""
        from hscc_daemon.verify import check_plugin_payload
        repo = self._tree(
            tmp_path / "repo", "hscc-commands",
            {"__init__.py": "register('x')\n",
             "tests/test_cmd.py": "def test():\n    pass\n",
             "__pycache__/hscc.cpython.pyc": "junk"})
        inst = self._tree(
            tmp_path / "plugins", "hscc-commands",
            {"__init__.py": "register('x')\n"})
        r = check_plugin_payload(repo_root=repo, plugins_dir=inst,
                                 names=["hscc-commands"])
        assert r["ok"] is True

