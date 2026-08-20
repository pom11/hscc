"""Tests for flightdeck.commands.sync — `flightdeck project sync`.

`sync` discovers from THREE independent sources (repos, Telegram topics, Hermes
boards) and correlates them into a proposed registry. It is the adoption path
that must survive **corrupted topic names** — titles overwritten by bot message
text on busy topics — and must never route work to the wrong topic.

Every test here drives :func:`run_sync` (pure decision logic — no I/O) with
plain-data stubs, and :func:`cmd_sync`/`apply_writes` against a stub registry
file in a pytest tmp_path. No test touches git, Telegram, or the live cluster.

The real-data fixture mirrors what the card verifies against (repos under
~/dev, topics in the HSCC group, boards default + ecofire) without hardcoding
results — the correlation must produce the intended semantics, not a canned
answer.
"""

from flightdeck.commands import sync as sync_cmd
from flightdeck.core import kanban, registry
from flightdeck.core.telegram import Topic

# --------------------------------------------------------------------------- #
# Real-data fixture (NOT hardcoded results — just the discoverable inputs)
# --------------------------------------------------------------------------- #
REPOS = [
    "~/dev/hscc",
    "~/dev/ecofire",
    "~/dev/EcoFire_customizations_bc",
    "~/dev/ecofire-powerbi",
    "~/dev/sphoin_engine",
    "~/dev/soconn",
    "~/dev/flosana",
    "~/dev/hermes-prfix",
]
TOPICS = [
    Topic(140, "HSCC cluster"),
    Topic(2046, "HSCC Repo"),
    Topic(2257, "app.ecofire.ro"),
    Topic(2, "sphoin"),
    Topic(3899, "EcoFire BC"),
    Topic(6366, "Power Bi"),
    Topic(6369, "EFS Driver"),
    Topic(7074, "SOCOON"),
    Topic(7253, "Flosana.es"),
]
BOARDS = ["default", "ecofire"]


def T(**kw):
    """Build a Project quickly from kwargs (default fixture values)."""
    return registry.Project(
        name=kw.get("name", "hscc"),
        repo=kw.get("repo", "~/dev/hscc"),
        topic=kw.get("topic"),
        topic_name=kw.get("topic_name"),
        board=kw.get("board"),
    )


# --------------------------------------------------------------------------- #
# Normalisation + corruption heuristics
# --------------------------------------------------------------------------- #

def test_normalize_folds_case_and_separators():
    assert sync_cmd.normalize("HSCC cluster") == "hscc-cluster"
    assert sync_cmd.normalize("EcoFire_customizations_bc") == "ecofire-customizations-bc"
    assert sync_cmd.normalize("app.ecofire.ro") == "app-ecofire-ro"
    assert sync_cmd.normalize("Flosana.es") == "flosana-es"


def test_names_match_folds_prefix_and_suffix():
    assert sync_cmd.names_match("hscc", "HSCC cluster") is True
    assert sync_cmd.names_match("sphoin", "sphoin_engine") is True
    assert sync_cmd.names_match("ecofire", "EcoFire_customizations_bc") is True
    assert sync_cmd.names_match("hscc", "soconn") is False


def test_looks_corrupted_detects_message_text():
    # busy-topic overwrites: long, newline, bot prefix, emoji+colon
    assert sync_cmd.looks_corrupted(
        "Acknowledged \u2014 v1.8.1 completes the alias work"
    ) is True
    assert sync_cmd.looks_corrupted("Self-improvement review: agent did X") is True
    assert sync_cmd.looks_corrupted("\U0001f4be Self-improvement review: ...") is True
    assert sync_cmd.looks_corrupted("multi\nline message text") is True
    # quiet topics keep clean short names
    assert sync_cmd.looks_corrupted("HSCC cluster") is False
    assert sync_cmd.looks_corrupted("app.ecofire.ro") is False
    assert sync_cmd.looks_corrupted("EcoFire BC") is False


# --------------------------------------------------------------------------- #
# Correlation — the three core behaviours
# --------------------------------------------------------------------------- #

def test_full_clean_correlation_matches_all_surfaces():
    """In the healthy case every repo/topic/board that can line up does
    (matched = all three present). Uses a self-consistent board set so the
    MATCHED path is exercised end to end."""
    boards = ["hscc-repo", "soconn", "sphoin", "unrelated-board"]
    report = sync_cmd.run_sync(
        repos=["~/dev/hscc", "~/dev/soconn", "~/dev/sphoin_engine"],
        topics=[Topic(140, "HSCC Repo"), Topic(7074, "Soconn"), Topic(2, "sphoin")],
        boards=boards,
        projects=[],
    )
    matched = {m.name: m for m in report.matched}
    assert "hscc" in matched
    assert matched["hscc"].topic == 140 and matched["hscc"].board == "hscc-repo"
    assert "soconn" in matched
    # project name derives from the repo basename -> sphoin_engine, not "sphoin"
    assert "sphoin_engine" in matched
    assert matched["sphoin_engine"].topic == 2
    assert matched["sphoin_engine"].board == "sphoin"
    # The unmatched board is an orphan.
    assert ("unrelated-board", 0) in report.orphan_boards
    # Nothing ambiguous here -> all unambiguous.
    assert report.ambiguous == []


