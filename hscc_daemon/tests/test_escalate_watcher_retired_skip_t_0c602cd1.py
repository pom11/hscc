"""The SUPERSEDED — DO NOT DISPATCH retirement skip (t_0c602cd1).

Live incident the file exists for: at 2026-11-23 07:45 the escalate watcher
sent "HSCC task t_0f214ba0 needs a HUMAN: strong tier is also failing" for a
card @hscc-orch had already dispositioned — three runs had crashed in ~60 s each,
the orch retired it (re-cut as t_1700dfc8), commented
``SUPERSEDED — DO NOT DISPATCH.`` and froze it ``blocked`` as the crash artifact
of record. A frozen blocked card with a high ``consecutive_failures`` is exactly
the shape ``decide_escalation`` reads as "the strong tier is also failing", so
the watcher kept paging a human about a card nobody would ever dispatch again,
every 15-minute tick.

These cases pin three things and nothing else:

  (a) escalation is NOT weakened — a crashed/gave_up card without the marker
      still announces, on the same board as a retired one;
  (b) a card whose comments carry the marker is silent: no action, no notify,
      no reassign;
  (c) retirement is per-card state, not a lifetime mute — a run that FAILED
      after the marker was written revives the card and re-announces.

Fixtures are sandboxed board DBs under ``tmp_path`` — a real sqlite file holding
the kanban columns the watcher's queries read, reached through an injected
``_kb``. NEVER the operator's live ``~/.hermes`` board (same doctrine as
``test_kanban_blocked.py::_FakeKb``). Because the fake executes REAL SQL against
a real schema, these cases also pin the column names and the parameter binding
of the two new queries; an injected-rows fake cannot catch a typo'd column.
"""

import contextlib
import sqlite3

import pytest

from hscc_daemon import escalate_watcher as ew
from hscc_daemon.escalate_watcher import scan_and_escalate


# ── fixture kit ─────────────────────────────────────────────────────────────

#: The phrase @hscc-orch writes on a retired card. Spelled with the em dash it
#: actually uses; ``\u2014`` keeps the glyph unambiguous in this file.
MARK = "SUPERSEDED \u2014 DO NOT DISPATCH"

#: The first sentence of the real comment on t_0f214ba0 (trimmed, no ids that
#: matter to the matcher) — the shape the matcher has to hit in production.
INCIDENT_COMMENT = (
    MARK + ". This card crashed three runs (2036-2038) in ~60 s each with "
    "'Kanban owner refused: Kanban execution unknown; retained by owner'; "
    "Re-cut byte-equivalent body WITHOUT the tag: t_1700dfc8. "
    "Keep this card blocked as the crash artifact of record."
)

T0 = 1_795_412_000          # board clock base (epoch s) — arbitrary but fixed
MARK_AT = T0 + 700           # the orch comments AFTER the crash it retires for
BEFORE_MARK = T0 + 500       # a failed run that predates the marker (the cause)
AFTER_MARK = T0 + 1500       # a failed run that postdates it (the card revived)

#: The kanban columns the watcher's three queries name. Declared here, not
#: imported from Hermes, so the tests do not need Hermes on the path — and
#: narrowed to what this module reads, so a rename upstream shows up as a
#: failing test rather than a silently skipped board.
SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0, last_failure_error TEXT);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
    body TEXT, created_at INTEGER);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, status TEXT,
    outcome TEXT, started_at INTEGER, ended_at INTEGER);
