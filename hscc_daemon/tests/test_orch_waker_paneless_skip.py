"""t_7b2d3dba — the PANE-LESS SKIP: a board whose orchestrator target can never
answer stops being retried forever, and its batch drains into a named skip state.

Measured on the live daemon: board ``general`` emitted 138 byte-identical audit
lines, ``failed: no tmux pane running a Hermes REPL for profile 'general-orch'``,
one every 30 s, with ``pending`` never draining. The target is not a dead pane —
it is the retired cluster-wide identity, and ``hscc-roles/main_orch.py`` says the
directory is kept on disk *on purpose* ("every pre-migration install still has
``general-orch`` on disk (retired, never deleted)"). ``resolve_orch`` approves a
profile on DIRECTORY EXISTENCE ALONE, so it approved a profile that has never had
a pane and has never been dispatched (measured: ``general-orch`` appears in zero
``task_runs`` rows on every board; the same board's orchestrator work is
dispatcher-dispatched).

The fix is a pane-expectation verdict with two independent halves, and each half
is useless — dangerous, even — without the other:

  (A) a pane sweep that SUCCEEDED counted ZERO panes for the target, and
  (B) hscc's OWN retirement record says no pane is expected for that profile.

So this suite pins all four quadrants plus the consequences:

  * (A)+(B)  -> SKIPPED: pending DRAINED, no inject, no audit line, no wake
    claim, no decision; one log line per ``backoff_s`` window; the skipped set
    stamped on the stream so a permanently-muted board can never read ``steady``.
  * (A) only -> the ordinary dead-pane case: t_f1122b7a's retry/backoff is
    untouched, attempts still attempted and audited (an operator's pane that
    comes back must still get its batch).
  * (B) only -> a retired profile WITH a pane is still nudged: a label never
    switches off a delivery that lands.
  * neither, or a BLIND sweep -> nudged / unknown-never-skips.

And the difference the card demanded be MEASURED, not asserted: dispatch keeps
working on a skipped board. The skip writes nothing at all — no board write, no
``kanban_wake_claims`` row (a claim held by a waker that will not speak would
mute the gateway notifier for the whole lease), no config entry. ``skip_boards``
stays empty for that board, because that knob feeds the ``paused`` set and would
mute the board wholesale.

Offline like every other waker suite: tmp boards/profiles/audit, an injected
clock, a stub repl. No tmux, no live board, no real ``~/.hscc``/``~/.hermes``.
"""

import hashlib
import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000
#: The delivery-decision clock. `seeded` below lets the board first-sight at NOW
#: and sees the event at NOW+10, so this is DEBOUNCE_S (90) past the moment the
#: batch started — and MIN_GAP_S is inert because nothing has ever been sent.
DELIVER = NOW + 100
BOARD = "general"
TARGET = "general-orch"      # what ow.orch_name("general") resolves to
TASK = "t_0f1e2d3c"
MAIN = "general"             # the main orchestrator (profile AND board)
RETIRED_MARK = "retired \u2014 use"
RETIRED_SOUL = ("## Retired \u2014 use `general`\n\n"
                "This profile is **retired \u2014 use general**. It is kept on "
                "disk only for history \u2014 do not create, dispatch or claim "
                "work here.\n")
LIVE_SOUL = "You are the orchestrator for this project.\n"

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
    claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
    current_run_id INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER);
CREATE TABLE kanban_notify_subs (id INTEGER PRIMARY KEY AUTOINCREMENT, channel
  TEXT, thread_id TEXT, task_id TEXT, notifier_profile TEXT, event TEXT,
  min_run INTEGER NOT NULL DEFAULT 0, last_event_id INTEGER NOT NULL DEFAULT 0,
  last_run INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER, updated_at INTEGER);
CREATE TABLE kanban_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
  PRIMARY KEY (parent_id, child_id));
CREATE TABLE kanban_workers (id TEXT PRIMARY KEY, pid INTEGER, status TEXT,
  started_at INTEGER, last_heartbeat INTEGER);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, stored_name TEXT, content_type TEXT, size INTEGER,
  sha256 TEXT, uploaded_by TEXT, uploaded_at INTEGER);
CREATE TABLE kanban_wake_claims (
    profile       TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_id      INTEGER NOT NULL,
    emitter       TEXT NOT NULL,
    claimed_at    INTEGER NOT NULL,
    confirmed_at  INTEGER,
    PRIMARY KEY (profile, task_id, event_id));