def test_real_data_projects_correlate_partial_when_shared_board():
    """The real ~/dev set correlates to its topics by name; because there is one
    shared 'default' board (not named per-project), the realistic result is
    PARTIAL (repo+topic present, board missing) — never a dropped project.

    Note the real gap intentionally NOT hand-waved: repo `soconn` and topic 7074
    'SOCOON' do NOT name-match (soconn != socoon), so soconn surfaces as an
    ORPHAN REPO and 7074 as an ORPHAN TOPIC — an honest gap, never a false
    binding. The tool reports it rather than guessing."""
    report = sync_cmd.run_sync(repos=REPOS, topics=TOPICS, boards=BOARDS, projects=[])
    seen = {m.name for m in report.matched} | {p.name for p in report.partial}
    assert "hscc" in seen
    assert "sphoin_engine" in seen
    # The quiet, correctly-named projects correlate cleanly.
    hscc = next((p for p in report.partial if p.name == "hscc"), None)
    assert hscc is not None and hscc.topic == 140
    # soconn/socoon is a genuine name gap -> reported, never guessed.
    assert any(r.endswith("/soconn") for r in report.orphan_repos)
    assert any(t.id == 7074 for t in report.orphan_topics)

def test_registry_binding_beats_a_conflicting_name_match():
    """The 2257-shaped disease, correctly resolved. Registry binds topic 140 to
    hscc. Live topic 140's name ('HSCC cluster') would ALSO name-match a free
    repo `~/dev/hscc-cluster`, but the registry binding must win: the bound
    topic never participates in name matching, so `~/dev/hscc-cluster` is left
    an orphan and topic 140 stays with hscc."""
    projects = [T(name="hscc", repo="~/dev/hscc", topic=140, topic_name="HSCC cluster", board="hscc")]
    free_repos = ["~/dev/hscc-cluster"]
    report = sync_cmd.run_sync(
        repos=free_repos + ["~/dev/hscc"],
        topics=[Topic(140, "HSCC cluster")],
        boards=["hscc"],
        projects=projects,
    )
    # hscc is MATCHED via its existing binding (topic id, not a guess).
    assert {m.name for m in report.matched} == {"hscc"}
    assert report.matched[0].topic == 140
    # The free repo that name-matches the bound topic is an ORPHAN, not bound.
    assert report.orphan_repos == ["~/dev/hscc-cluster"]
    # Nothing is proposed for the bound-topic conflict.
    assert report.to_write == []


def test_name_corrupted_topic_correlates_via_registry_id_and_reports_restore():
    """A NAME-CORRUPTED topic (live name overwritten by a bot message) still
    correlates to its project through the registry's topic id, is reported as
    MATCHED (not lost), and is flagged with the exact rename command to restore
    its expected name."""
    projects = [T(name="hscc", repo="~/dev/hscc", topic=140, topic_name="HSCC cluster", board="hscc")]
    corrupted = Topic(140, "Acknowledged \u2014 v1.8.1 completes the alias work")
    report = sync_cmd.run_sync(
        repos=["~/dev/hscc"],
        topics=[corrupted],
        boards=["hscc"],
        projects=projects,
    )
    # Still correlated through its registry id.
    assert {m.name for m in report.matched} == {"hscc"}
    assert report.matched[0].topic == 140
    # Flagged NAME-CORRUPTED with the expected name + exact restore command.
    assert len(report.name_corrupted) == 1
    c = report.name_corrupted[0]
    assert c.topic_id == 140
    assert c.expected == "HSCC cluster"
    assert c.project == "hscc"
    assert c.rename_cmd == "flightdeck topics rename 140 'HSCC cluster'"
    # The corrupted topic is NOT proposed for any rewrite.
    assert report.to_write == []


# --------------------------------------------------------------------------- #
# Ambiguity — never auto-bound
# --------------------------------------------------------------------------- #

def test_ambiguous_repo_is_reported_and_not_written_even_with_apply(tmp_path, capsys):
    """An ambiguous name (ecofire -> both ~/dev/ecofire AND
    ~/dev/EcoFire_customizations_bc) is reported and NEVER written, even under
    --apply. A wrong binding routes work to the wrong topic."""
    reg = str(tmp_path / "registry.yaml")
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire", "~/dev/EcoFire_customizations_bc"],
        topics=[Topic(2257, "ecofire")],
        boards=["ecofire"],
        projects=[],
    )
    assert len(report.ambiguous) == 1
    amb = report.ambiguous[0]
    assert set(amb.repos) == {"~/dev/ecofire", "~/dev/EcoFire_customizations_bc"}
    # report.to_write stays empty -> nothing safe to write.
    assert report.to_write == []
    written = sync_cmd.apply_writes(report.to_write, reg)
    assert written == []
    assert registry.load_registry(reg) == []  # registry untouched


def test_render_shows_ambiguous_with_resolving_hint(capsys):
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire", "~/dev/EcoFire_customizations_bc"],
        topics=[Topic(2257, "ecofire")],
        boards=["ecofire"],
        projects=[],
    )
    out = sync_cmd.render(report)
    assert "AMBIGUOUS" in out
    assert "ecofire" in out
    assert "EcoFire_customizations_bc" in out


# --------------------------------------------------------------------------- #
# Orphans on all three sides
# --------------------------------------------------------------------------- #

