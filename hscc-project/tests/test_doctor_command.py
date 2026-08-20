"""Tests for the `flightdeck doctor` command.

doctor's job is honesty: report an unverifiable project *distinctly* from a
healthy one, and exit non-zero the moment anything cannot be read. No test
touches a live repo, board, or the network — every external surface (git,
kanban board list, telegram client) is injected.
"""

import argparse
import json
import os
import plistlib
from pathlib import Path

import pytest

from flightdeck.commands import doctor as cmd
from flightdeck.core.registry import Project
from flightdeck.core.telegram import Topic


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "run": None,
        "client": None,
        "boards": None,
        "topics": None,
        "repo_check": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(name="hscc", repo="/tmp/hscc", board="hscc", topic=140):
    return Project(name=name, repo=repo, board=board, topic=topic)


def _repo_ok_fake(ok=True, detail="repo ok"):
    def check(proj, **_):
        return {"ok": ok, "detail": detail}
    return check


def _healthy_world(repo="/tmp/hscc", board="hscc", topic=140, repo_ok=True):
    """projects + injectable snapshots describing a fully healthy world.

    The repo check is injected so no test needs a real git checkout on disk.
    """
    projects = [_project(repo=repo, board=board, topic=topic)]
    snap = {
        "run": None,
        "boards": [board],
        "topics": [Topic(id=topic, name="HSCC cluster")],
        "repo_check": _repo_ok_fake(repo_ok),
    }
    return projects, snap


def test_healthy_project_reports_ok(monkeypatch, capsys):
    projects, snap = _healthy_world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr()
    assert "[ok]" in out.out
    assert "[PROBLEM]" not in out.out
    assert "all clear" in out.err


def test_unreadable_project_reported_distinctly(monkeypatch, capsys):
    """A missing repo is a clear PROBLEM, and it is never a silent skip."""
    projects, snap = _healthy_world(repo_ok=False)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1                    # non-zero: not trustworthy
    captured = capsys.readouterr()
    assert "[PROBLEM]" in captured.out
    assert "NOT ALL CLEAR" in captured.err


def test_missing_board_is_a_problem(monkeypatch, capsys):
    projects, snap = _healthy_world()
    snap["boards"] = ["otherboard"]  # registered board not present
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[PROBLEM] board 'hscc' NOT found" in out


def test_topic_not_resolving_is_a_problem(monkeypatch, capsys):
    projects, snap = _healthy_world(topic=140)
    snap["topics"] = [Topic(id=999, name="unrelated")]  # 140 absent
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[PROBLEM] topic 140 does NOT resolve" in out


def test_telegram_transport_down_marks_topic_unverifiable(monkeypatch, capsys):
    """When the transport fails, the topic dimension is unverifiable, and the
    command says so loudly rather than guessing it is fine."""
    projects, snap = _healthy_world()
    # Inject everything but topics: leave topics at its default (None) so the
    # command actually goes to read it — and stub that read to fail, which is
    # the real production shape of a transport outage.
    snap.pop("topics")
    import flightdeck.core.telegram as tcore

    def _boom(**kwargs):
        raise tcore.TelegramError("daemon unreachable")

    monkeypatch.setattr(cmd.telegram, "list_topics", _boom)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[PROBLEM] Telegram unverifiable" in out


def test_exit_nonzero_when_anything_unverifiable():
    """Across all four breaking scenarios, doctor exits 1; healthy exits 0."""
    assert cmd._any_problem([
        {"name": "p", "checks": {"repo": {"ok": True}, "board": {"ok": False}}}
    ]) is True
    assert cmd._any_problem([
        {"name": "p", "checks": {"repo": {"ok": True}, "board": {"ok": True}}}
    ]) is False


def test_boards_unreadable_marks_board_unverifiable(monkeypatch, capsys):
    """If the board list itself cannot be read, every board check is unverifiable."""
    projects, snap = _healthy_world()
    # Inject everything but boards so the command reads them itself — and that
    # read fails, marking every board check unverifiable.
    snap.pop("boards")
    import flightdeck.core.kanban as kcore

    def _boom(**kwargs):
        raise kcore.KanbanError("hermes missing")

    monkeypatch.setattr(cmd.kanban, "list_boards", _boom)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[PROBLEM] unverifiable — cannot read board list" in out


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.func is not None


def test_json_shape(monkeypatch, capsys):
    projects, snap = _healthy_world()
    rc = cmd.cmd_doctor(_args(json=True, **snap), projects)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hscc"]["repo"]["ok"] is True
    assert payload["hscc"]["board"]["ok"] is True


