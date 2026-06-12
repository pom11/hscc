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


def test_legacy_hardcoded_ip_header_is_stripped(tmp_path):
    """M4: a SOUL whose first line hardcodes cluster IPs must have that line
    replaced by the topology-free HSCC identity, preserving user sections."""
    p = tmp_path / "SOUL.md"
    p.write_text(
        "You are Hermes, orchestrator (gateway 10.0.0.244; workers "
        ".246/.247/.248; NAS .249).\n\n## Brevity\nbe terse.\n")
    IS.install_soul(str(p))
    text = p.read_text()
    assert "10.0.0.244" not in text          # legacy IP line gone
    assert "## Brevity" in text and "be terse." in text  # user section kept
    assert "HSCC" in text and IS.HEAD_BEGIN in text


def test_identity_is_hscc_and_topology_free(tmp_path):
    import re
    p = str(tmp_path / "SOUL.md")
    IS.install_soul(p)
    text = open(p).read()
    assert "HSCC" in text
    assert IS.HEAD_BEGIN in text and IS.HEAD_END in text
    assert "~/dev/" in text                        # working-dir discipline (D6)
    assert "discovery_status" in text              # discovery referenced
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)  # no IPs


def test_replaces_in_place_preserving_surroundings(tmp_path):
    # The first paragraph is the (managed) identity preamble — it gets replaced.
    # User SECTIONS (after a blank line) and the footer are preserved.
    p = tmp_path / "SOUL.md"
    p.write_text(
        f"Old identity line.\n\n## User section\nkeep me.\n\n"
        f"{IS.BEGIN}\nOLD STALE BLOCK\n{IS.END}\n\nFOOTER stays.\n")
    assert IS.install_soul(str(p)) == "replaced"
    text = p.read_text()
    assert "## User section" in text and "keep me." in text     # user content kept
    assert "FOOTER stays." in text
    assert "Old identity line." not in text                     # preamble replaced
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
    # First paragraph is the managed preamble (replaced); a user SECTION below is
    # preserved.
    path = _cfg(tmp_path / "config.yaml",
                {"personalities": {"ops":
                 f"Old persona line.\n\n## My rules\nkeep me.\n\n{IS.BEGIN}\nOLD\n{IS.END}\n"}})
    assert IS.install_personality(path) == "replaced"
    ops = yaml.safe_load(open(path))["personalities"]["ops"]
    assert "## My rules" in ops and "keep me." in ops and "OLD" not in ops
    assert "Old persona line." not in ops
    assert "sparkrun_exec" in ops


def test_personality_idempotent(tmp_path):
    path = _cfg(tmp_path / "config.yaml", {"personalities": {}})
    IS.install_personality(path)
    first = open(path).read()
    assert IS.install_personality(path) == "unchanged"
    assert open(path).read() == first


def test_personality_missing_config_noop(tmp_path):
    assert IS.install_personality(str(tmp_path / "nope.yaml")) == "no-config"


def test_personality_legacy_ip_header_stripped(tmp_path):
    """M4: an ops persona whose preamble hardcodes IPs gets it replaced by the
    topology-free HSCC header, preserving the rest of the user's prose."""
    import re
    path = _cfg(tmp_path / "config.yaml", {"personalities": {"ops":
                "You are Hermes (gateway 10.0.0.244, workers .246/.247/.248).\n\n"
                "## My rules\nbe terse.\n"}})
    IS.install_personality(path)
    ops = yaml.safe_load(open(path))["personalities"]["ops"]
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", ops)
    assert "HSCC" in ops and "## My rules" in ops and "be terse." in ops


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
