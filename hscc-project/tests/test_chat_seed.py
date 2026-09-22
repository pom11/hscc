"""Tests for seeding the EMPTY orchestrator session with the project digest —
`flightdeck.commands.project.cmd_chat` (+ the ``seed_session_with_digest``
helper in ``flightdeck.core.project_lifecycle``).

The feature (card 4): when the orchestrator session is CONFIRMED empty (a
provisioning placeholder with 0 messages, or freshly created in this command)
AND the project has a digest (archived history on the DEFAULT profile), seed
the digest into the orchestrator session as its opening content so the operator
lands in a session that already knows the project history instead of a blank
slate. Idempotent — after one seed the session is non-empty, so the next chat
never re-injects. Never auto-resumes telegram. Messaging is honest about what
it did (seeded / not re-seeding / no history). An env fault renders as
``runtime_error`` (exit 3), never as "no history".

Nothing touches the operator's real ``~/.hermes`` store: the orchestration
seam, the default-db seam, and the archive dir are all injected.
"""

import argparse

from flightdeck.commands import project as project_cmd
from flightdeck.core import digest as digest_core
from flightdeck.core import registry


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

class CaptureExec:
    """Injected exec seam: records the exec call instead of replacing the
    process. Mirrors ``os.execvp``'s ``(file, argv)`` contract."""

    def __init__(self):
        self.calls = []

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))
        return 0  # os.execvp never returns; only for the seam


class FakeOrchDBSession:
    """One fake covering the read+write slice the chat flow uses on the orch
    profile's state.db: ``get_session`` (discovery), ``resolve_session_by_title``
    (ensure), the create/title writes, and ``append_message`` (seeding). Tracks
    every seed append so tests can assert idempotency on the message itself."""

    def __init__(self, title=None, session_id=None, message_count=0):
        # title=None/session_id=None => a truly fresh db (no persistent session
        # yet: get_session is None and resolve_session_by_title is False), so
        # `ensure_session` will CREATE + title the session in this command.
        self._title = title
        self._session_id = session_id
        self._count = message_count
        self.appended = []  # [(session_id, role, text), ...]
        self.closed = False

    def get_session(self, session_id):
        if self._title is None or self._session_id is None:
            return None
        if session_id in (self._session_id, self._title):
            return {
                "id": self._session_id or self._title,
                "source": "cli",
                "title": self._title,
                "message_count": self._count,
                "started_at": 1700000000,
                "last_active": 1700000000,
            }
        return None

    def resolve_session_by_title(self, title):
        return self._title is not None and title == self._title

    def create_session(self, sid, **kwargs):
        self._session_id = sid

    def set_session_title(self, sid, title):
        self._session_id = sid
        self._title = title
        self._count = 0

    def append_message(self, session_id, role, text):
        self.appended.append((session_id, role, text))
        self._count += 1  # same effect as the real SessionDB (empty -> non-empty)

    def close(self):
        self.closed = True


class FakeOrchProvider:
    """A ``_session_db`` seam value: an opener returning a shared orch fake."""

    def __init__(self, db):
        self._db = db

    def __call__(self, profile):
        return self._db

    @property
    def db(self):
        return self._db


class FakeDefaultDB:
    """State.db stand-in for the DEFAULT profile (telegram side)."""

    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.closed = False

    def list_sessions_rich(self, source, limit=5000):
        assert source == "telegram"
        return [dict(r) for r in self._rows]

    def close(self):
        self.closed = True


class FakeDefaultProvider:
    """A ``_default_db`` seam value: an opener returning a shared default fake."""

    def __init__(self, rows=()):
        self._db = FakeDefaultDB(rows)

    def __call__(self, profile):
        return self._db


def _reg(tmp_path, name="ecofire", topic=8891, session="20260921_abc"):
    """A registry with project ``name`` and a persisted orchestrator session id
    (so ``resolve_orchestrator`` returns the timestamped id, not the name)."""
    path = str(tmp_path / "registry.yaml")
    registry.add_project(name, repo=str(tmp_path / name), board=name,
                         topic=topic, path=path)
    if session:
        registry.set_session(name, session, path=path)
    return path