# --------------------------------------------------------------------------- #
# Triangle: topic <-> board <-> repo binding
# --------------------------------------------------------------------------- #


def _triangle_world(projects=None, **overrides):
    """Injectable world that enables the full triangle check (boards + workdirs)."""
    projects = projects or [_project(repo="/tmp/hscc", board="hscc", topic=140)]
    snap = {
        "run": None,
        "boards": [p.board for p in projects if p.board],
        "topics": [Topic(id=p.topic, name=f"t{p.topic}") for p in projects if p.topic is not None],
        "repo_check": _repo_ok_fake(True),
        "workdirs": {p.board: p.repo for p in projects if p.board},
    }
    snap.update(overrides)
    return projects, snap


def test_healthy_fleet_prints_all_clear_with_count(monkeypatch, capsys):
    """A healthy fleet prints an explicit all-clear line naming how many
    projects passed the triangle check — evidence, not absence of output."""
    projects, snap = _triangle_world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    captured = capsys.readouterr()
    assert "[PROBLEM]" not in captured.out
    assert "TRIANGLE all clear" in captured.err
    assert "1 of 1 projects passed the triangle check" in captured.err


def test_missing_board_slug_is_a_problem_not_unknown(monkeypatch, capsys):
    """A registry naming a board that does not exist on Hermes is a PROBLEM
    (the registry is pointing at nothing), never dismissed as 'unknown'."""
    projects, snap = _triangle_world()
    snap["boards"] = ["some-other-board"]  # registered 'hscc' absent
    snap["workdirs"] = {}                  # no workdir data for a board we don't have
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[PROBLEM] board 'hscc' NOT found" in out


def test_workdir_mismatch_reports_both_paths_side_by_side(monkeypatch, capsys):
    """A board whose default_workdir differs from the project repo is reported
    with BOTH paths so the operator sees where cards would land vs where the
    project lives."""
    projects, snap = _triangle_world(repo="/tmp/hscc", board="hscc", topic=140)
    snap["workdirs"] = {"hscc": "/tmp/WRONG_repo_elsewhere"}
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "workdir [PROBLEM]" in out
    assert "default_workdir='/tmp/WRONG_repo_elsewhere'" in out
    assert "repo='/tmp/hscc'" in out


def test_workdir_match_is_normalised_not_raw(monkeypatch, capsys):
    """The workdir/repo comparison resolves + normalises paths, so a trailing
    slash or a symlinked/expanded form of the same repo is still a match."""
    projects, snap = _triangle_world(repo="/tmp/hscc", board="hscc", topic=140)
    snap["workdirs"] = {"hscc": "/tmp/hscc/"}  # trailing slash == /tmp/hscc
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr().out
    assert "workdir [ok]" in out


def test_workdir_none_for_mapped_board_is_a_problem(monkeypatch, capsys):
    """A board with no default_workdir at all still differs from a repo that
    exists, so it is reported (the pre-existing `hscc` board on this host)."""
    projects, snap = _triangle_world(repo="/tmp/hscc", board="hscc", topic=140)
    snap["workdirs"] = {"hscc": None}
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "workdir [PROBLEM]" in out
    assert "(no default_workdir)" in out


def test_two_projects_sharing_board_slug_is_reported(monkeypatch, capsys):
    """Two registry projects bound to the same board slug is a mis-binding that
    would silently merge their work; both projects are flagged."""
    p1 = _project(name="a", repo="/tmp/a", board="shared", topic=1)
    p2 = _project(name="b", repo="/tmp/b", board="shared", topic=2)
    projects = [p1, p2]
    _, snap = _triangle_world(projects=projects)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "board 'shared' is bound to MULTIPLE projects: a, b" in out


def test_two_projects_sharing_topic_id_is_reported(monkeypatch, capsys):
    """Two registry projects bound to the same Telegram topic id is a
    mis-binding that would attribute one topic's work to two projects; both
    are flagged."""
    p1 = _project(name="a", repo="/tmp/a", board="a", topic=140)
    p2 = _project(name="b", repo="/tmp/b", board="b", topic=140)
    projects = [p1, p2]
    _, snap = _triangle_world(projects=projects)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "topic 140 is bound to MULTIPLE projects: a, b" in out


