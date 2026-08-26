"""Tests for hscc_daemon.verify — each check's ok and fail paths plus run_all."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


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

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "plugins"),
                config=str(config),
                gateway_state=str(gw_state),
                profiles_dir=str(tmp_path / "profiles"),
                state_dir=str(state_dir),
                url="http://localhost:4000/v1/models",
            )

        assert result["ok"] is True
        assert len(result["checks"]) == 5
        assert all(c["ok"] for c in result["checks"])

    def test_run_all_fail_aggregation(self, tmp_path):
        from hscc_daemon.verify import run_all
        # Point plugins_dir at nonexistent -> skipped (ok=True), but proxy fails
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "missing"),
                config=str(tmp_path / "missing.yaml"),
                state_dir=str(tmp_path / "missing_state"),
            )

        assert len(result["checks"]) == 5
        # At least the proxy check passes here — overall ok depends on individual checks
        names = [c["name"] for c in result["checks"]]
        assert "plugins" in names
        assert "proxy" in names
        assert "ok" in result

    def test_run_all_overrides_selective(self, tmp_path):
        from hscc_daemon.verify import run_all
        # Only override plugins_dir; others use defaults (which skip)
        plugins_dir = tmp_path / "plugins" / "hscc-commands"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "__init__.py").write_text('register("workers-up")\nregister("cluster-restart")\nregister("template")\n')

        result = run_all(plugins_dir=str(tmp_path / "plugins"))
        assert len(result["checks"]) == 5

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

        with patch("hscc_daemon.verify.urllib.request.urlopen", return_value=mock_resp):
            result = run_all(
                plugins_dir=str(tmp_path / "plugins"),
                config=str(config),
                gateway_state=str(tmp_path / "no_gateway.json"),
                profiles_dir=str(tmp_path / "profiles"),
                state_dir=str(state_dir),
                url="http://localhost:4000/v1/models",
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

        return {
            "plugins_dir": str(tmp_path / "plugins"),
            "config": str(config),
            "gateway_state": str(gw_state),
            "profiles_dir": str(tmp_path / "profiles"),
            "state_dir": str(state_dir),
            "url": "http://localhost:4000/v1/models",
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