def test_orphans_reported_on_all_three_sides():
    """A repo with no topic, a topic with no repo, and a board with no project
    are each a real gap and all three must surface."""
    report = sync_cmd.run_sync(
        repos=["~/dev/hermes-prfix"],          # repo, no topic/board
        topics=[Topic(6369, "EFS Driver")],     # topic, no repo/board
        boards=["default", "ecofire"],          # board 'default' has no repo
        projects=[],
        board_cards={"default": 358, "ecofire": 12},
    )
    assert report.orphan_repos == ["~/dev/hermes-prfix"]
    assert any(t.id == 6369 for t in report.orphan_topics)
    board_slugs = {b for b, _ in report.orphan_boards}
    assert "default" in board_slugs
    assert "ecofire" in board_slugs


def test_orphan_board_reports_card_count():
    report = sync_cmd.run_sync(
        repos=[],
        topics=[],
        boards=["ecofire"],
        projects=[],
        board_cards={"ecofire": 4},
    )
    assert report.orphan_boards == [("ecofire", 4)]


# --------------------------------------------------------------------------- #
# --apply idempotence + never-overwrite
# --------------------------------------------------------------------------- #

def test_rerun_after_apply_is_a_noop(tmp_path):
    """After --apply writes the unambiguous matches, re-running sync reports
    them as MATCHED and proposes NOTHING new."""
    reg = str(tmp_path / "registry.yaml")
    # First pass: discover + apply everything unambiguous.
    report1 = sync_cmd.run_sync(
        repos=["~/dev/hscc"],
        topics=[Topic(140, "HSCC cluster")],
        boards=["hscc"],
        projects=[],
    )
    written = sync_cmd.apply_writes(report1.to_write, reg)
    assert len(written) == 1
    # Second pass against the now-populated registry: same inputs.
    projects = registry.load_registry(reg)
    report2 = sync_cmd.run_sync(
        repos=["~/dev/hscc"],
        topics=[Topic(140, "HSCC cluster")],
        boards=["hscc"],
        projects=projects,
    )
    # hscc still MATCHED (now via registry), nothing new proposed.
    assert {m.name for m in report2.matched} == {"hscc"}
    assert report2.to_write == []
    assert report2.orphan_repos == []
    assert report2.orphan_topics == []
    assert report2.orphan_boards == []


def test_existing_binding_never_overwritten_conflict_reported():
    """If a free topic/board would name-match a repo that is ALREADY bound (and
    that dimension is already full), sync reports the conflict and does not
    overwrite the existing binding."""
    projects = [T(name="hscc", repo="~/dev/hscc", topic=140, board="hscc", topic_name="HSCC cluster")]
    report = sync_cmd.run_sync(
        repos=["~/dev/hscc"],
        topics=[Topic(140, "HSCC cluster"), Topic(999, "hscc")],
        boards=["hscc"],
        projects=projects,
    )
    # The conflicting free topic 999 name-matches the bound repo hscc.
    conflicts = [c for c in report.conflicts if c.source == "topic"]
    assert len(conflicts) == 1
    # repo is stored expanded (registry normalises ~ -> absolute), so compare
    # normalized rather than the raw "~/dev/hscc" spelling.
    assert sync_cmd.git_state_head_norm(conflicts[0].repo).endswith("/dev/hscc")
    # hscc's binding is untouched — it stays matched, not rewritten.
    assert {m.name for m in report.matched} == {"hscc"}
    assert report.matched[0].topic == 140
    assert report.to_write == []


# --------------------------------------------------------------------------- #
# CLI wiring: run()/cmd_sync dispatch, apply path
# --------------------------------------------------------------------------- #

def test_cmd_sync_apply_writes_only_unambiguous(tmp_path, capsys):
    """cmd_sync with --apply writes hscc (unambiguous) but leaves ecofire
    ambiguous untouched, to a registry file."""
    reg = str(tmp_path / "registry.yaml")

    import argparse

    args = argparse.Namespace(
        project_cmd="sync",
        apply=True,
        json=False,
        # discovery injected directly (pure stubs, never real systems):
        repos=["~/dev/hscc", "~/dev/ecofire", "~/dev/EcoFire_customizations_bc"],
        _run=None,
        _boards={"hscc": 0, "ecofire": 5},
        client=None,
        roots=None,
        registry=reg,
    )
    # Monkeypatch the three discovery functions so cmd_sync never touches git,
    # Telegram, or the cluster.
    from flightdeck.commands import sync as mod

    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: ["hscc", "ecofire"]
    mod.discover_topics = lambda _client=None: [Topic(140, "HSCC cluster")]

    rc = sync_cmd.cmd_sync(args)
    out = capsys.readouterr().out
    assert rc == 1  # ambiguous present -> nonzero (operator must look)

    written = registry.load_registry(reg)
    names = {p.name for p in written}
    assert "hscc" in names
    # Ambiguous ecofire is NOT written.
    assert not any("ecofire" in n for n in names)
    # hscc bound to topic 140 and board hscc.
    hscc = registry.get_project("hscc", reg)
    assert hscc.topic == 140
    assert hscc.board == "hscc"