def _session_md(session_id, title):
    """One archive .md file in the exact shape archive.py writes."""
    body = (
        "**user** · 2026-07-02 10:05:16\n"
        "Let's fix the fire reporting pipeline.\n\n"
        "**assistant** · 2026-07-02 10:06:00\n"
        "Agreed. We'll deploy the new ingest service.\n"
    )
    return (
        f"# {title}\n\n"
        f"- **session id**: `{session_id}`\n"
        f"- **source**: telegram\n"
        f"- **thread_id**: 8891\n"
        f"- **project**: ecofire\n"
        f"- **started**: 2026-07-02 10:05:16 UTC\n"
        f"- **ended**: 2026-07-02 10:30:00 UTC\n"
        f"- **message count**: 2\n\n"
        f"---\n\n{body}\n"
    )


def _write_archive(tmp_path, project, sessions):
    """Write fake session .md files under ``<tmp>/<project>/``; return the dir."""
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    for sid, title in sessions:
        (d / f"{sid}_{title.replace(' ', '_')}.md").write_text(
            _session_md(sid, title), encoding="utf-8",
        )
    return d


def _ns(tmp_path, name, orch_db, *, archive_dir, registry_path=None,
        default_rows=(), runtime_error=None, exec_seam=None):
    """Build a chat ``args`` namespace with all seams wired to the fakes."""
    reg = registry_path if registry_path is not None else _reg(tmp_path, name)
    ns = argparse.Namespace(
        name=name, registry=reg,
        session_db=FakeOrchProvider(orch_db),
        default_db=FakeDefaultProvider(default_rows),
        archive_dir=archive_dir,
        mapping_path=None,
        exec_seam=exec_seam,
        resume_id=None,
        extra=None,
        session=None,  # filled by resolve path (kept for parity with prod args)
    )
    # Model the argv/cmd fields cmd_chat reads defensively via getattr.
    return ns


# --------------------------------------------------------------------------- #
# Seeding the EMPTY orchestrator session
# --------------------------------------------------------------------------- #

def test_seed_empty_orch_session_with_digest(tmp_path, capsys):
    """A provisioning placeholder (0 messages) with an existing digest → seed
    the digest into the orch session as its opening message; exec proceeds."""
    reg = _reg(tmp_path, "ecofire", session="20260921_abc")
    # Placeholder: the orch session's title == its persisted id (the shape of
    # restored/provisioned sessions), message_count=0 -> exists + empty.
    orch = FakeOrchDBSession("20260921_abc", "20260921_abc", message_count=0)
    _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
    seam = CaptureExec()
    args = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path),
               registry_path=reg, exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert seam.calls, "exec must still proceed after seeding"
    # The digest was appended as the opening user message on the orch session.
    assert len(orch.appended) == 1
    sid, role, text = orch.appended[0]
    assert sid == "20260921_abc"
    assert role == "user"
    assert "Fire pipeline fix" in text
    # ... and the seed text is EXACTLY the digest render this project would print.
    digest = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path), _runtime_error_fn=lambda: None,
    )
    assert text == digest_core.format_digest(digest)
    # Honest messaging.
    assert "seeded session '20260921_abc' with the project digest" in out
    assert "1 thread, 2 messages" in out
    assert "(first use)" not in out
    # argv still targets the permanent orchestrator session.
    assert seam.calls[0][1] == [
        "hermes", "-p", "ecofire-orch", "chat", "--continue", "20260921_abc",
    ]


def test_seed_only_once_second_chat_does_not_reseed(tmp_path, capsys):
    """Idempotency: after a seed the session has the digest message, so the next
    chat (non-empty) must NOT re-inject, and says so honestly."""
    reg = _reg(tmp_path, "ecofire", session="20260921_abc")
    # Session NOW non-empty (message_count=1 — the digest we seeded last time).
    orch = FakeOrchDBSession("20260921_abc", "20260921_abc", message_count=1)
    _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
    seam = CaptureExec()
    args = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path),
               registry_path=reg, exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert orch.appended == [], "must NOT re-seed a non-empty session"
    assert "already has history — not re-seeding" in out
    assert "seeded session" not in out


def test_created_fresh_session_with_digest_seeds(tmp_path, capsys):
    """A brand-new orchestrator session created in this command + an existing
    digest → seed. The freshly-created session is confirmed empty (just-created
    in this command), and the seed targets the REAL created session id (not the
    name that ``--continue`` resolves by title), so hermes sees the message."""
    # No set_session -> resolve_orchestrator 'session' == the name 'ecofire';
    # orch db has NO persistent session -> ensure_session CREATES one.
    reg_path = str(tmp_path / "registry.yaml")
    registry.add_project("ecofire", repo=str(tmp_path / "ecofire"),
                         board="ecofire", topic=8891, path=reg_path)
    orch = FakeOrchDBSession(None, None, message_count=0)
    _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
    seam = CaptureExec()
    args = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path),
               registry_path=reg_path, exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert orch.appended, "a freshly-created session is empty -> seed"
    sid, role, text = orch.appended[0]
    assert role == "user"
    assert sid != "ecofire", "must seed the REAL created id, not the title name"
    assert "Fire pipeline fix" in text
    # Exec still happens and targets the permanent session (by name, resolved
    # by title to the created session on --continue).
    assert seam.calls and seam.calls[0][1][0:6] == [
        "hermes", "-p", "ecofire-orch", "chat", "--continue", "ecofire",
    ]


