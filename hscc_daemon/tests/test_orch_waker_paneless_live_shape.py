"""t_fc2c09cb — the ``general -> general-orch`` lane's paneless skip, pinned in
the shape the live install actually has.

WHY THIS FILE EXISTS. The card asserts that the pane-expectation predicate
(:func:`hscc_daemon.orch_waker.paneless_reason`) cannot cover the retired
catch-all on the ``general`` board, because that board "is not a main-orch-owned
board" and the profile "carries no retirement marker". Measured against the
shipped code and hscc's OWN records, both halves of that claim are false, and
they are the only two halves the predicate reads:

  * the profile's ``SOUL.md`` written by ``hscc-roles/migrate_main`` DOES carry
    ``main_orch.RETIRED_MARKER``, so ``profile_is_retired`` answers True; and
  * ``main_orch.main_board()`` resolves to the board the catch-all is resolved
    for, so ``legacy_cluster_identity`` answers True on that board too.

The waker's own audit trail puts the last ``general -> general-orch`` failed
attempt 17 seconds BEFORE the daemon was restarted onto the build that contains
#587, and records no attempt for that lane in the 4h12m since; the deployed
``orch_waker.py`` is byte-identical to the ``main`` copy that carries the gate.
So the noise the card describes is not reproducible on the shipped build.

WHAT THIS FILE ADDS (t_7b2d3dba's file is untouched — the rule it pins is the
rule this card asks to keep). The original suite proves the four quadrants of the
verdict; these tests pin the three shapes the live install has that it does not
cover, each of which is a way the general lane could regress back into a FAILED
cycle without any test going red:

  * a sweep that SUCCEEDS on a BUSY tmux server — other lanes' panes present,
    zero for the target. An empty inventory is not the live fact, and a future
    "skip only when the whole server is empty" relaxation would silently put the
    lane back on the metronome;
  * each half of (B) carrying the lane ALONE. The identity half alone (a SOUL
    that never got the marker) and the retirement half alone (a main orchestrator
    renamed onto another board) — the card's "neither applies" case is exactly
    the one where the two conditions are independent, and the independence is
    what makes the lane safe under a rename;
  * the two directions the card demanded, end to end: a pane for the target means
    it is still nudged, and a sweep that REFUSES means never skipped.

And a source pin for the card's "no generic catch-all reason" rule: the skip
vocabulary is exactly the two named reasons the resolver owns.

Offline like every other waker suite: tmp boards/profiles/audit, an injected
clock, a stub repl. No tmux, no live board, no real ``~/.hscc``/``~/.hermes``.
"""

import inspect
import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000
#: DEBOUNCE_S past the moment the batch started; MIN_GAP_S is inert because
#: nothing has ever been sent on a fresh state document.
DELIVER = NOW + 100
BOARD = "general"           # the board whose catch-all lane the report named
TARGET = "general-orch"     # ow.orch_name("general") resolves to this
MAIN = "general"            # main_orch.main_board() for the un-renamed install
#: The other orchestrator lanes of a busy server — the live audit line names
#: several of these in its ``found:`` list while naming no pane for TARGET.
OTHER_LANES = ("ecofire-app-orch", "sphoin-orch", "micro-notify-orch",
               "headless-display-orch")
RETIRED_SOUL = ("## Retired \u2014 use `general`\n\n"
                "This profile is **retired \u2014 use general**. It is kept on "
                "disk only for history \u2014 do not create, dispatch or claim "
                "work here.\n")
LIVE_SOUL = "You are the orchestrator for this project.\n"

#: Hermes' board DDL, copied whole. A fixture narrower than the table it imitates
#: is invisible to every assertion: ``task_runs`` and ``kanban_wake_claims`` are
#: read by the very paths under test, and a missing table fails open, which makes
#: a "nothing was written" assertion pass while exercising nothing.
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


# ── fixtures / helpers (the ones test_orch_waker_paneless_skip.py proved) ───

@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hscc-state" / "orch_waker.json")


def make_board(path, tasks=(), events=()):
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