def test_cmd_sync_dry_run_writes_nothing(tmp_path, capsys):
    """Without --apply, cmd_sync leaves the registry file absent/empty."""
    reg = str(tmp_path / "registry.yaml")

    import argparse

    from flightdeck.commands import sync as mod

    args = argparse.Namespace(
        project_cmd="sync",
        apply=False,
        json=False,
        repos=["~/dev/hscc"],
        _run=None,
        _boards={"hscc": 0},
        client=None,
        roots=None,
        registry=reg,
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: ["hscc"]
    mod.discover_topics = lambda _client=None: [Topic(140, "HSCC cluster")]

    rc = sync_cmd.cmd_sync(args)
    out = capsys.readouterr().out
    # Dry-run still reports MATCHED (the proposal) but writes nothing.
    assert "MATCHED" in out
    assert "--apply" in out
    import os

    assert os.path.exists(reg) is False  # nothing persisted


def test_run_rejects_unknown_project_subcommand(capsys):
    import argparse

    args = argparse.Namespace(project_cmd="bogus", registry=None, client=None, json=False)
    rc = sync_cmd.run(args, "x")
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown subcommand" in err


# --------------------------------------------------------------------------- #
# Rendered output — the NAME-CORRUPTED block carries the restore command
# --------------------------------------------------------------------------- #

def test_render_emits_name_corrupted_block_with_rename_command():
    """The rendered proposal's NAME-CORRUPTED section gives the operator the
    expected name and the exact one-line restore command."""
    projects = [T(name="hscc", repo="~/dev/hscc", topic=140, topic_name="HSCC cluster", board="hscc")]
    corrupted = Topic(140, "Acknowledged \u2014 v1.8.1 completes the alias work")
    report = sync_cmd.run_sync(
        repos=["~/dev/hscc"],
        topics=[corrupted],
        boards=["hscc"],
        projects=projects,
    )
    out = sync_cmd.render(report)
    assert "NAME-CORRUPTED" in out
    assert "'HSCC cluster'" in out
    assert "restore: flightdeck topics rename 140 'HSCC cluster'" in out


# --------------------------------------------------------------------------- #
# CLI integration — `project sync` is reachable through the auto-discovery seam
# --------------------------------------------------------------------------- #

def test_project_sync_is_discovered_and_dispatched_by_cli(tmp_path, monkeypatch):
    """The whole point of the auto-discovery wiring: `flightdeck project sync`
    must parse AND dispatch. This guards the argparse integration where sync is
    a subcommand of the `project` group (owned by command.project) — if that
    ever regresses into a duplicate `project` parser, the CLI breaks badly."""
    from flightdeck.cli import build_parser, _dispatch
    from flightdeck.commands import sync as mod

    # Stub discovery so nothing touches git / Telegram / the cluster.
    monkeypatch.setattr(mod, "discover_repos", lambda roots=None, _run=None: ["~/dev/hscc"])
    monkeypatch.setattr(mod, "discover_boards", lambda: ["hscc"])
    monkeypatch.setattr(mod, "discover_topics", lambda _client=None: [Topic(140, "HSCC cluster")])
    # cmd_sync reads each board's card count for orphan reporting; stub it so
    # nothing touches the live board (HOME-coupled) — we assert on the CLI
    # dispatch, not on board I/O.
    monkeypatch.setattr(kanban, "board_card_count", lambda b: 0)

    args = build_parser().parse_args(["project", "sync"])
    assert args.command == "project"          # routed via the project group
    assert args.project_cmd == "sync"
    assert getattr(args, "func", None) is not None

    rc = _dispatch(args)
    assert rc == 0                            # clean proposal, nothing to fix


def test_project_sync_apply_end_to_end_is_idempotent(tmp_path, monkeypatch, capsys):
    """Full lifecycle: dry-run proposes, --apply writes unambiguous, and a
    re-run after apply is a clean MATCHED no-op — all through the real CLI."""
    from flightdeck.cli import build_parser, _dispatch
    from flightdeck.commands import sync as mod

    reg = str(tmp_path / "registry.yaml")

    def fake_repos(roots=None, _run=None):
        return ["~/dev/hscc"]

    monkeypatch.setattr(mod, "discover_repos", fake_repos)
    monkeypatch.setattr(mod, "discover_boards", lambda: ["hscc"])
    monkeypatch.setattr(mod, "discover_topics", lambda _client=None: [Topic(140, "HSCC cluster")])
    # Stub the per-board card-count I/O so nothing touches the live board
    # (HOME-coupled); this test asserts on the apply lifecycle, not board reads.
    monkeypatch.setattr(kanban, "board_card_count", lambda b: 0)

    # Apply pass.
    args = build_parser().parse_args(["project", "sync", "--apply"])
    args.registry = reg  # registry normally defaults to ~/.flightdeck — force tmp
    rc = _dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied: wrote 'hscc'" in out
    assert registry.get_project("hscc", reg).topic == 140

    # Re-run (dry): everything now MATCHED, nothing proposed.
    args2 = build_parser().parse_args(["project", "sync"])
    args2.registry = reg
    rc2 = _dispatch(args2)
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "MATCHED" in out2
    assert "applied:" not in out2
    assert len(registry.load_registry(reg)) == 1  # no duplicates, nothing new


# --------------------------------------------------------------------------- #
# N2 — qualified no-board wording, ambiguous resolve command, --ignore-topic
# --------------------------------------------------------------------------- #

def test_no_board_partial_renders_qualified_wording_not_bare_partial():
    """A project with a repo (and topic) but no board mapping is reported with
    the benign 'no board mapping (cards attributed by repo path)' wording —
    NOT an unqualified PARTIAL that reads like a defect."""
    projects = [
        T(name="ecofire-app", repo="~/dev/ecofire-app", topic=2257, board=None),
    ]
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire-app"],
        topics=[Topic(2257, "ecofire")],
        boards=["default"],
        projects=projects,  # board=None -> bound but board dimension missing
    )
    # The bound project lands in partial (board missing) with no_board True.
    partials = [p for p in report.partial if p.name == "ecofire-app"]
    assert len(partials) == 1
    assert partials[0].no_board is True
    out = sync_cmd.render(report)
    # Qualified wording, not a bare defect.
    assert "no board mapping (cards attributed by repo path" in out
    assert "MATCHED" not in out  # it is NOT silently treated as fully matched


