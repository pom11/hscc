import os
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