def make_profiles(tmp_path, **souls):
    """profiles_dir with one dir per name; SOUL.md written when given."""
    pdir = tmp_path / "profiles"
    for name, soul in souls.items():
        (pdir / name).mkdir(parents=True, exist_ok=True)
        if soul is not None:
            (pdir / name / "SOUL.md").write_text(soul, encoding="utf-8")
    return str(pdir)


def main_orch_doc(tmp_path, name):
    """A tmp ``~/.hscc`` holding the main-orchestrator identity document.

    The resolver owns the identity; the test only says which name the install
    stored, so ``main_board()`` answers for the board under test.
    """
    hsc = tmp_path / "hscc-root" / str(name)
    hsc.mkdir(parents=True, exist_ok=True)
    (hsc / "main-orchestrator.json").write_text(json.dumps({"name": name}),
                                                encoding="utf-8")
    return str(hsc)


def audit_lines(path):
    if not os.path.exists(str(path)):
        return []
    with open(str(path)) as f:
        return [json.loads(line) for line in f if line.strip()]


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


class InjectErrorLike(Exception):
    """Carries ``.kind`` like ``repl_inject.InjectError`` (no import coupling)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def pane(profile, target="0:1.0"):
    return {"pane": "%" + target, "target": target, "tty": "/dev/ttys0",
            "profile": profile, "cmd": f"hermes -p {profile} chat"}


def busy_server(extra=()):
    """A server with several orchestrator panes and none for TARGET.

    This is the live shape: the failed attempt the operator quoted names other
    lanes in its ``found:`` list while naming no pane for ``general-orch``. A
    sweep like this SUCCEEDED \u2014 it is (A) satisfied, not blindness.
    """
    panes = [pane(name, f"0:{i}.0") for i, name in enumerate(OTHER_LANES)]
    panes += list(extra)
    return panes


def seeded(tmp_path, state_path, board=BOARD):
    """Tail-start the board (the watermark stamp) and age one event past the
    debounce, so the next tick owns a batch that is actually due.

    The tail-start tick is not optional: without it the first batch is swallowed
    as pre-existing backlog, which reads as "the gate never fired".
    """
    db = make_board(tmp_path / board / "kanban.db",
                    tasks=[("t_pinned01", "Lane pin fixture card")])
    ow.tick(now=NOW, boards={board: db}, state_path=state_path)
    add_events(db, [("t_pinned01", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={board: db}, state_path=state_path)
    return db


def run(tmp_path, state_path, souls, repl, board=BOARD, hscc_dir=None):
    """One delivery-owning ``run_tick`` over ``board`` with everything injected."""
    paused = tmp_path / "kanban_paused.json"
    paused.write_text(json.dumps({}))
    dec = ow.run_tick(
        now=DELIVER, repl=repl, boards={board: str(tmp_path / board / "kanban.db")},
        profiles_dir=make_profiles(tmp_path, **souls),
        paused_path=str(paused),
        audit_path=str(tmp_path / "orch_waker.jsonl"),
        state_path=state_path, hscc_dir=hscc_dir)
    return dec, str(tmp_path / "orch_waker.jsonl")


def pending_n(state_path, board=BOARD):
    return len(ow.load_state(state_path)["pending"].get(board) or [])


def skip_rec(state_path, board=BOARD):
    return ow.paneless_state(ow.load_state(state_path)).get(board)


# ── (i) the live shape: a BUSY server, zero panes for the retired lane ──────

def test_busy_server_with_no_pane_for_the_retired_lane_skips_end_to_end(
        tmp_path, state_path):
    """The measured live fact, at the level the operator measured it.

    Several other orchestrators have panes, the retired catch-all has none, and
    the answer must still be a named skip: no inject attempt, no audit line, the
    batch drained. The original suite's end-to-end skip case sweeps an EMPTY
    server; an inventory full of other lanes is what the install really has, and
    pinning it here is what stops "skip only when nothing at all is running"
    from ever being mistaken for a tightening of the rule.
    """
    seeded(tmp_path, state_path)
    repl = FakeRepl(panes=busy_server())
    dec, audit = run(tmp_path, state_path, {TARGET: RETIRED_SOUL}, repl,
                     hscc_dir=main_orch_doc(tmp_path, MAIN))

    assert dec == []
    assert repl.injects == []
    assert audit_lines(audit) == []
    assert pending_n(state_path) == 0
    assert skip_rec(state_path)["reason"] == ow.REASON_RETIRED_PROFILE
    assert skip_rec(state_path)["profile"] == TARGET


def test_each_half_of_the_expectation_carries_the_lane_alone(tmp_path):
    """(B) is TWO independent records, and the lane survives losing either one.

    ``identity only`` is a catch-all whose ``SOUL.md`` never got the marker (an
    install the marker migration did not touch); ``retired only`` is the same
    profile after the main orchestrator was RENAMED onto a different board, which
    switches ``legacy_cluster_identity`` off for it. Both must still skip: that
    independence is the reason the card's "board is not main-orch-owned AND no
    marker" scenario does not exist for this lane \u2014 it would need BOTH records
    to be absent at once, and the migration writes one of them.
    """
    # identity half alone: no marker in the SOUL, but it IS the legacy catch-all
    # on the board the main orchestrator owns.
    pdir = make_profiles(tmp_path, **{TARGET: LIVE_SOUL})
    repl = FakeRepl(panes=busy_server())
    check = ow.make_paneless_checker(profiles_dir=pdir, repl=repl,
                                     hscc_dir=main_orch_doc(tmp_path, MAIN))
    assert check(BOARD, TARGET) == ow.REASON_LEGACY_IDENTITY

    # retirement half alone: the main orchestrator now lives on another board,
    # so the identity answer is False for `general` and the SOUL carries it.
    pdir2 = make_profiles(tmp_path, **{TARGET: RETIRED_SOUL})
    check2 = ow.make_paneless_checker(profiles_dir=pdir2, repl=FakeRepl(panes=busy_server()),
                                      hscc_dir=main_orch_doc(tmp_path, "grid"))
    assert check2(BOARD, TARGET) == ow.REASON_RETIRED_PROFILE

    # neither record: the ordinary dead-pane case, still nudged (control, so the
    # two assertions above cannot pass by the checker skipping everything).
    check3 = ow.make_paneless_checker(profiles_dir=pdir2, repl=FakeRepl(panes=busy_server()),
                                      hscc_dir=main_orch_doc(tmp_path, "grid"))
    assert check3(BOARD, "grid-orch") is None


# ── (ii)/(iii) the two directions the card demanded ─────────────────────────

def test_a_pane_for_the_retired_lane_is_still_nudged_end_to_end(tmp_path,
                                                                state_path):
    """(ii): a label never switches off a delivery that lands.

    Same retired profile, same busy server, plus ONE pane for the target: the
    batch goes out, the audit line says ok, and no skip is recorded.
    """
    seeded(tmp_path, state_path)
    repl = FakeRepl(panes=busy_server(extra=[pane(TARGET, "0:2.0")]))
    dec, audit = run(tmp_path, state_path, {TARGET: RETIRED_SOUL}, repl,
                     hscc_dir=main_orch_doc(tmp_path, MAIN))

    assert [d["board"] for d in dec] == [BOARD] and dec[0]["ok"] is True
    assert repl.injects == [(TARGET, dec[0]["msg"], "auto")]
    lines = audit_lines(audit)
    assert len(lines) == 1 and lines[0]["ok"] is True
    assert lines[0]["profile"] == TARGET
    assert skip_rec(state_path) is None


def test_a_sweep_that_refuses_never_skips_the_lane_end_to_end(tmp_path,
                                                              state_path):
    """(iii) the pin: blindness is unknown, unknown is not zero panes.

    The refusal repl_inject raises when it will not guess between two live
    servers must leave the lane exactly where t_f1122b7a put it: attempted,
    audited FAILED, counted toward the backoff, batch kept. Silencing a board on
    an outage would mute a lane that works the moment tmux answers again.
    """
    seeded(tmp_path, state_path)
    # The SAME refusal on both reads: a server it will not choose between is
    # unknown to the gate (so the gate stays out of the way) and un-deliverable
    # for `inject` (so the attempt is audited FAILED, which is the line the
    # operator saw). A fake that only refuses the sweep would model a server that
    # answers delivery but not discovery, and would prove nothing about (A).
    refusal = InjectErrorLike("ambiguous", "two live tmux servers")
    repl = FakeRepl(raises=refusal, inject_raises=refusal)
    dec, audit = run(tmp_path, state_path, {TARGET: RETIRED_SOUL}, repl,
                     hscc_dir=main_orch_doc(tmp_path, MAIN))

    assert len(dec) == 1 and dec[0]["ok"] is False
    assert len(repl.injects) == 1                      # attempted anyway
    lines = audit_lines(audit)
    assert len(lines) == 1 and lines[0]["ok"] is False
    assert ow.fail_state(ow.load_state(state_path))[BOARD]["n"] == 1
    assert pending_n(state_path) == 1                  # kept for the retry
    assert skip_rec(state_path) is None


# ── the seam's own contract: one verdict per (board, profile) ───────────────

def test_the_identity_verdict_never_leaks_between_profiles_of_one_board(tmp_path):
    """The checker's cache must be keyed by the pair it answers for.

    ``check(board, profile)`` is the public seam (``tick(paneless=...)``), and its
    contract is one verdict per PAIR. The identity half was cached per BOARD, so a
    second profile asked about the same board in the same tick inherited the first
    profile's "this name is the retired cluster identity" answer — and inherited it
    in the dangerous direction: a live, un-retired orchestrator with no pane got a
    SKIP, attributed to a retirement record that is not about it. ``tick`` happens
    to resolve one target per board per pass, so the leak is latent there; the
    seam itself is where it is reachable, and the direction of the bug (silencing
    a lane instead of retrying it) is the one thing the two-conditions rule exists
    to make impossible.

    Both orders are pinned: the live lane must never borrow the skip, and the
    retired lane must not lose its reason because a live profile was asked first.
    """
    pdir = make_profiles(tmp_path, **{TARGET: LIVE_SOUL, "grid-orch": LIVE_SOUL})
    hsc = main_orch_doc(tmp_path, MAIN)

    # retired name first, live name second: the live one must NOT inherit.
    repl = FakeRepl(panes=busy_server())
    check = ow.make_paneless_checker(profiles_dir=pdir, repl=repl, hscc_dir=hsc)
    assert check(BOARD, TARGET) == ow.REASON_LEGACY_IDENTITY
    assert check(BOARD, "grid-orch") is None

    # live name first, retired name second: the retired one keeps its verdict.
    check2 = ow.make_paneless_checker(profiles_dir=pdir, repl=FakeRepl(
        panes=busy_server()), hscc_dir=hsc)
    assert check2(BOARD, "grid-orch") is None
    assert check2(BOARD, TARGET) == ow.REASON_LEGACY_IDENTITY

    # and one sweep still covers the whole pass for all of it (cost rule intact)
    assert repl.sweeps == 1


# ── the vocabulary: the card's "no generic catch-all reason" rule ───────────

def test_the_skip_vocabulary_is_only_the_two_named_reasons():
    """A skip must always name WHICH record said no pane is expected.

    A future generic ``"paneless"``/``"no-pane"`` catch-all is the shape that
    makes a muted lane indistinguishable from a broken verdict, so the function
    that decides is pinned to return nothing but the two resolver-owned names
    (or None). Behaviourally the two constants are already pinned by t_7b2d3dba;
    this pins the *vocabulary*, which no behaviour test can see.
    """
    src = inspect.getsource(ow.paneless_reason)
    returned = [line.split("return", 1)[1].strip()
                for line in src.splitlines()
                if line.strip().startswith("return")]
    assert set(returned) == {"None", "REASON_RETIRED_PROFILE",
                             "REASON_LEGACY_IDENTITY"}, returned
    # and both names come from the module, not from a local literal
    assert ow.REASON_RETIRED_PROFILE == "retired-orch-profile"
    assert ow.REASON_LEGACY_IDENTITY == "legacy-cluster-identity"