"""


# ── fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hscc-state" / "orch_waker.json")


def make_board(path, tasks=(), events=()):
    """A tmp board DB with the dispatcher's OWN claim columns present, so a
    write into the dispatch path would be visible rather than invisible."""
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO tasks (id, title, assignee, status, claim_lock, "
        "claim_expires, worker_pid, current_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(t[0], t[1], t[2] if len(t) > 2 else "backend-engineer",
          t[3] if len(t) > 3 else "ready", None, None, None, None)
         for t in tasks])
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(t, r, k, p, NOW) for (t, r, k, p) in events])
    conn.commit()
    conn.close()
    return path


def add_events(path, events):
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(t, r, k, p, NOW) for (t, r, k, p) in events])
    conn.commit()
    conn.close()


def claims(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT profile, task_id, event_id, emitter, "
                            "confirmed_at FROM kanban_wake_claims").fetchall()
    finally:
        conn.close()
    return rows


def task_row(path, task_id):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def digest(path):
    with open(str(path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_profiles(tmp_path, **souls):
    """profiles_dir with one dir per name; SOUL.md written when given."""
    pdir = tmp_path / "profiles"
    for name, soul in souls.items():
        (pdir / name).mkdir(parents=True, exist_ok=True)
        if soul is not None:
            (pdir / name / "SOUL.md").write_text(soul, encoding="utf-8")
    return str(pdir)


def audit_lines(path):
    if not os.path.exists(str(path)):
        return []
    with open(str(path)) as f:
        return [json.loads(line) for line in f if line.strip()]


class StubInject:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        return self.ok


class FakeRepl:
    """The repl_inject parity module: a pane inventory plus delivery."""

    def __init__(self, panes=(), raises=None, inject_raises=None):
        self.panes = list(panes)
        self.raises = raises
        self.inject_raises = inject_raises
        self.sweeps = 0
        self.injects = []

    def hermes_panes(self):
        self.sweeps += 1
        if self.raises is not None:
            raise self.raises
        return list(self.panes)

    def inject(self, profile, text, mode="auto"):
        self.injects.append((profile, text, mode))
        if self.inject_raises is not None:
            raise self.inject_raises


def pane(profile, target="0:1.0"):
    return {"pane": "%%" + target, "target": target, "tty": "/dev/ttys0",
            "profile": profile, "cmd": f"hermes -p {profile} chat"}


class InjectErrorLike(Exception):
    """Carries `.kind` like repl_inject.InjectError (no import coupling)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class RecordingClaims:
    """The wake-claim table's four methods, recording every call."""

    def __init__(self):
        self.calls = []

    def _rec(self, name, db, profile, idents):
        self.calls.append((name, os.path.basename(str(db))))
        return []

    def suppressions(self, db, profile, idents):
        self._rec("suppressions", db, profile, idents)
        return []

    def take(self, db, profile, idents):
        self._rec("take", db, profile, idents)
        return list(idents)

    def confirm(self, db, profile, idents):
        self._rec("confirm", db, profile, idents)

    def release(self, db, profile, idents):
        self._rec("release", db, profile, idents)


def seeded(tmp_path, state_path, souls=None, board=BOARD, tasks=None,
           events=True):
    """Tail-start a board; with ``events`` also age one event past the debounce.

    The tail-start tick is NOT optional: it is what stamps the board's watermark,
    and a test that skipped it would have its first batch swallowed as
    "pre-existing backlog" — which reads as "the waker did nothing" for a reason
    that has nothing to do with what the test is measuring.
    """
    db = make_board(tmp_path / board / "kanban.db",
                    tasks=tasks or [(TASK, "Pane-less skip fixture card")])
    ow.tick(now=NOW, boards={board: db}, state_path=state_path)
    if events:
        add_events(db, [(TASK, 1, "completed", None)])
        ow.tick(now=NOW + 10, boards={board: db}, state_path=state_path)
    return db


def queue(db, state_path, task, t, board=BOARD):
    """Put one event on the board and let the scan tick at ``t`` see it.

    Returns the moment that batch is ELIGIBLE (aged past ``debounce_s``). A scan
    tick with no ``inject`` only batches (the W1 report-only contract), so this
    costs no delivery and no claim — which is what makes each iteration of a
    multi-tick test its own batch instead of riding on the previous one.
    """
    add_events(db, [(task, 1, "completed", None)])
    ow.tick(now=t, boards={board: db}, state_path=state_path)
    return t + (ow.DEBOUNCE_S + 5)


def pending_n(state_path, board=BOARD):
    return len(ow.load_state(state_path)["pending"].get(board) or [])


