"""Tests for the payload-drift warning on `hscc api start`.

Scope: the drift check itself (verify.check_plugin_payload) already has full
unit coverage in test_verify.py. These tests cover ONLY the NEW wiring — that
``_warn_payload_drift`` turns ok=None (no repo checkout) into silence, ok=True
(match) into silence, and ok=False (drift) into a LOUD warning naming the
drifted plugin and the remedy. We inject temp repo/plugins dirs, so no live
~/.hermes/plugins state is touched.
"""


class TestWarnPayloadDrift:
    def _tree(self, root, plugin, files):
        base = root / plugin
        base.mkdir(parents=True)
        for rel, content in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root

    def _make_repo_with_payload(self, tmp_path):
        """A repo root whose bootstrap declares one deployable plugin."""
        repo = tmp_path / "repo"
        boot = repo / "hscc-bootstrap"
        boot.mkdir(parents=True)
        (boot / "install_payload.py").write_text(
            "DEFAULT_PAYLOAD = ['hscc_daemon']\n")
        self._tree(repo, "hscc_daemon", {"__init__.py": "x = 1\n"})
        return repo

    def test_drift_produces_loud_warning(self, tmp_path, capsys):
        """ok=False (installed payload differs) must print a LOUD warning."""
        from hscc_daemon.api_cli import _warn_payload_drift

        repo = self._make_repo_with_payload(tmp_path)
        # Installed counterpart has DIFFERENT content -> drift.
        plugins = self._tree(tmp_path / "plugins", "hscc_daemon",
                             {"__init__.py": "x = 999\n"})

        _warn_payload_drift(repo_root=str(repo), plugins_dir=str(plugins))
        out = capsys.readouterr().out

        assert "WARNING" in out
        assert "INSTALLED payload differs" in out
        assert "hscc_daemon" in out        # names the drifted plugin
        assert "install_payload.py" in out  # gives the exact remedy

    def test_ok_None_is_silent(self, tmp_path, capsys, monkeypatch):
        """ok=None (no repo to diff against) must print NOTHING, start normally."""
        from hscc_daemon import verify
        from hscc_daemon.api_cli import _warn_payload_drift

        # Simulate a real user install: no repo checkout -> ok=None.
        monkeypatch.setattr(
            verify, "check_plugin_payload",
            lambda **kw: {"name": "plugin_payload", "ok": None,
                          "detail": "unverified: no repo"})

        _warn_payload_drift(repo_root=str(tmp_path / "nope"),
                            plugins_dir=str(tmp_path / "plugins"))
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_ok_true_is_silent(self, tmp_path, capsys):
        """ok=True (installed payload matches) must print NOTHING."""
        from hscc_daemon.api_cli import _warn_payload_drift

        repo = self._make_repo_with_payload(tmp_path)
        plugins = self._tree(tmp_path / "plugins", "hscc_daemon",
                             {"__init__.py": "x = 1\n"})

        _warn_payload_drift(repo_root=str(repo), plugins_dir=str(plugins))
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_check_exception_does_not_block_startup(self, tmp_path, capsys,
                                                    monkeypatch):
        """A crash in the check must never block startup — warn, keep going."""
        from hscc_daemon import verify
        from hscc_daemon.api_cli import _warn_payload_drift

        def _boom(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(verify, "check_plugin_payload", _boom)
        # Should not raise, should print a warning to stderr and return.
        _warn_payload_drift(repo_root=str(tmp_path / "nope"),
                            plugins_dir=str(tmp_path / "plugins"))
        err = capsys.readouterr().err
        assert "payload-drift check failed" in err