"""


class _RecordingConn:
    """sqlite3.Connection proxy that logs every (sql, params) the watcher sends.

    The cheapness pins need it: "fetch comments for the candidate set only" is
    only testable if the binding is visible.
    """

    def __init__(self, conn):
        self._conn = conn
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), tuple(params or ())))
        return self._conn.execute(sql, tuple(params or ()))

    def close(self):
        self._conn.close()


class BoardKB:
    """Fake ``hermes_cli.kanban_db`` over a sandboxed sqlite file."""

    def __init__(self, path, *, with_comments=True, with_runs=True):
        self.path = str(path)
        self.connects = 0
        self.last_conn = None
        self._missing = set()
        if not with_comments:
            self._missing.add("task_comments")
        if not with_runs:
            self._missing.add("task_runs")

    @contextlib.contextmanager
    def connect_closing(self):
        self.connects += 1
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        for table in self._missing:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        rec = _RecordingConn(conn)
        self.last_conn = rec
        try:
            yield rec
        finally:
            conn.close()


def make_board(tmp_path, *, tasks=(), comments=(), runs=(), name="kanban.db"):
    """Write a sandboxed board. ``tasks``/``comments``/``runs`` are tuples."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for (tid, assignee, failures, status, error) in tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, "
            "consecutive_failures, last_failure_error) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, f"card {tid}", assignee, status, failures, error))
    for (tid, body, at) in comments:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)", (tid, "hscc-orch", body, at))
    for (tid, outcome, ended, started) in runs:
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, "
            "ended_at) VALUES (?, ?, ?, ?, ?)",
            (tid, outcome, outcome, started, ended))
    conn.commit()
    conn.close()
    return path


#: The real crash signature of the incident card (classify_failure -> "other",
#: which is the category the false page carried).
CRASH = ("pid 19115 exited with code 1 [managed worker lost: "
         "managed_worker_failed] Kanban owner refused")


def crashed_card(tid="t-crashed", assignee="devops-engineer", failures=3):
    """A card in the exact state that paged the human: at threshold, blocked."""
    return (tid, assignee, failures, "blocked", CRASH)


def scan(board, *, notified=None, strong_profile=None, fail_limit=3):
    """One watcher pass with the side-effecting seams recorded."""
    notifications, reassignments = [], []
    actions = scan_and_escalate(
        fail_limit=fail_limit,
        strong_profile=strong_profile,
        _kb=board,
        _reassign=lambda tid, to: reassignments.append((tid, to)) or True,
        _notify=lambda title, body: notifications.append((title, body)),
        _notified=notified if notified is not None else set(),
    )
    return actions, notifications, reassignments


# ── (a) escalation is NOT weakened ──────────────────────────────────────────