def skip_rec(state_path, board=BOARD):
    return ow.paneless_state(ow.load_state(state_path)).get(board)


# ── the pure verdict: all four quadrants ────────────────────────────────────

def test_zero_panes_and_retired_is_the_only_skip():
    """(A)+(B) -> skip, and the reason is hscc's retirement record."""
    assert ow.paneless_reason(0, retired=True) == ow.REASON_RETIRED_PROFILE


def test_zero_panes_and_legacy_identity_skips_too():
    assert ow.paneless_reason(0, legacy_identity=True) == ow.REASON_LEGACY_IDENTITY


def test_zero_panes_alone_never_skips():
    """(A) alone is t_f1122b7a's dead-pane case: a live orchestrator whose pane
    is briefly down must KEEP retrying, or the batch is lost when it returns."""
    assert ow.paneless_reason(0) is None
    assert ow.paneless_reason(0, retired=False, legacy_identity=False) is None


def test_retired_with_a_pane_is_still_nudged():
    """(B) alone never blinds a delivery that WORKS: a retired profile with a
    pane open in tmux can be talked to, and a label must not switch that off."""
    assert ow.paneless_reason(1, retired=True) is None
    assert ow.paneless_reason(1, retired=True, legacy_identity=True) is None


def test_a_count_that_is_not_zero_is_not_a_fact_of_absence():
    """Two panes is ambiguous-to-resolve, not "no pane"; a nonsense count is
    neither. Only an exact zero is (A)."""
    for count in (2, 7, -1, 0.5):
        assert ow.paneless_reason(count, retired=True) is None


# ── the checker: cost discipline and the blindness rule ─────────────────────

def test_checker_sweeps_once_for_the_whole_pass():
    """One tmux sweep per tick, not one per board/profile."""
    repl = FakeRepl(panes=[pane("hscc-orch")])
    check = ow.make_paneless_checker(repl=repl)
    for board, profile in (("hscc", "hscc-orch"), ("grid", "grid-orch"),
                           ("kronos", "kronos-orch")):
        check(board, profile)
    assert repl.sweeps == 1


def test_blind_sweep_never_skips_even_a_retired_profile():
    """"Cannot see" is not "zero panes". This is the fail-open half of the whole
    design: a tmux outage must not silence every retired-target board at once."""
    repl = FakeRepl(raises=InjectErrorLike("ambiguous", "two live tmux servers"))
    check = ow.make_paneless_checker(repl=repl)
    assert check(BOARD, TARGET) is None


def test_missing_repl_module_is_blind_not_empty():
    class NoPanes:                            # a module that cannot answer
        pass

    check = ow.make_paneless_checker(repl=NoPanes())
    assert check(BOARD, TARGET) is None


def test_no_profile_file_is_read_when_a_pane_exists(monkeypatch, tmp_path):
    """Cost discipline: healthy boards must never touch a SOUL.md at all."""
    reads = []
    monkeypatch.setattr(ow, "profile_is_retired",
                        lambda name, *a, **k: reads.append(name) or False)
    repl = FakeRepl(panes=[pane("hscc-orch", "0:1.4")])
    check = ow.make_paneless_checker(repl=repl,
                                     profiles_dir=make_profiles(tmp_path))
    assert check("hscc", "hscc-orch") is None
    assert reads == []


def test_checker_skips_the_retired_target_and_leaves_the_live_one(tmp_path):
    """The real install's shape: `general-orch` retired with no pane, every
    other orch profile live with exactly one pane."""
    pdir = make_profiles(tmp_path, **{TARGET: RETIRED_SOUL, "hscc-orch": LIVE_SOUL,
                                      "grid-orch": LIVE_SOUL})
    repl = FakeRepl(panes=[pane("hscc-orch", "0:1.4"), pane("grid-orch", "0:1.9")])
    check = ow.make_paneless_checker(profiles_dir=pdir, repl=repl)
    assert check(BOARD, TARGET) == ow.REASON_RETIRED_PROFILE
    assert check("hscc", "hscc-orch") is None
    assert check("grid", "grid-orch") is None
    # an empty profile name is not a target worth a sweep-verdict
    assert check(BOARD, "") is None


# ── the two identity reads, against hscc's own records ──────────────────────