def test_for_uncoordinated_projects_triangle_pass_count_is_accurate(monkeypatch, capsys):
    """In a healthy multi-project fleet the all-clear line names the full count."""
    projects = [
        _project(name="a", repo="/tmp/a", board="a", topic=1),
        _project(name="b", repo="/tmp/b", board="b", topic=2),
        _project(name="c", repo="/tmp/c", board="c", topic=3),
    ]
    _, snap = _triangle_world(projects=projects)
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    captured = capsys.readouterr()
    assert "TRIANGLE all clear" in captured.err
    assert "3 of 3 projects passed the triangle check" in captured.err


def test_triangle_json_includes_workdir(monkeypatch, capsys):
    projects, snap = _triangle_world()
    rc = cmd.cmd_doctor(_args(json=True, **snap), projects)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hscc"]["workdir"]["ok"] is True
    assert payload["hscc"]["workdir"]["detail"] == "board 'hscc' default_workdir matches repo"


# --------------------------------------------------------------------------- #
# Learning pipeline: is Hermes' memory actually ingesting?
# --------------------------------------------------------------------------- #
# Every filesystem + HTTP source is injected (tmp_path for the config file,
# fake callables for exists/access/mtime, a fake probe for /v1/models) so no
# test touches the real ~/.hermes or the network.

NOW = 1_900_000_000
# An mtime older than the 7-day threshold — the two-week blackout shape.
STALE_MTIME = NOW - (cmd._STALE_THRESHOLD_SECONDS + 2 * 86400)
# A memory file modified 1h ago — fresh under the 7-day threshold.
FRESH_MTIME = NOW - 3600
# A memori DB that is very old — informational only, must never fail anything.
DB_OLD_MTIME = NOW - (cmd._STALE_THRESHOLD_SECONDS + 20 * 86400)


def _memory_paths(home, profiles=("qa", "backend-engineer")):
    """Candidate MEMORY.md paths: the global one plus one per named profile."""
    p = Path(home)
    paths = [str(p / "memories" / cmd._MEMORY_FILENAME)]
    for name in profiles:
        paths.append(str(p / "profiles" / name / "memories" / cmd._MEMORY_FILENAME))
    return paths


def _path_mtime(specific, default):
    """A path-aware mtime injectable: any path containing a specific key gets
    that key's mtime; every other path gets `default`."""
    def _f(p):
        sp = str(p)
        for key, val in specific.items():
            if key in sp:
                return val
        return default
    return _f


def _write_empty_plist(tmp_path):
    """A scratch gateway plist with NO augment keys (a hermetic fallback)."""
    plist_path = tmp_path / "_empty_gateway.plist"
    data = {"Label": "ai.hermes.gateway", "EnvironmentVariables": {}}
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    return str(plist_path)


def _write_mem_config(tmp_path, db_path="memory.db"):
    """Write a real memori_byodb.json with a dbPath under tmp_path."""
    cfg = tmp_path / cmd._LEARNING_CONFIG
    cfg.write_text(json.dumps({"dbPath": db_path}), encoding="utf-8")
    return cfg


def _learning_args(tmp_path, **overrides):
    """Injectable kwargs for a healthy learning world (+ `learning=True`).

    Writes the memori_byodb.json config pointing at an ANCIENT memori DB
    (informational only — its age must never fail anything), and gives every
    candidate MEMORY.md file a FRESH mtime so memory-stale reads ok. The
    augment config comes from `env`; a scratch plist with NO augment keys is
    injected so the plist fallback can never accidentally supply one (and no
    test ever touches the real gateway plist).
    """
    home = str(tmp_path)
    db_path = str(tmp_path / "memories.db")
    _write_mem_config(tmp_path, db_path=db_path)
    memory_files = _memory_paths(home, profiles=("qa", "backend-engineer"))
    # Build a path-aware mtime map: every MEMORY.md is fresh, with the LAST
    # profile (backend-engineer) deterministically the newest; the memori DB is
    # ancient (informational only). This makes the named freshest file stable.
    mtime_map = {p: FRESH_MTIME for p in memory_files}
    mtime_map[memory_files[-1]] = FRESH_MTIME + 300  # newest profile wins
    mtime_map[db_path] = DB_OLD_MTIME
    snap = {
        "learning": True,
        "home": home,
        "env": {
            cmd.ENV_AUGMENT_URL: "http://augment.local:8000",
            cmd.ENV_AUGMENT_MODEL: "gpt-4o",
        },
        "now": NOW,
        # memori DB ancient (informational); every MEMORY.md fresh.
        "mtime": _path_mtime(mtime_map, default=FRESH_MTIME),
        "exists": lambda p: True,
        "access": lambda p, m: True,         # DB directory writable
        "probe": lambda url: ["gpt-4o"],      # served & no logical alias
        "memory_files": memory_files,
        "plist_source": _write_empty_plist(tmp_path),
    }
    snap.update(overrides)
    return snap