class TestEscalationStillFires:
    """The skip must not cost a live card its page."""

    def test_crashed_card_without_marker_still_announces(self, tmp_path):
        """(a) REGRESSION: the incident card's state, no marker -> HUMAN alert.

        This is the same row the 07:45 notification fired on. Strip the marker
        and the watcher must still page.
        """
        board = BoardKB(make_board(tmp_path, tasks=[crashed_card()]))

        actions, notifications, reassignments = scan(board)

        assert [a["action"] for a in actions] == ["human"]
        assert actions[0]["task"] == "t-crashed"
        assert len(notifications) == 1
        assert "t-crashed" in notifications[0][0]
        assert reassignments == []       # acting reassign stays opt-in

    def test_retired_sibling_does_not_weaken_the_live_card(self, tmp_path):
        """A retired card on the same board must not swallow the live one."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card("t-retired"), crashed_card("t-live")],
            comments=[("t-retired", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-retired", "crashed", BEFORE_MARK, BEFORE_MARK - 60)],
        ))

        actions, notifications, _ = scan(board)

        assert [a["task"] for a in actions] == ["t-live"]
        assert len(notifications) == 1
        assert "t-live" in notifications[0][0]

    def test_marker_never_touches_threshold_or_tier(self, tmp_path):
        """The at-threshold reassign path is unchanged for a card without one."""
        board = BoardKB(make_board(
            tmp_path, tasks=[crashed_card("t-live", assignee="backend-engineer")]))

        actions, _, reassignments = scan(board, strong_profile="architect")

        assert actions[0]["action"] == "escalate"
        assert reassignments == [("t-live", "architect")]


# ── (b) a retired card is silent ────────────────────────────────────────────

class TestRetiredCardIsSilent:
    """The marker is the orchestrator's disposition; the watcher must obey it."""

    def test_marker_makes_the_card_silent(self, tmp_path):
        """(b) Same card, marker present -> no action, no notify, no reassign."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-crashed", "crashed", BEFORE_MARK, BEFORE_MARK - 60),
                  ("t-crashed", "crashed", BEFORE_MARK + 60, BEFORE_MARK)],
        ))

        actions, notifications, reassignments = scan(board)

        assert actions == []
        assert notifications == []
        assert reassignments == []

    def test_no_reassign_either(self, tmp_path):
        """Skip = no announce AND no reassign, even with acting escalation on.

        A retired card reassigned to the strong tier would be re-dispatched,
        which is precisely what DO NOT DISPATCH forbids.
        """
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card(assignee="backend-engineer")],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
        ))

        actions, notifications, reassignments = scan(board, strong_profile="architect")

        assert actions == []
        assert notifications == []
        assert reassignments == []

    def test_hyphen_instead_of_em_dash_still_mutes(self, tmp_path):
        """The dash is typed by hand; a hyphen must not cost the operator a page.

        The fold is about the WORDS: missing this variant re-creates the exact
        false page this card exists to stop.
        """
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", "SUPERSEDED - DO NOT DISPATCH. re-cut as "
                                    "t_1700dfc8.", MARK_AT)],
        ))

        assert scan(board)[0] == []

    def test_case_and_wrapping_do_not_matter(self, tmp_path):
        """A marker wrapped over lines / uppercased by habit still counts."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", "  superseded —\n  DO NOT   DISPATCH (re-cut) ",
                       MARK_AT)],
        ))

        assert scan(board)[0] == []

    def test_partial_phrase_does_not_mute(self, tmp_path):
        """Only the whole phrase disposes of a card — no partial-match slop.

        Both halves must be present and adjacent, or a comment that merely says
        "this supersedes X" or "do not dispatch yet" would silence a card that
        still needs a human.
        """
        bodies = ("SUPERSEDED by t_1700dfc8, still needs review",
                  "DO NOT DISPATCH this card until CI is green",
                  "SUPERSEDED ... eventually ... DO NOT",
                  "SUPERSEDED \u2014 DO NOT DISPATCHING that worker again")
        for i, body in enumerate(bodies):
            board = BoardKB(make_board(
                tmp_path, tasks=[crashed_card()],
                comments=[("t-crashed", body, MARK_AT)], name=f"partial-{i}.db"))

            actions, notifications, _ = scan(board)

            assert [a["action"] for a in actions] == ["human"], body
            assert len(notifications) == 1, body

    def test_marker_on_another_card_does_not_mute_this_one(self, tmp_path):
        """Retirement is per-card: the query is scoped by task_id."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card("t-retired"), crashed_card("t-live")],
            comments=[("t-retired", INCIDENT_COMMENT, MARK_AT)],
        ))

        actions, _, _ = scan(board)

        assert [a["task"] for a in actions] == ["t-live"]

    def test_marker_written_by_any_author_counts(self, tmp_path):
        """The disposition is what matters, not who typed it."""
        path = make_board(tmp_path, tasks=[crashed_card()])
        board = BoardKB(path)
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t-crashed", "operator", MARK + " (frozen by hand)", MARK_AT))
        conn.commit()
        conn.close()

        assert scan(board)[0] == []


# ── (c) retirement is per-card state, not a lifetime mute ───────────────────

class TestRevivalReannounces:
    """Unblocked + dispatched again + failed again => page again."""

    def test_new_failure_after_marker_reannounces(self, tmp_path):
        """(c) marker, then a run that failed AFTER it -> the card is live."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-crashed", "crashed", BEFORE_MARK, BEFORE_MARK - 60),
                  # the orch unblocked it, it was claimed again and crashed:
                  ("t-crashed", "crashed", AFTER_MARK, MARK_AT + 60)],
        ))

        actions, notifications, _ = scan(board)

        assert [a["action"] for a in actions] == ["human"]
        assert len(notifications) == 1
        assert "t-crashed" in notifications[0][0]

    def test_failure_before_the_marker_keeps_it_mute(self, tmp_path):
        """The boundary: the crashes the card was retired FOR are not evidence.

        Without the timestamp comparison this card is un-muted on the next tick
        and the operator is back where they were — the whole point of using the
        comment's own created_at as the line.
        """
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-crashed", "crashed", BEFORE_MARK, BEFORE_MARK - 60),
                  ("t-crashed", "gave_up", BEFORE_MARK + 60, BEFORE_MARK)],
        ))

        assert scan(board)[0] == []

    def test_only_a_FAILED_run_revives_it(self, tmp_path):
        """A completed or merely blocked later run is not a failure."""
        for outcome in ("completed", "blocked"):
            board = BoardKB(make_board(
                tmp_path,
                tasks=[crashed_card()],
                comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
                runs=[("t-crashed", outcome, AFTER_MARK, MARK_AT + 60)],
                name=f"{outcome}.db"))

            assert scan(board)[0] == [], outcome

    def test_newest_marker_is_the_boundary(self, tmp_path):
        """Retired twice: the SECOND retirement mutes failures the first allowed.

        A card retired, revived, then retired again must be silent for the runs
        that predate the newer comment.
        """
        second_mark = T0 + 2000
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT),
                      ("t-crashed", INCIDENT_COMMENT, second_mark)],
            runs=[("t-crashed", "crashed", AFTER_MARK, MARK_AT + 60)],
        ))

        assert scan(board)[0] == []

    def test_revival_then_reannounce_is_still_deduped(self, tmp_path):
        """Dedupe behaviour is unchanged: a revived card pages ONCE per episode.

        Two passes over the same revived board notify once (the second sees the
        id in ``_notified``), exactly as a never-retired card behaves.
        """
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-crashed", "crashed", AFTER_MARK, MARK_AT + 60)],
        ))
        notified = set()

        _, first, _ = scan(board, notified=notified)
        _, second, _ = scan(board, notified=notified)

        assert len(first) == 1
        assert second == []          # already announced this episode
        assert len(notified) == 1