def test_profile_is_retired_reads_the_marker_casefolded(tmp_path):
    pdir = make_profiles(tmp_path, retired=RETIRED_SOUL, live=LIVE_SOUL,
                         empty="")
    assert ow.profile_is_retired("retired", profiles_dir=pdir) is True
    # migrate_main greps the marker against CASEFOLDED text — same rule here,
    # so a heading the operator's editor re-capitalised still counts.
    upper = tmp_path / "profiles" / "shouty"
    upper.mkdir(parents=True)
    (upper / "SOUL.md").write_text(
        RETIRED_SOUL.replace(RETIRED_MARK, RETIRED_MARK.upper()), encoding="utf-8")
    assert ow.profile_is_retired("shouty", profiles_dir=pdir) is True
    assert ow.profile_is_retired("live", profiles_dir=pdir) is False
    assert ow.profile_is_retired("empty", profiles_dir=pdir) is False
    # a profile with no SOUL.md, or a name that is not a profile: NOT a
    # retirement — this answer may only ever REMOVE a delivery, never add one
    assert ow.profile_is_retired("no-such-profile", profiles_dir=pdir) is False
    assert ow.profile_is_retired("", profiles_dir=pdir) is False


def test_legacy_identity_fires_only_on_the_main_board(tmp_path):
    """The identity-shaped half of (B): the retired cluster identities are
    retired for the board the main orchestrator owns, and nowhere else."""
    hsc = tmp_path / "hscc"
    hsc.mkdir()
    (hsc / "main-orchestrator.json").write_text(json.dumps({"name": MAIN}),
                                                encoding="utf-8")
    mod = ow._load_main_orch()
    assert mod is not None, "hscc-roles/main_orch.py must be importable"
    assert ow.legacy_cluster_identity(mod.LEGACY_GENERAL_PROFILE, MAIN,
                                      hscc_dir=str(hsc)) is True
    # the same legacy name on a project board is a different question
    assert ow.legacy_cluster_identity(mod.LEGACY_GENERAL_PROFILE, "hscc",
                                      hscc_dir=str(hsc)) is False
    # and a project's own orchestrator is never a legacy cluster identity
    assert ow.legacy_cluster_identity("hscc-orch", MAIN,
                                      hscc_dir=str(hsc)) is False
    # unknown resolver / junk input never skips
    assert ow.legacy_cluster_identity("", MAIN, hscc_dir=str(hsc)) is False
    assert ow.legacy_cluster_identity("x", "", hscc_dir=str(hsc)) is False


# ── the core gate ───────────────────────────────────────────────────────────

def test_skip_drains_and_writes_nothing(tmp_path, state_path):
    db = seeded(tmp_path, state_path)
    stub = StubInject()
    claims_before = claims(db)
    report = {}

    dec = ow.tick(now=DELIVER, boards={BOARD: db}, state_path=state_path,
                  inject=stub, report=report,
                  has_orch=lambda b: TARGET,
                  paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)

    assert dec == []                                   # no delivery decision
    assert stub.calls == []                            # nothing attempted
    # nothing claims the board was spoken to: no `sent` stamp, no audit line
    # (`sent` is what `min_gap_s` reads, so a skip must never borrow its credit)
    assert ow.load_state(state_path)["sent"].get(BOARD) is None
    assert pending_n(state_path) == 0                  # DRAINED, not accumulating
    assert report.get("paneless") == [{
        "board": BOARD, "profile": TARGET, "reason": ow.REASON_RETIRED_PROFILE,
        "ticks": 1, "since": DELIVER}]
    assert claims(db) == claims_before                 # claim table untouched
    assert skip_rec(state_path)["profile"] == TARGET


def test_gate_off_by_default_and_on_a_none_verdict(tmp_path, state_path):
    """No seam, or a None answer, = the pre-existing retry path, unchanged.

    Each variant gets its OWN batch and its OWN clock: a delivery drains
    ``pending`` and stamps ``sent`` (``min_gap_s``), which would otherwise
    suppress the second variant for reasons that have nothing to do with this
    test's subject.
    """
    db = seeded(tmp_path, state_path)
    stub = StubInject()
    for i, kwargs in enumerate(({}, {"paneless": lambda b, t: None})):
        due = queue(db, state_path, f"t_g{i}", DELIVER + 1000 * i)
        dec = ow.tick(now=due, boards={BOARD: db},
                      state_path=state_path, inject=stub,
                      has_orch=lambda b: TARGET, **kwargs)
        assert len(dec) == 1 and dec[0]["ok"] is True
        assert len(stub.calls) == i + 1
        assert skip_rec(state_path) is None