def test_learning_fresh_db_ok(monkeypatch, capsys, tmp_path):
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr().out
    assert "learning pipeline:" in out
    assert "memory-db [ok]" in out
    assert "memory-stale [ok]" in out
    assert "augment-model [ok]" in out
    assert "[PROBLEM]" not in out
    assert "[UNVERIFIED]" not in out


def test_learning_db_on_readonly_dir_is_problem(monkeypatch, capsys, tmp_path):
    """Fault #1: a DB on a read-only mount is a PROBLEM even though it exists."""
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path, access=lambda p, m: False))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "memory-db [PROBLEM]" in out
    assert "NOT writable" in out


def test_learning_db_missing_is_problem(monkeypatch, capsys, tmp_path):
    """The memori DB missing is still surfaced (a hardware fault), not hidden."""
    projects, snap = _healthy_world()
    db_path = str(tmp_path / "memories.db")
    snap.update(_learning_args(tmp_path, exists=lambda p: not p.endswith("memories.db")))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "memory-db [PROBLEM]" in out
    assert "memory DB missing" in out


# --- Memory freshness watches MEMORY.md, NOT the memori DB (D2) ------------- #
# The memori DB is inert and its age is informational; the honest freshness
# signal is the NEWEST MEMORY.md across profiles + global.

def test_memory_freshness_ignores_ancient_memori_db(monkeypatch, capsys, tmp_path):
    """The check reports the NEWEST profile MEMORY.md, never the DB's age.

    Regression test for the wrong-artifact bug: the memori DB is 27d old, but
    a profile MEMORY.md is fresh -> memory-stale must read OK and name the
    fresh memory file — NOT the DB.
    """
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr().out
    assert "memory-stale [ok]" in out
    assert "memory fresh: newest MEMORY.md is" in out
    # the freshest file is named, and it is a MEMORY.md, not memories.db
    assert "backend-engineer/memories/MEMORY.md" in out
    assert "memory-stale" in out and "memories.db" not in out.split("memory-stale")[-1]


def test_newest_across_profiles_is_picked_and_named(monkeypatch, capsys, tmp_path):
    """When profiles have different mtimes the NEWEST wins and is named.

    the wrong-artifact bug one more way: several profiles write memory at
    different times; the newest one must be the one reported.
    """
    home = str(tmp_path)
    db_path = str(tmp_path / "memories.db")
    _write_mem_config(tmp_path, db_path=db_path)
    memory_files = _memory_paths(home, profiles=("old1", "newest", "old2"))
    # 'newest' profile is the freshest; the others are stale.
    newest = str(tmp_path / "profiles" / "newest" / "memories" / cmd._MEMORY_FILENAME)
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        memory_files=memory_files,
        mtime=_path_mtime(
            {str(tmp_path / "profiles" / "old1" / "memories" / cmd._MEMORY_FILENAME): STALE_MTIME,
             str(tmp_path / "profiles" / "old2" / "memories" / cmd._MEMORY_FILENAME): STALE_MTIME,
             newest: FRESH_MTIME},
            default=STALE_MTIME,
        ),
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    # fresh -> ok (even though two other profiles are stale)
    assert rc == 0
    assert "memory-stale [ok]" in out
    assert "profiles/newest/memories/MEMORY.md" in out


def test_all_memory_files_stale_is_problem(monkeypatch, capsys, tmp_path):
    """ALL MEMORY.md files older than threshold -> learning stopped, PROBLEM."""
    home = str(tmp_path)
    db_path = str(tmp_path / "memories.db")
    _write_mem_config(tmp_path, db_path=db_path)
    memory_files = _memory_paths(home, profiles=("qa", "backend-engineer"))
    snap = _learning_args(
        tmp_path,
        # every memory file stale AND the memori DB ancient
        mtime=_path_mtime(
            {p: STALE_MTIME for p in memory_files + [db_path]},
            default=STALE_MTIME,
        ),
    )
    projects, _ = _healthy_world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "memory-stale [PROBLEM]" in out
    assert "memory STALE" in out
    assert "learning has stopped" in out


