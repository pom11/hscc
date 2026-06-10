import os

import yaml

import install_soul as IS


# ── install_soul ─────────────────────────────────────────────────────────────

def test_creates_when_absent(tmp_path):
    p = str(tmp_path / "SOUL.md")
    assert IS.install_soul(p) == "created"
    text = open(p).read()
    assert IS.BEGIN in text and IS.END in text
    assert "sparkrun_exec" in text and "/orch-restart" in text


def test_appends_when_no_sentinels(tmp_path):
    p = tmp_path / "SOUL.md"
    p.write_text("You are Hermes.\n\n## My own section\nkeep me.\n")
    assert IS.install_soul(str(p)) == "appended"
    text = p.read_text()
    assert "## My own section" in text and "keep me." in text   # preserved
    assert IS.BEGIN in text


def test_replaces_in_place_preserving_surroundings(tmp_path):
    p = tmp_path / "SOUL.md"
    p.write_text(
        f"HEADER stays.\n\n{IS.BEGIN}\nOLD STALE BLOCK\n{IS.END}\n\nFOOTER stays.\n")
    assert IS.install_soul(str(p)) == "replaced"
    text = p.read_text()
    assert "HEADER stays." in text and "FOOTER stays." in text
    assert "OLD STALE BLOCK" not in text
    assert "sparkrun_exec" in text
    assert text.count(IS.BEGIN) == 1                            # exactly one block


def test_idempotent_no_rewrite(tmp_path):
    p = str(tmp_path / "SOUL.md")
    IS.install_soul(p)
    first = open(p).read()
    assert IS.install_soul(p) == "unchanged"
    assert open(p).read() == first


def test_backup_made_on_change(tmp_path):
    p = tmp_path / "SOUL.md"
    p.write_text("existing soul, no sentinels.\n")
    IS.install_soul(str(p))
    baks = list(tmp_path.glob("SOUL.md.bak-*"))
    assert len(baks) == 1
    assert baks[0].read_text() == "existing soul, no sentinels.\n"


# ── install_personality ──────────────────────────────────────────────────────

def _cfg(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_personality_seeded_when_absent(tmp_path):
    path = _cfg(tmp_path / "config.yaml", {"model": {"default": "X"}})
    assert IS.install_personality(path) == "seeded"
    cfg = yaml.safe_load(open(path))
    ops = cfg["personalities"]["ops"]
    assert IS.BEGIN in ops and "sparkrun_exec" in ops
    assert cfg["model"]["default"] == "X"                       # preserved
    assert "personality" not in cfg.get("display", {})         # display untouched


def test_personality_block_updated_preserving_prose(tmp_path):
    path = _cfg(tmp_path / "config.yaml",
                {"personalities": {"ops":
                 f"My ops persona prose.\n\n{IS.BEGIN}\nOLD\n{IS.END}\n"}})
    assert IS.install_personality(path) == "replaced"
    ops = yaml.safe_load(open(path))["personalities"]["ops"]
    assert "My ops persona prose." in ops and "OLD" not in ops
    assert "sparkrun_exec" in ops


def test_personality_idempotent(tmp_path):
    path = _cfg(tmp_path / "config.yaml", {"personalities": {}})
    IS.install_personality(path)
    first = open(path).read()
    assert IS.install_personality(path) == "unchanged"
    assert open(path).read() == first


def test_personality_missing_config_noop(tmp_path):
    assert IS.install_personality(str(tmp_path / "nope.yaml")) == "no-config"


# ── shared block integrity ───────────────────────────────────────────────────

def test_topology_free_no_hardcoded_ips():
    import re
    assert not re.search(r"\b192\.168\.\d", IS.HSCC_SOUL_BLOCK)
    assert not re.search(r"\b10\.\d+\.\d+\.\d+\b", IS.HSCC_SOUL_BLOCK)


def test_soul_and_personality_share_one_block(tmp_path):
    sp = str(tmp_path / "SOUL.md")
    cp = _cfg(tmp_path / "config.yaml", {})
    IS.install_soul(sp)
    IS.install_personality(cp)
    soul_block = open(sp).read().split(IS.BEGIN, 1)[1].split(IS.END, 1)[0]
    ops = yaml.safe_load(open(cp))["personalities"]["ops"]
    pers_block = ops.split(IS.BEGIN, 1)[1].split(IS.END, 1)[0]
    assert soul_block == pers_block