def test_one_log_line_per_window_and_ticks_are_counted(tmp_path, state_path,
                                                       caplog):
    """The metronome stops: 4 skipped batches, 1 warning, and `n` counts all 4.

    Each round puts its OWN batch on the board (:func:`queue`), because a tick
    with an empty batch never reaches the gate at all — it drops out earlier, on
    the pre-existing "nothing pending" rule — so a test that re-ticked the same
    drained board would be counting ticks that were never decisions.
    """
    db = seeded(tmp_path, state_path, events=False)      # tail-start only
    stub = StubInject()
    check = lambda b, t: ow.REASON_RETIRED_PROFILE    # noqa: E731

    first = DELIVER + ow.DEBOUNCE_S + 5               # the first eligible tick
    for i in range(4):
        due = queue(db, state_path, f"t_c{i}", DELIVER + 200 * i)
        assert due == first + 200 * i
        ow.tick(now=due, boards={BOARD: db}, state_path=state_path, inject=stub,
                has_orch=lambda b: TARGET, paneless=check)

    lines = [r for r in caplog.records
             if "no pane and none expected" in r.getMessage()]
    assert len(lines) == 1 and BOARD in lines[0].getMessage()
    assert stub.calls == []
    rec = skip_rec(state_path)
    assert rec["n"] == 4 and rec["at"] == first        # announced once, counted 4

    # The window expires: the operator is told again, and `n` keeps climbing.
    due = queue(db, state_path, "t_cX", DELIVER + 1000)
    ow.tick(now=due, boards={BOARD: db}, state_path=state_path, inject=stub,
            has_orch=lambda b: TARGET, paneless=check)
    assert len([r for r in caplog.records
                if "no pane and none expected" in r.getMessage()]) == 2
    assert skip_rec(state_path)["n"] == 5


def test_pending_never_accumulates_across_ticks(tmp_path, state_path):
    """THE acceptance criterion: the batch drains into the skip state instead of
    growing 9 lines deep forever."""
    db = seeded(tmp_path, state_path)
    stub = StubInject()
    check = lambda b, t: ow.REASON_RETIRED_PROFILE    # noqa: E731

    for i, t in enumerate((DELIVER, DELIVER + 30, DELIVER + 60), start=1):
        add_events(db, [(f"t_card{i}", 1, "blocked", None)])
        ow.tick(now=t, boards={BOARD: db}, state_path=state_path, inject=stub,
                has_orch=lambda b: TARGET, paneless=check)
        ow.tick(now=t + 95, boards={BOARD: db}, state_path=state_path,
                inject=stub, has_orch=lambda b: TARGET, paneless=check)
        assert pending_n(state_path) == 0, f"pending grew on tick {i}"
    assert stub.calls == []
    # and every event it drained was counted, i.e. the watermark moved
    assert ow.load_state(state_path)["last"][BOARD] >= 4


def test_a_pane_appearing_resumes_nudging_on_the_next_tick(tmp_path,
                                                           state_path):
    """The skip is a per-tick verdict, not a latch: the moment the target is
    reachable, new events nudge — without a flood of the drained history."""
    db = seeded(tmp_path, state_path)
    stub = StubInject()
    ow.tick(now=DELIVER, boards={BOARD: db}, state_path=state_path, inject=stub,
            has_orch=lambda b: TARGET,
            paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)
    assert pending_n(state_path) == 0 and stub.calls == []

    add_events(db, [(TASK, 2, "review_requested", None)])
    ow.tick(now=DELIVER + 30, boards={BOARD: db}, state_path=state_path,
            inject=stub, has_orch=lambda b: TARGET,
            paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)
    dec = ow.tick(now=DELIVER + 400, boards={BOARD: db}, state_path=state_path,
                  inject=stub, has_orch=lambda b: TARGET,
                  paneless=lambda b, t: None)

    assert len(dec) == 1 and dec[0]["ok"] is True
    assert "review_requested" not in dec[0]["msg"]      # kind shows as a label
    assert len(dec[0]["lines"]) == 1                    # only the NEW event
    assert skip_rec(state_path) is None                 # proven reachable: forget


def test_no_wake_claim_is_taken_for_a_board_that_will_not_speak(tmp_path,
                                                                state_path):
    """A claim is a lease on the OTHER emitter. Taking one for a send that never
    happens is a dropped wake dressed as a dedupe — so the skip must never write
    the table, not even to "just" suppress.

    The read-only consultation (:meth:`BoardWakeClaim.suppressions`) runs before
    the gate, as it does for every board; what must never happen is a SIDE
    EFFECT — ``take`` / ``confirm`` / ``release`` — and no row may appear.
    """
    db = seeded(tmp_path, state_path)
    claims_obj = RecordingClaims()
    ow.tick(now=DELIVER, boards={BOARD: db}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: TARGET,
            wake_claim=claims_obj,
            paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)
    kinds = [name for name, _db in claims_obj.calls]
    assert "take" not in kinds and "confirm" not in kinds \
        and "release" not in kinds
    assert claims(db) == []