def test_partial_missing_topic_is_not_no_board():
    """no_board is a specific, benign case (repo present, board missing). A
    partial missing its repo is NOT flagged no_board — it is a genuine gap."""
    projects = [T(name="hscc", repo="~/dev/hscc", topic=140, board=None)]
    report = sync_cmd.run_sync(
        repos=[],  # repo absent -> dims['repo'] False
        topics=[Topic(140, "HSCC cluster")],
        boards=[],
        projects=projects,
    )
    partials = [p for p in report.partial if p.name == "hscc"]
    assert len(partials) == 1
    assert partials[0].repo is None
    assert partials[0].no_board is False  # repo missing is a real gap


def test_ambiguous_repo_reported_with_resolve_command_never_bound(tmp_path):
    """An ambiguous repo match (e.g. sphoin -> both ~/dev/sphoin and
    ~/dev/sphoin_engine) is reported WITH the exact command to resolve it and
    is never auto-bound. A wrong binding sends work to the wrong topic."""
    report = sync_cmd.run_sync(
        repos=["~/dev/sphoin", "~/dev/sphoin_engine"],
        topics=[Topic(2, "sphoin")],
        boards=["sphoin"],
        projects=[],  # no registry entry yet -> both repos are free candidates
    )
    # The ambiguity surfaces: one source matches two free repos.
    assert len(report.ambiguous) == 1
    out = sync_cmd.render(report)
    assert "AMBIGUOUS" in out
    # The exact resolve command (with the chosen repo plugged in) is shown.
    assert "resolve: `flightdeck project new sphoin" in out
    assert "--repo '<chosen repo>' --apply`" in out
    # Never auto-bound: nothing proposed, nothing written, registry untouched.
    assert report.to_write == []
    reg = str(tmp_path / "registry.yaml")
    assert sync_cmd.apply_writes(report.to_write, reg) == []


def test_ambiguous_is_never_auto_rebound_despite_existing_registry_entry():
    """An existing registry entry always wins over a guess. When the bound repo
    is present alongside an ambiguous free lookalike, sync never rebinds the
    existing project to a different repo — the free repo stays a reported
    ambiguity/orphan, and the bound project keeps its binding."""
    projects = [T(name="sphoin", repo="~/dev/sphoin_engine", topic=2, board="sphoin")]
    report = sync_cmd.run_sync(
        repos=["~/dev/sphoin", "~/dev/sphoin_engine"],
        topics=[Topic(2, "sphoin")],
        boards=["sphoin"],
        projects=projects,
    )
    # The bound project stays matched via its registry binding (topic id).
    assert {m.name for m in report.matched} == {"sphoin"}
    assert report.matched[0].repo == "~/dev/sphoin_engine"
    # The free lookalike is surfaced as ambiguous (or orphan), never written.
    assert report.to_write == []


def test_apply_still_writes_only_unambiguous_matches():
    """--apply continues to write only unambiguous matches; an ambiguous repo
    and a no-board project are never written, and a fully clean match is."""
    projects = [T(name="ecofire", repo="~/dev/ecofire", topic=2257, board="ecofire")]
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire", "~/dev/sphoin", "~/dev/sphoin_engine"],
        topics=[Topic(2257, "ecofire"), Topic(2, "sphoin")],
        boards=["ecofire", "sphoin"],
        projects=projects,
    )
    # sphoin is ambiguous (both repos match) -> not in to_write.
    assert any("sphoin" in a.key for a in report.ambiguous)
    assert all("sphoin" not in proj.name for proj in report.to_write)
    assert report.to_write == []


def test_topic_id_1_general_suppressed_by_default():
    """Telegram's built-in General topic (id 1) is never reported as an orphan
    by default — no ignore config needed."""
    report = sync_cmd.run_sync(
        repos=[],
        topics=[Topic(1, "General"), Topic(6369, "EFS Driver")],
        boards=[],
        projects=[],
    )
    orphan_ids = {t.id for t in report.orphan_topics}
    assert 1 not in orphan_ids       # General suppressed
    assert 6369 in orphan_ids        # the real orphan still surfaces