def test_no_readable_memory_file_is_unverified(monkeypatch, capsys, tmp_path):
    """No readable MEMORY.md at all -> UNVERIFIED, never ok. Cannot confirm."""
    snap = _learning_args(
        tmp_path,
        exists=lambda p: not p.endswith(cmd._MEMORY_FILENAME),
    )
    projects, _ = _healthy_world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "memory-stale [UNVERIFIED]" in out
    assert "no readable MEMORY.md" in out


# --------------------------------------------------------------------------- #
# Augment config source: gateway launchd plist, os.environ override (D2)
# --------------------------------------------------------------------------- #
# The augment env vars are NOT in a bare shell's environment — the gateway sets
# them in its launchd plist. We read env first then the plist as fallback.

def _write_plist(tmp_path, name, env_vars):
    """Write a real gateway-style plist with the given EnvironmentVariables."""
    plist_path = tmp_path / name
    data = {
        "Label": "ai.hermes.gateway",
        "EnvironmentVariables": env_vars,
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    return str(plist_path)


def test_plist_sourced_env_found_when_os_environ_empty(monkeypatch, capsys, tmp_path):
    """No os.environ augment vars but the plist has them -> config is found.

    The exact production shape: doctor runs outside the gateway process, so
    os.environ is empty and only the plist carries the augment URL/model.
    """
    plist_path = _write_plist(tmp_path, "gateway.plist", {
        cmd.ENV_AUGMENT_URL: "http://plist.local:9000",
        cmd.ENV_AUGMENT_MODEL: "gpt-4o",
    })
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path, env={}, plist_source=plist_path))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    out = capsys.readouterr().out
    assert rc == 0
    assert "augment-model [ok]" in out
    assert "model 'gpt-4o' served" in out
    assert "http://plist.local:9000" in out


def test_os_environ_overrides_plist(monkeypatch, capsys, tmp_path):
    """os.environ wins over the plist when both define the augment vars."""
    plist_path = _write_plist(tmp_path, "gateway.plist", {
        cmd.ENV_AUGMENT_URL: "http://plist.local:9000",
        cmd.ENV_AUGMENT_MODEL: "plist-model",
    })
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        env={
            cmd.ENV_AUGMENT_URL: "http://env.local:7000",
            cmd.ENV_AUGMENT_MODEL: "gpt-4o",
        },
        plist_source=plist_path,
        probe=lambda url: ["gpt-4o"],
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr().out
    # the env value (not the plist value) is what gets probed and reported
    assert "model 'gpt-4o' served at http://env.local:7000" in out


def test_neither_env_nor_plist_configured_is_unverified(monkeypatch, capsys, tmp_path):
    """Both sources absent -> genuinely not configured -> UNVERIFIED, never ok."""
    snap = _learning_args(tmp_path, env={})
    projects, _ = _healthy_world()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "augment-model [UNVERIFIED]" in out
    assert "augmentation not configured" in out


def test_learning_served_model_is_ok(monkeypatch, capsys, tmp_path):
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        env={cmd.ENV_AUGMENT_URL: "http://augment.local:8000",
             cmd.ENV_AUGMENT_MODEL: "gpt-4o"},
        probe=lambda url: ["gpt-4o", "other-llm"],
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 0
    out = capsys.readouterr().out
    assert "augment-model [ok]" in out
    assert "served" in out


def test_learning_unserved_model_is_problem_naming_both(monkeypatch, capsys, tmp_path):
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        env={cmd.ENV_AUGMENT_URL: "http://augment.local:8000",
             cmd.ENV_AUGMENT_MODEL: "gpt-4o"},
        probe=lambda url: ["claude-3-haiku-20240307"],
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "augment-model [PROBLEM]" in out
    assert "'gpt-4o' NOT served" in out
    assert "'claude-3-haiku-20240307'" in out  # names the served id too


def test_learning_concrete_model_under_alias_is_problem(monkeypatch, capsys, tmp_path):
    """Fault #3: pinning a concrete id when a logical alias is served is flagged."""
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        env={cmd.ENV_AUGMENT_URL: "http://augment.local:8000",
             cmd.ENV_AUGMENT_MODEL: "gpt-4o-2024-08-06"},
        probe=lambda url: ["gpt-4o", "gpt-4o-2024-08-06"],
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "augment-model [PROBLEM]" in out
    assert "CONCRETE id" in out
    assert "'gpt-4o'" in out