def test_empty_no_digest_starts_fresh(tmp_path, capsys):
    """Empty orch session with NO archived history -> nothing to seed; honest
    blank-start messaging, exec proceeds."""
    reg = _reg(tmp_path, "ecofire", session="20260921_abc")
    orch = FakeOrchDBSession("20260921_abc", "20260921_abc", message_count=0)
    # No archive dir for this project -> digest sessions empty.
    seam = CaptureExec()
    args = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path / "empty"),
               registry_path=reg, exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert orch.appended == []
    assert "starting fresh" in out or "first use" in out
    assert "seeded session" not in out
    assert seam.calls


def test_created_session_no_digest_first_use(tmp_path, capsys):
    """A brand-new project with no archive history: create the session and say
    so honestly ('first use'); nothing to seed."""
    reg_path = str(tmp_path / "registry.yaml")
    registry.add_project("fresh", repo=str(tmp_path / "fresh"), board="fresh",
                         topic=8891, path=reg_path)
    # Fresh orch db (no persistent session) -> ensure_session creates it.
    orch = FakeOrchDBSession(None, None, message_count=0)
    seam = CaptureExec()
    args = _ns(tmp_path, "fresh", orch,
               archive_dir=str(tmp_path / "archive"), registry_path=reg_path,
               exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert orch.appended == []
    assert "created session 'fresh'" in out
    assert "(first use)" in out


# --------------------------------------------------------------------------- #
# Env fault vs data, and send/seed failure honesty
# --------------------------------------------------------------------------- #

def test_runtime_error_is_not_empty_neither_seeds(tmp_path, capsys):
    """An unreadable Hermes runtime is an ENV FAULT, never 'no history': chat
    must NOT seed (it would claim to know a history it cannot read), return
    non-zero, and NOT exec hermes."""
    reg = _reg(tmp_path, "ecofire", session="20260921_abc")
    orch = FakeOrchDBSession("ecofire", "20260921_abc", message_count=0)
    _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
    seam = CaptureExec()

    # Force discovery to report a runtime fault (seams don't probe, so inject it).
    ns = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path),
             registry_path=reg, exec_seam=seam)
    # Simulate an unreadable runtime through the discovery seam.
    import unittest.mock as mock
    with mock.patch.object(
        project_cmd.session_discovery, "list_project_sessions",
        return_value={
            "project": "ecofire", "topic": 8891,
            "orch_profile": "ecofire-orch", "telegram_profile": "default",
            "orch_session": "20260921_abc",
            "orchestrator": None, "telegram": [], "telegram_unmapped": 0,
            "runtime_error": "cannot import the Hermes runtime (boom)",
        },
    ):
        rc = project_cmd.cmd_chat(ns)
    err = capsys.readouterr().err

    assert rc == 3
    assert orch.appended == [], "must never seed on an env fault"
    assert seam.calls == [], "must not exec hermes when the runtime is unreadable"
    assert "cannot read session history" in err
    assert "cannot import the Hermes runtime" in err


def test_seed_write_failure_is_honest_but_still_attaches(tmp_path, capsys):
    """If the seed WRITE fails, say so on stderr and continue to exec (the
    operator can still chat blank); never claim the seed succeeded."""
    reg = _reg(tmp_path, "ecofire", session="20260921_abc")

    class FailingDB(FakeOrchDBSession):
        def append_message(self, session_id, role, text):  # noqa: D102
            raise IOError("disk full")

    orch = FailingDB("20260921_abc", "20260921_abc", message_count=0)
    _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
    seam = CaptureExec()
    args = _ns(tmp_path, "ecofire", orch, archive_dir=str(tmp_path),
               registry_path=reg, exec_seam=seam)

    rc = project_cmd.cmd_chat(args)
    err = capsys.readouterr().err

    assert rc == 0
    assert "could not seed session '20260921_abc'" in err
    assert "continuing with a blank session" in err
    assert seam.calls, "exec still proceeds so the operator can chat"
