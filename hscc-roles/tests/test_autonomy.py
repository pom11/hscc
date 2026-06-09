import os
import subprocess
import sys
import autonomy


def test_default_off(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    assert autonomy.is_on() is False


def test_set_on_then_off(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    autonomy.set_state("on")
    assert autonomy.is_on() is True
    autonomy.set_state("off")
    assert autonomy.is_on() is False


def test_truthy_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    for v in ("on", "1", "true", "yes", "ON", "True"):
        autonomy.set_state(v)
        assert autonomy.is_on() is True
    for v in ("off", "0", "false", "no", ""):
        autonomy.set_state(v)
        assert autonomy.is_on() is False


def test_cli_autonomy_show_on_off(tmp_path):
    plugin_dir = os.path.dirname(os.path.abspath(autonomy.__file__))
    venv_py = os.path.join(plugin_dir, "..", "..", "hermes-agent", "venv", "bin", "python")
    hscc = os.path.join(plugin_dir, "hscc.py")
    env = dict(os.environ, HOME=str(tmp_path))
    r = subprocess.run([venv_py, hscc, "autonomy"], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "off" in r.stdout.lower()
    r = subprocess.run([venv_py, hscc, "autonomy", "on"], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    r = subprocess.run([venv_py, hscc, "autonomy"], capture_output=True, text=True, env=env)
    assert "on" in r.stdout.lower()