def test_learning_unreachable_endpoint_is_unverified_never_ok(
    monkeypatch, capsys, tmp_path
):
    """Probe failure (endpoint down) -> UNVERIFIED, and it is never presented as ok."""
    projects, snap = _healthy_world()
    snap.update(_learning_args(
        tmp_path,
        env={cmd.ENV_AUGMENT_URL: "http://augment.local:8000",
             cmd.ENV_AUGMENT_MODEL: "gpt-4o"},
        probe=lambda url: None,
    ))
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1
    out = capsys.readouterr().out
    assert "augment-model [UNVERIFIED]" in out


def test_learning_missing_mem_config_is_reported_not_crash(monkeypatch, capsys, tmp_path):
    """No memori_byodb.json at all -> reported as UNVERIFIED, never a crash."""
    # Do NOT write the config; home points at an empty tmp_path.
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path))
    # remove the config file we auto-wrote
    (tmp_path / cmd._LEARNING_CONFIG).unlink()
    rc = cmd.cmd_doctor(_args(**snap), projects)
    assert rc == 1                       # reported, exits non-zero
    out = capsys.readouterr().out
    assert "memory-db [UNVERIFIED]" in out
    assert "cannot read the memori DB" in out


def test_learning_json_shape(monkeypatch, capsys, tmp_path):
    projects, snap = _healthy_world()
    snap.update(_learning_args(tmp_path))
    rc = cmd.cmd_doctor(_args(json=True, **snap), projects)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    key = "_learning"
    assert payload[key]["memory-db"]["status"] == "ok"
    assert payload[key]["augment-model"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# doctor uses the SHARED probe helper (flightdeck.core.probe) — the fix that
# stops the wrong-method bug recurring. `_probe_models` must route through
# `probe.probe_http`, derive the GET-safe /models URL from the configured
# chat-completions URL, and POST (never GET) when no models URL can be derived.
# --------------------------------------------------------------------------- #

class _FakeResponse:
    """A urllib-style response object exposing ``status`` and ``read()``."""

    def __init__(self, status=200, payload=b"{}"):
        self.status = status
        self._payload = payload

    def getcode(self):
        return self.status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_doctor_probe_models_get_derived_models_url_and_uses_shared_helper():
    """_probe_models GETs the /models URL derived from a chat-completions URL.

    This is the behavioural proof that doctor routes through the shared probe
    helper — it records the actual method + URL the probe issued, and asserts
    it is a GET to .../v1/models, never a GET (or POST) to the POST-only
    chat-completions URL.
    """
    calls = []

    def recording_urlopen(request, timeout=5.0):  # noqa: ARG001
        calls.append((request.method, request.full_url))
        return _FakeResponse(status=200, payload=json.dumps(
            {"data": [{"id": "gpt-4o"}, {"id": "other-llm"}]}).encode())

    served = cmd._probe_models(
        "http://augment.local:8000/v1/chat/completions",
        _urlopen=recording_urlopen,
    )
    assert served == ["gpt-4o", "other-llm"]
    # The derived, GET-safe /models URL was used — not the POST-only chat url.
    assert calls == [("GET", "http://augment.local:8000/v1/models")]


def test_doctor_probe_models_posts_when_no_models_url_derivable():
    """When the /models URL cannot be derived, _probe_models POSTs a minimal
    request to the configured URL — it NEVER GETs a chat-completions URL."""
    calls = []

    def recording_urlopen(request, timeout=5.0):  # noqa: ARG001
        calls.append((request.method, request.full_url))
        return _FakeResponse(status=200, payload=b"{}")

    # Not a chat-completions shape -> derive_models_url returns None -> POST.
    served = cmd._probe_models("http://augment.local:8000/v1/models", _urlopen=recording_urlopen)
    assert served == []  # reachable (any HTTP response) but no models list
    assert calls == [("POST", "http://augment.local:8000/v1/models")]


def test_doctor_probe_models_connection_refused_is_unverified_none():
    """Connection refused through _probe_models -> None -> doctor reports the
    augment check as UNVERIFIED (unreachable), never ok."""
    from urllib import error as _uerror

    def refused_urlopen(request, timeout=5.0):  # noqa: ARG001
        raise _uerror.URLError(ConnectionRefusedError("connection refused"))

    assert cmd._probe_models(
        "http://augment.local:8000/v1/chat/completions",
        _urlopen=refused_urlopen,
    ) is None


def test_doctor_probe_is_shared_probe_module():
    """doctor's default model probe IS the shared helper's probe_http (not a
    hand-rolled copy) — the whole point of the fix: one place to know how to
    check a service, used by every command."""
    from flightdeck.core import probe
    assert cmd.probe is probe