def test_dispatch_path_is_untouched_on_a_skipped_board(tmp_path, state_path):
    """THE measured difference. The waker may stop nudging board `general` and
    still change nothing about how that board is dispatched: no byte of the board
    DB moves, the ready card keeps its un-claimed dispatch columns, the board is
    still scanned (so a later dispatch event is still seen), and a second board
    with a real pane is still nudged in the same tick. `skip_boards` stays empty
    for it — that knob would mute the board wholesale instead."""
    db = seeded(tmp_path, state_path)
    other = make_board(tmp_path / "hscc" / "kanban.db",
                       tasks=[("t_oth1", "Other board card")])
    ow.tick(now=NOW, boards={"hscc": other}, state_path=state_path)
    add_events(other, [("t_oth1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"hscc": other}, state_path=state_path)

    before_db, before_row = digest(db), task_row(db, TASK)
    dec = ow.tick(now=DELIVER, boards={BOARD: db, "hscc": other},
                  state_path=state_path, inject=StubInject(ok=True),
                  has_orch=lambda b: {"general": TARGET,
                                      "hscc": "hscc-orch"}[b],
                  paneless=lambda b, t: (ow.REASON_RETIRED_PROFILE
                                         if b == BOARD else None))

    assert digest(db) == before_db, "a skip must not write to the board DB"
    assert task_row(db, TASK) == before_row            # dispatch columns intact
    assert before_row["status"] == "ready" and before_row["claim_lock"] is None
    # the live board still got its nudge on the same tick
    assert [d["board"] for d in dec] == ["hscc"] and dec[0]["ok"] is True
    # the skipped board is still ENUMERATED and SCANNED: its watermark advances,
    # so dispatch keeps reporting to the waker even while the nudge is skipped
    add_events(db, [(TASK, 3, "completed", None)])
    last_before = ow.load_state(state_path)["last"][BOARD]
    ow.tick(now=DELIVER + 30, boards={BOARD: db}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: TARGET,
            paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)
    assert ow.load_state(state_path)["last"][BOARD] > last_before


def test_skip_record_survives_the_stream_stamp(tmp_path, state_path,
                                               monkeypatch):
    """t_fa9a2faf discipline for the new key: the stream stamp and the waker's
    own state file are the SAME document, so a key the stamp does not carry is
    erased every tick — which would silently restart the announcement window.

    The two paths are made identical the way the daemon's are: ``STATE_DIR``
    pointed at the directory holding ``orch_waker.json`` (the stream's own file
    name), which is exactly the collision the collision suite reproduces.
    """
    from hscc_daemon import state as _state
    monkeypatch.setattr(_state, "STATE_DIR",
                        os.path.dirname(state_path))
    db = seeded(tmp_path, state_path)
    ow.tick(now=DELIVER, boards={BOARD: db}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: TARGET,
            paneless=lambda b, t: ow.REASON_RETIRED_PROFILE)
    assert ow.load_state(state_path).get(ow.PANELESS_STATE_KEY)

    stamped = ow.stamp_entry(state_path, {"ok": True, "last_tick": DELIVER})
    assert stamped.get(ow.PANELESS_STATE_KEY), \
        "the stamp must merge the skip records it does not own"
    assert ow.paneless_state(stamped)[BOARD]["reason"] == ow.REASON_RETIRED_PROFILE


def test_state_doc_stays_the_stopgap_schema_when_nothing_is_skipped(
        tmp_path, state_path):
    db = seeded(tmp_path, state_path)
    ow.tick(now=DELIVER, boards={BOARD: db}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: TARGET)
    assert set(ow.load_state(state_path)) == {"last", "pending", "sent"}


def test_malformed_skip_record_is_dropped_not_trusted(state_path):
    """A counter that cannot be parsed must never mute a board."""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({"last": {}, "pending": {}, "sent": {},
                   ow.PANELESS_STATE_KEY: {
                       "junk1": "not-a-dict",
                       "junk2": {"at": "now", "n": 1},
                       "junk3": {"at": NOW, "n": 0},
                       "good": {"at": NOW, "n": 2, "profile": TARGET,
                                "reason": ow.REASON_RETIRED_PROFILE}}}, fh)
    assert set(ow.paneless_state(ow.load_state(state_path))) == {"good"}


def test_foreign_top_level_keys_are_left_alone(state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({"last": {}, "pending": {}, "sent": {},
                   ow.PANELESS_STATE_KEY: "junk", "foreign": {"x": 1}}, fh)
    st = ow.load_state(state_path)
    assert ow.PANELESS_STATE_KEY not in st and st["foreign"] == {"x": 1}


# ── the wiring: run_tick / make_inject / the daemon stamp ───────────────────

def _w2_env(tmp_path, state_path, souls, panes, orch_map=None):
    paused = tmp_path / "kanban_paused.json"
    paused.write_text(json.dumps({}))
    return {
        "profiles_dir": make_profiles(tmp_path, **souls),
        "paused_path": str(paused),
        "audit_path": str(tmp_path / "orch_waker.jsonl"),
        "state_path": state_path,
        "orch_map": orch_map or {},
        "boards": {BOARD: str(tmp_path / BOARD / "kanban.db")},
        "repl": FakeRepl(panes=panes),
    }


def test_run_tick_skips_the_retired_board_end_to_end(tmp_path, state_path):
    """The measured defect, at the level it was measured: no audit line at all,
    no inject attempt at all, batch drained."""
    db = seeded(tmp_path, state_path)
    env = _w2_env(tmp_path, state_path, {TARGET: RETIRED_SOUL}, panes=[])
    dec = ow.run_tick(now=DELIVER, repl=env["repl"], boards=env["boards"],
                      profiles_dir=env["profiles_dir"],
                      paused_path=env["paused_path"],
                      audit_path=env["audit_path"], state_path=env["state_path"],
                      orch_map=env["orch_map"])
    assert dec == []
    assert env["repl"].injects == []
    assert audit_lines(env["audit_path"]) == []
    assert pending_n(state_path) == 0


def test_run_tick_still_retries_a_live_profile_that_has_no_pane(tmp_path,
                                                                state_path):
    """The fail-open half, end to end: an UN-retired orchestrator with no pane
    is still attempted, still audited, and still arms t_f1122b7a's backoff —
    because an operator's pane coming back must still get its batch."""
    db = seeded(tmp_path, state_path, board="grid")
    env = _w2_env(tmp_path, state_path, {"grid-orch": LIVE_SOUL}, panes=[])
    repl = FakeRepl(inject_raises=InjectErrorLike(
        "no_panes", "no tmux pane running a Hermes REPL for profile 'grid-orch'"))
    ow.run_tick(now=DELIVER, repl=repl, boards={"grid": db},
                profiles_dir=env["profiles_dir"], paused_path=env["paused_path"],
                audit_path=env["audit_path"], state_path=state_path)
    lines = audit_lines(env["audit_path"])
    assert len(lines) == 1 and lines[0]["ok"] is False
    assert (ow.fail_state(ow.load_state(state_path)).get("grid") or {}).get("n") == 1
    assert pending_n(state_path, "grid") == 1          # batch kept for the retry


def test_run_tick_nudges_a_pane_resident_orchestrator_as_before(tmp_path,
                                                                state_path):
    db = seeded(tmp_path, state_path, board="hscc")
    env = _w2_env(tmp_path, state_path, {"hscc-orch": LIVE_SOUL},
                  panes=[pane("hscc-orch", "0:1.4")])
    dec = ow.run_tick(now=DELIVER, repl=env["repl"], boards={"hscc": db},
                      profiles_dir=env["profiles_dir"],
                      paused_path=env["paused_path"],
                      audit_path=env["audit_path"], state_path=state_path)
    assert [d["board"] for d in dec] == ["hscc"] and dec[0]["ok"] is True
    assert env["repl"].injects == [("hscc-orch", dec[0]["msg"], "auto")]
    assert audit_lines(env["audit_path"])[0]["ok"] is True


def test_gate_is_off_for_a_caller_that_owns_its_delivery(tmp_path, state_path):
    """A caller that brings its own ``inject`` owns its own tmux budget: the
    default checker must not sweep a server on its behalf (that would also make
    every pre-existing caller depend on tmux)."""
    db = seeded(tmp_path, state_path)
    env = _w2_env(tmp_path, state_path, {TARGET: RETIRED_SOUL}, panes=[])
    stub = StubInject()
    dec = ow.run_tick(now=DELIVER, repl=env["repl"], inject=stub,
                      boards=env["boards"], profiles_dir=env["profiles_dir"],
                      paused_path=env["paused_path"],
                      audit_path=env["audit_path"], state_path=state_path)
    assert env["repl"].sweeps == 0
    assert len(dec) == 1 and stub.calls == [(BOARD, dec[0]["msg"])]


def test_blind_pane_discovery_leaves_the_board_on_retry(tmp_path, state_path):
    db = seeded(tmp_path, state_path)
    env = _w2_env(tmp_path, state_path, {TARGET: RETIRED_SOUL}, panes=[])
    repl = FakeRepl(raises=InjectErrorLike("ambiguous", "two live tmux servers"),
                    inject_raises=InjectErrorLike("no_panes", "no pane"))
    dec = ow.run_tick(now=DELIVER, repl=repl, boards=env["boards"],
                      profiles_dir=env["profiles_dir"],
                      paused_path=env["paused_path"],
                      audit_path=env["audit_path"], state_path=state_path)
    assert len(dec) == 1 and dec[0]["ok"] is False     # attempted, not skipped
    assert pending_n(state_path) == 1
    assert skip_rec(state_path) is None


# ── the operator-facing surfaces ────────────────────────────────────────────

def test_waker_tick_stamps_the_skip_and_names_it(tmp_path, monkeypatch):
    """A permanently muted board must never be able to stamp a bare `steady`.

    ``run_tick`` is patched at its own seam, so this pins the STAMPING contract
    and nothing else: whatever the scan reports as skipped must land in the
    stream entry AND in the one human field doctor/verify print.
    """
    cfg = tmp_path / "cfg" / "orch_waker.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"enabled": True}))
    st = str(tmp_path / "state" / "orch_waker.json")

    class Spy:
        def __init__(self):
            self.writes = []

        def write(self, name, data):
            self.writes.append((name, data))

        def read(self, name):
            return None

    spy = Spy()
    report = {"paneless": [{"board": BOARD, "profile": TARGET,
                            "reason": ow.REASON_RETIRED_PROFILE, "ticks": 1,
                            "since": NOW}]}

    def fake_run_tick(**kwargs):
        kwargs["report"].update(report)
        return []

    monkeypatch.setattr(ow, "run_tick", fake_run_tick)
    entry = ow.waker_tick(now=NOW, config_path=str(cfg), state_path=st,
                          read=spy.read, write=spy.write)
    assert entry["ok"] is True and entry["paneless"] == report["paneless"]
    # `steady` is the base the counter math produces for a tick with no
    # decisions; the skip must be NAMED on top of it, never folded into it.
    assert entry["message"] != "steady"
    assert "no pane expected" in entry["message"] and BOARD in entry["message"]
    assert ow.REASON_RETIRED_PROFILE in entry["message"]
    assert spy.writes and spy.writes[0][0] == ow.STREAM_NAME


def test_tick_message_keeps_its_pinned_shapes_and_names_the_skip():
    assert ow._tick_message(0, 0, 0) == "steady"
    assert ow._tick_message(1, 0, 1) == "1 nudged, 0 failed, 1 decided"
    assert ow._tick_message(0, 0, 0, [{"board": "b", "retry_in_s": 870.0,
                                       "failures": 3}]).startswith(
        "steady — backing off: b (870.0s)")
    msg = ow._tick_message(0, 0, 0, (), 0, [{"board": BOARD, "profile": TARGET,
                                             "reason": ow.REASON_RETIRED_PROFILE}])
    assert BOARD in msg and ow.REASON_RETIRED_PROFILE in msg
    assert "steady" in msg            # the counters are still the truth
    many = [{"board": f"b{i}", "profile": "p", "reason": "r"} for i in range(5)]
    assert ow._tick_message(0, 0, 0, (), 0, many).endswith("+2 more")


def test_status_reports_the_skip_set_and_is_empty_when_not(tmp_path):
    st = str(tmp_path / "state" / "orch_waker.json")
    os.makedirs(os.path.dirname(st), exist_ok=True)
    with open(st, "w") as fh:
        json.dump({"last": {BOARD: 7}, "pending": {}, "sent": {},
                   ow.PANELESS_STATE_KEY: {
                       BOARD: {"at": NOW, "n": 12, "profile": TARGET,
                               "reason": ow.REASON_RETIRED_PROFILE}}}, fh)
    status = ow.waker_status(now=NOW, config_path=str(tmp_path / "cfg.json"),
                             state_path=st,
                             audit_path=str(tmp_path / "none.jsonl"))
    assert status["paneless_skip"] == [{
        "board": BOARD, "profile": TARGET, "reason": ow.REASON_RETIRED_PROFILE,
        "ticks": 12, "since": NOW}]
    assert status["boards"][BOARD]["pending"] == 0