def test_ignored_topic_can_be_supplied_and_suppresses_orphan():
    """A topic flagged via ignored_topics is suppressed from orphan reporting."""
    report = sync_cmd.run_sync(
        repos=[],
        topics=[Topic(6369, "EFS Driver"), Topic(12, "X Feed")],
        boards=[],
        projects=[],
        ignored_topics={6369},
    )
    orphan_ids = {t.id for t in report.orphan_topics}
    assert 6369 not in orphan_ids
    assert 12 in orphan_ids


def test_ignore_topic_persists_and_suppresses_on_next_run(tmp_path, capsys):
    """cmd_sync --apply --ignore-topic persists the id to the registry's
    ignored_topics list, and a re-run (dry or apply) suppresses that topic."""
    reg = str(tmp_path / "registry.yaml")

    import argparse
    from flightdeck.commands import sync as mod

    args = argparse.Namespace(
        project_cmd="sync",
        apply=True,
        json=False,
        repos=["~/dev/sphoin_engine"],
        _run=None,
        _boards={},
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=[6369],
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: []
    mod.discover_topics = lambda _client=None: [Topic(6369, "EFS Driver"), Topic(12, "X Feed")]
    rc = sync_cmd.cmd_sync(args)
    capsys.readouterr()
    # Persisted.
    assert registry.load_ignored_topics(reg) == [6369]

    # Next run (dry) reads the persisted ignore set and suppresses 6369.
    args2 = argparse.Namespace(
        project_cmd="sync",
        apply=False,
        json=False,
        repos=["~/dev/sphoin_engine"],
        _run=None,
        _boards={},
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=None,
    )
    mod.discover_topics = lambda _client=None: [Topic(6369, "EFS Driver"), Topic(12, "X Feed")]
    rc2 = sync_cmd.cmd_sync(args2)
    out2 = capsys.readouterr().out
    assert "6369" not in out2  # suppressed on the next run
    assert "12" in out2        # the unflagged orphan still reported
    assert rc2 == 1          # topic 12 still an orphan -> nonzero


def test_ignore_topic_merges_repeatable_and_roundtrips_sorted(tmp_path, capsys):
    """Repeated --ignore-topic ids persist, and re-saving via project writes
    does not drop the ignore set (carried through every save)."""
    reg = str(tmp_path / "registry.yaml")

    import argparse
    from flightdeck.commands import sync as mod

    args = argparse.Namespace(
        project_cmd="sync",
        apply=True,
        json=False,
        repos=["~/dev/sphoin_engine"],
        _run=None,
        _boards={},
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=[6369, 12],
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: []
    mod.discover_topics = lambda _client=None: []
    sync_cmd.cmd_sync(args)
    capsys.readouterr()
    assert registry.load_ignored_topics(reg) == [12, 6369]

    # A later unrelated project save must NOT drop the ignore set.
    registry.add_project("x", "~/dev/x", path=reg)
    assert registry.load_ignored_topics(reg) == [12, 6369]


def test_no_board_partial_is_not_a_defect_cue():
    """A project missing only its board is NOT a routing defect — attribution is
    by repo path, so a no-board partial is distinct from orphan/ambiguous/
    conflict (things that would send work to the wrong place). It lands as a
    no-board partial, proposing nothing to write."""
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire-app"],
        topics=[Topic(2257, "ecofire")],
        boards=[],
        projects=[T(name="ecofire-app", repo="~/dev/ecofire-app", topic=2257, board=None)],
    )
    # It lands as a no_board partial, not an orphan/ambiguous/conflict.
    assert [p.no_board for p in report.partial] == [True]
    assert not report.orphan_repos
    assert not report.ambiguous
    assert not report.conflicts
    assert report.to_write == []


def test_renders_qualified_wording_in_full_proposal_not_just_partial_line():
    """End-to-end: a registry-bound PARTIAL project (has a repo and topic but
    no board mapping) renders the qualified wording inline, so the operator
    sees it is benign, not broken."""
    report = sync_cmd.run_sync(
        repos=["~/dev/ecofire-app"],
        topics=[Topic(2257, "app.ecofire.ro")],
        boards=[],
        projects=[T(name="ecofire-app", repo="~/dev/ecofire-app", topic=2257, board=None)],
    )
    out = sync_cmd.render(report)
    assert "PARTIAL" in out
    assert "no board mapping (cards attributed by repo path" in out


# --------------------------------------------------------------------------- #
# N9 — --create-boards: give every project its own Hermes board
# --------------------------------------------------------------------------- #

class FakeKdb:
    """A stand-in for hermes_cli.kanban_db with create_board recorded.

    Records every create_board call so tests can assert exactly which slug /
    name / default_workdir were used — and that NO read/move/write method on
    the board library is ever touched (creation only). Nothing here reads,
    moves or modifies a card.
    """

    def __init__(self):
        self.created = []          # (slug, name, default_workdir)
        self.card_methods_called = []  # e.g. 'list_tasks', 'complete_task'

    def create_board(self, slug, *, name=None, description=None, icon=None,
                     color=None, default_workdir=None):
        self.created.append(
            {"slug": slug, "name": name, "default_workdir": default_workdir}
        )

    # Any card-touching method is a bug under N9 — forbid it so a regression
    # fails loudly instead of silently adopting/moving cards.
    def connect(self, board=None):
        raise AssertionError("N9 board creation must never connect to a board")

    def list_tasks(self, *a, **kw):
        raise AssertionError("N9 board creation must never read cards")

    def complete_task(self, *a, **kw):
        raise AssertionError("N9 board creation must never move a card")

    def archive_task(self, *a, **kw):
        raise AssertionError("N9 board creation must never move a card")


def _reg_write(tmp_path, projects):
    """Write `projects` to a registry file and return its path."""
    p = str(tmp_path / "registry.yaml")
    registry.save_registry(projects, p)
    return p


def _abs(repo):
    """Expand `~` the way the registry does (projects store absolute repos)."""
    return registry._expand(repo)


EXP = _abs  # shorthand: EXP("~/dev/eco") -> "/Users/.../dev/eco"


def test_board_statuses_reported_per_project_exists_missing_unset():
    """Each registered project is classified EXISTS / MISSING / UNSET."""
    projects = [
        T(name="hscc", repo="~/dev/hscc", board="hscc"),        # exists
        T(name="eco", repo="~/dev/eco", board="eco-missing"),   # missing
        T(name="sphoin", repo="~/dev/sphoin", board=None),      # unset
    ]
    report = sync_cmd.run_sync(
        repos=[], topics=[], boards=["hscc"], projects=projects,
    )
    by_name = {s.name: s for s in report.board_statuses}
    assert by_name["hscc"].status == "exists"
    assert by_name["eco"].status == "missing"
    assert by_name["sphoin"].status == "unset"
    # target slug is the normalized project name
    assert by_name["sphoin"].target_slug == "sphoin"


def test_plan_board_creation_skips_existing_picks_missing_and_unset():
    """plan_board_creation creates exactly the lacking boards, never an
    existing one, and never a slug that collides with an existing board."""
    projects = [
        T(name="hscc", repo="~/dev/hscc", board="hscc"),        # has board
        T(name="eco", repo="~/dev/eco", board="eco-gone"),      # missing
        T(name="sphoin", repo="~/dev/sphoin", board=None),      # unset
    ]
    conflicts, to_create = sync_cmd.plan_board_creation(projects, boards=["hscc"])
    assert [s.name for s in to_create] == ["eco", "sphoin"]
    assert to_create[0].target_slug == "eco"
    assert to_create[1].target_slug == "sphoin"
    assert conflicts == []


def test_apply_create_boards_creates_missing_with_slug_and_workdir(tmp_path):
    """--create-boards --apply calls create_board with slug=name and
    default_workdir=repo for exactly the lacking projects, and writes
    `board:` into each registry entry."""
    reg = _reg_write(tmp_path, [
        T(name="hscc", repo="~/dev/hscc", board="hscc"),
        T(name="eco", repo="~/dev/eco", board=None),
        T(name="sphoin", repo="~/dev/sphoin", board=None),
    ])
    kdb = FakeKdb()
    _, to_create = sync_cmd.plan_board_creation(
        registry.load_registry(reg), boards=["hscc"]
    )
    created = sync_cmd.apply_create_boards(to_create, reg, _kdb=kdb)

    assert created == ["eco", "sphoin"]
    # Board created with the right slug, name, and default_workdir (the repo).
    assert kdb.created == [
        {"slug": "eco", "name": "eco", "default_workdir": EXP("~/dev/eco")},
        {"slug": "sphoin", "name": "sphoin", "default_workdir": EXP("~/dev/sphoin")},
    ]
    # `board:` written into each project's registry entry.
    assert registry.get_project("eco", reg).board == "eco"
    assert registry.get_project("sphoin", reg).board == "sphoin"
    assert registry.get_project("hscc", reg).board == "hscc"


def test_create_boards_without_apply_creates_nothing(tmp_path, capsys):
    """--create-boards alone (no --apply) reports the plan but never creates."""
    import argparse
    from flightdeck.commands import sync as mod

    reg = _reg_write(tmp_path, [
        T(name="hscc", repo="~/dev/hscc", board=None),
    ])
    kdb = FakeKdb()
    args = argparse.Namespace(
        project_cmd="sync",
        apply=False,
        json=False,
        create_boards=True,
        repos=["~/dev/hscc"],
        _run=None,
        _boards={},
        _kdb=kdb,
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=None,
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: []
    mod.discover_topics = lambda _client=None: []

    rc = sync_cmd.cmd_sync(args)
    out = capsys.readouterr().out
    assert rc == 0
    # The plan is shown...
    assert "BOARDS" in out
    assert "UNSET" in out
    # ...but nothing was created and no registry board was written.
    assert kdb.created == []
    assert registry.get_project("hscc", reg).board is None


def test_create_boards_apply_is_idempotent_on_rerun(tmp_path, capsys):
    """Re-running --create-boards --apply after boards exist creates nothing."""
    import argparse
    from flightdeck.commands import sync as mod

    reg = _reg_write(tmp_path, [
        T(name="hscc", repo="~/dev/hscc", board=None),
        T(name="eco", repo="~/dev/eco", board=None),
    ])
    kdb = FakeKdb()

    def run_cmd(boards):
        args = argparse.Namespace(
            project_cmd="sync",
            apply=True,
            json=False,
            create_boards=True,
            repos=["~/dev/hscc", "~/dev/eco"],
            _run=None,
            _boards={},
            _kdb=kdb,
            client=None,
            roots=None,
            registry=reg,
            ignore_topic=None,
        )
        mod.discover_repos = lambda roots=None, _run=None: args.repos
        mod.discover_boards = lambda: boards
        mod.discover_topics = lambda _client=None: []
        return sync_cmd.cmd_sync(args)

    # First run: no boards exist -> create both.
    rc1 = run_cmd(boards=[])
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert kdb.created == [
        {"slug": "eco", "name": "eco", "default_workdir": EXP("~/dev/eco")},
        {"slug": "hscc", "name": "hscc", "default_workdir": EXP("~/dev/hscc")},
    ]
    assert registry.get_project("hscc", reg).board == "hscc"

    # Second run: boards now exist -> nothing created, nothing re-bound.
    kdb.created.clear()
    rc2 = run_cmd(boards=["hscc", "eco"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert kdb.created == []
    assert registry.get_project("hscc", reg).board == "hscc"


def test_slug_collision_with_other_project_board_reported_conflict_and_skipped(tmp_path, capsys):
    """A project whose desired slug is already an existing board owned by a
    DIFFERENT project is reported as a conflict and skipped — never adopted."""
    import argparse
    from flightdeck.commands import sync as mod

    # The 'hscc' Hermes board already exists and is owned by project `other`.
    # Project `hscc` (unset) therefore WANTS slug 'hscc' -> collision -> skip.
    # Project `report` (unset) wants slug 'report' (free) -> created.
    reg = _reg_write(tmp_path, [
        T(name="other", repo="~/dev/other", board="hscc"),
        T(name="hscc", repo="~/dev/hscc", board=None),      # collides with 'other'
        T(name="report", repo="~/dev/report", board=None),  # free slug
    ])
    kdb = FakeKdb()
    args = argparse.Namespace(
        project_cmd="sync",
        apply=True,
        json=False,
        create_boards=True,
        repos=["~/dev/other", "~/dev/hscc", "~/dev/report"],
        _run=None,
        _boards={},
        _kdb=kdb,
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=None,
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: ["hscc"]
    mod.discover_topics = lambda _client=None: []

    rc = sync_cmd.cmd_sync(args)
    out = capsys.readouterr().out
    # `report` has a free slug -> created; the colliding `hscc` is NOT created.
    assert kdb.created == [
        {"slug": "report", "name": "report", "default_workdir": EXP("~/dev/report")}
    ]
    assert "conflict" in out
    assert "hscc" in out
    # rc nonzero: the operator must resolve the collision (never silently).
    assert rc == 1
    # hscc not bound to the other project's board, its registry untouched.
    assert registry.get_project("hscc", reg).board is None
    # The board already owned by `other` is never stolen or re-adopted.
    assert registry.get_project("other", reg).board == "hscc"

    # Uncollided `report` IS bound to its new board in the registry.
    assert registry.get_project("report", reg).board == "report"


def test_no_card_is_read_moved_or_modified_by_board_creation(tmp_path):
    """create_board never touches the board library's card methods, and only
    writes the single `board:` field to the registry — no card data."""
    import argparse
    from flightdeck.commands import sync as mod

    reg = _reg_write(tmp_path, [
        T(name="hscc", repo="~/dev/hscc", board=None),
    ])
    kdb = FakeKdb()
    args = argparse.Namespace(
        project_cmd="sync",
        apply=True,
        json=False,
        create_boards=True,
        repos=["~/dev/hscc"],
        _run=None,
        _boards={},
        _kdb=kdb,
        client=None,
        roots=None,
        registry=reg,
        ignore_topic=None,
    )
    mod.discover_repos = lambda roots=None, _run=None: args.repos
    mod.discover_boards = lambda: []
    mod.discover_topics = lambda _client=None: []

    rc = sync_cmd.cmd_sync(args)
    assert rc == 0
    # Only the board field changed in the registry — repo/topic untouched.
    p = registry.get_project("hscc", reg)
    assert p.board == "hscc"
    assert p.repo == registry._expand("~/dev/hscc")
    # FakeKdb raises if any card read/move method is touched -> already enforced.


def test_board_statuses_present_in_json(tmp_path):
    report = sync_cmd.run_sync(
        repos=[],
        topics=[],
        boards=["hscc"],
        # statuses are sorted by project name: 'eco' then 'hscc'
        projects=[T(name="hscc", repo="~/dev/hscc", board="hscc"),
                  T(name="eco", repo="~/dev/eco", board=None)],
    )
    import json
    payload = json.loads(sync_cmd._render_json(report))
    assert payload["boards"]["statuses"][0]["status"] == "unset"  # eco
    assert payload["boards"]["statuses"][1]["status"] == "exists"  # hscc
    assert payload["boards"]["statuses"][0]["target_slug"] == "eco"


def test_render_shows_board_block_and_existing_cards_stay_wording():
    """The rendered proposal includes the BOARDS block with the plan and the
    'existing cards stay on their current board' note."""
    report = sync_cmd.run_sync(
        repos=[],
        topics=[],
        boards=["hscc"],
        projects=[T(name="hscc", repo="~/dev/hscc", board=None)],
    )
    out = sync_cmd.render(report)
    assert "BOARDS" in out
    assert "UNSET" in out
    assert "existing cards stay on their current board." in out