# ── the two new reads: cheap, same connection, fail in the safe direction ───

class TestAdvisoryReads:
    """The skip must cost the tick nothing it did not already pay, and must
    never turn a broken read into a muted escalation."""

    def test_comments_are_read_for_the_candidate_set_only(self, tmp_path):
        """The extra read is bounded by the cards a tick can act on, not the board."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card("t-live"),
                   ("t-clean", "coder", 0, "running", None),        # not failing
                   ("t-done", "coder", 9, "done", CRASH),           # not dispatchable
                   ("t-draft", "coder", 9, "draft", CRASH)],        # not dispatchable
            comments=[("t-clean", INCIDENT_COMMENT, MARK_AT),
                      ("t-done", INCIDENT_COMMENT, MARK_AT),
                      ("t-draft", INCIDENT_COMMENT, MARK_AT),
                      ("t-live", "just an update", MARK_AT)],
        ))

        actions, notifications, _ = scan(board)

        assert [a["task"] for a in actions] == ["t-live"]
        assert board.last_conn is not None
        comment_queries = [q for q in board.last_conn.queries
                           if "task_comments" in q[0]]
        assert len(comment_queries) == 1
        assert set(comment_queries[0][1]) == {"t-live"}   # candidates ONLY

    def test_runs_query_is_scoped_to_retired_ids(self, tmp_path):
        """The revival query asks only about cards that carry a marker."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card("t-live"), crashed_card("t-retired")],
            comments=[("t-retired", INCIDENT_COMMENT, MARK_AT)],
        ))

        scan(board)

        assert board.last_conn is not None
        run_queries = [q for q in board.last_conn.queries if "task_runs" in q[0]]
        assert len(run_queries) == 1
        # params are the retired ids plus the fixed outcome list; only the ids
        # are interesting here.
        assert set(run_queries[0][1][:1]) == {"t-retired"}

    def test_one_connection_for_the_whole_pass(self, tmp_path):
        """Both reads ride the connection the scan already opened."""
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
            runs=[("t-crashed", "crashed", AFTER_MARK, MARK_AT + 60)],
        ))

        scan(board)

        assert board.connects == 1

    def test_no_candidates_reads_no_comments(self, tmp_path):
        """An idle tick stays a single cheap query — the common case."""
        board = BoardKB(make_board(
            tmp_path, tasks=[("t-clean", "coder", 0, "running", None)]))

        actions, _, _ = scan(board)

        assert actions == []
        assert board.last_conn is not None
        assert len(board.last_conn.queries) == 1
        sql = board.last_conn.queries[0][0]
        assert "task_comments" not in sql and "task_runs" not in sql

    def test_unreadable_comments_fail_open_to_escalating(self, tmp_path):
        """No task_comments table -> escalate as before.

        Direction is deliberate: a broken read that MUTED escalations would
        trade a false page for a silent real failure. Here the watcher loses the
        skip and keeps its job.
        """
        board = BoardKB(make_board(tmp_path, tasks=[crashed_card()]),
                        with_comments=False)

        actions, notifications, _ = scan(board)

        assert [a["action"] for a in actions] == ["human"]
        assert len(notifications) == 1

    def test_unreadable_runs_keeps_a_retired_card_mute(self, tmp_path):
        """No task_runs table -> the marker still silences the card.

        The other direction, same rule: a card is un-muted only by POSITIVE
        evidence of a newer failure, never by the absence of a readable log.
        """
        board = BoardKB(make_board(
            tmp_path,
            tasks=[crashed_card()],
            comments=[("t-crashed", INCIDENT_COMMENT, MARK_AT)],
        ), with_runs=False)

        assert scan(board)[0] == []


# ── the marker itself ───────────────────────────────────────────────────────

class TestMarkerContract:
    """The phrase is a convention between @hscc-orch and this watcher."""

    def test_the_phrase_is_the_one_the_orchestrator_writes(self):
        """Pinned verbatim: a source edit to the words silently un-mutes cards."""
        assert ew.RETIRED_MARKER == "SUPERSEDED \u2014 DO NOT DISPATCH"

    def test_fold_folds_dashes_case_and_whitespace(self):
        assert ew._fold(ew.RETIRED_MARKER) == ew._MARKER_FOLDED
        assert ew._fold("superseded --\n DO  NOT DISPATCH") != ew._MARKER_FOLDED
        assert ew._fold("SUPERSEDED \u2013 DO NOT DISPATCH") == ew._MARKER_FOLDED
        assert ew._fold("") == ""
        assert ew._fold(None) == ""

    def test_phrase_match_is_the_boundary_between_mute_and_page(self):
        """The matcher alone decides — pinned on both sides, at the unit level."""
        hits = (INCIDENT_COMMENT,
                MARK,
                "SUPERSEDED \u2014 do not dispatch.",
                'the orch wrote "SUPERSEDED \u2014 DO NOT DISPATCH" and re-cut it',
                "  superseded \u2014\n DO NOT  DISPATCH  (frozen) ")
        misses = ("", None, "SUPERSEDED by t_1700dfc8",
                  "DO NOT DISPATCH this until CI is green",
                  "SUPERSEDED \u2014 DO NOT DISPATCHING that worker",
                  "this supersedes the earlier card")
        for body in hits:
            assert ew._is_retirement_comment(body), repr(body)
        for body in misses:
            assert not ew._is_retirement_comment(body), repr(body)

    def test_blocked_run_is_not_a_failure(self):
        """A block is a disposition, not a failure — pin the outcome list."""
        assert "blocked" not in ew._FAILED_RUN
        assert "completed" not in ew._FAILED_RUN
        assert {"crashed", "timed_out", "gave_up"} <= set(ew._FAILED_RUN)
