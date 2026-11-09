"""The leg environment of scripts/run_tests.sh must not inherit the board pins
of the dispatching worker (t_7e143e04).

What is under test is a property of a SHELL SCRIPT plus a property of hermes'
resolver, and only the pair matters: the runner drops
``HERMES_DELEGATED_CHILD_CONTEXT`` (the read fence) so fixture boards can be
created, but until t_7e143e04 it left the *location* pins
(``HERMES_KANBAN_DB`` / ``_BOARD`` / ``_WORKSPACES_ROOT`` /
``_ATTACHMENTS_ROOT``) in place. hermes resolves those pins ahead of an explicit
``board=`` inside a fence, so a test asking the real resolver for a throwaway
board got the operator's live board — measured in a dispatched worker shell to
``<kanban-home>/kanban/boards/<live-board>/workspaces`` even with
``HERMES_KANBAN_HOME`` pinned at a tmp root.

Four claims, each pinned separately so a regression names itself:

  1. the scrub list contains the marker and every pin hermes consults (a text
     check on the shipped file, so editing the invocation cannot quietly drop one);
  2. the leg invocation is actually launched through that list (a list nothing
     consumes is documentation);
  3. a child process of a runner built from the SHIPPED block does not see the
     pins — and DOES still see ``HERMES_KANBAN_TASK``, because scrubbing worker
     identity is deliberately not what this harness does (run_tests.sh explains
     why: it would flip worker-mode branches to their non-worker side);
  4. NON-VACUITY, per pin: with a fake worker env armed, hermes' own resolver
     answers with the pinned board for an explicit ``board=`` (the leak), and
     with the scrub applied it answers under the fixture root (the fix).

Hermetic: every path is a tmp dir; the only board ever created is created by
hermes' own ``create_board`` inside a tmp ``HERMES_KANBAN_HOME``, and no live
kanban path appears in any assertion or is opened by this file.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
RUNNER = SCRIPTS / "run_tests.sh"

#: Every var a leg must not inherit. The pairing with the shipped array is
#: asserted by test_scrub_list_covers_the_marker_and_every_location_pin.
MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"
PATH_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)
SLUG_PIN = "HERMES_KANBAN_BOARD"
LOCATION_PINS = (PATH_PINS[0], SLUG_PIN, PATH_PINS[1], PATH_PINS[2])

# The shipped block, extracted verbatim: both a text fixture (claims 1-2) and the
# script that is actually EXECUTED (claim 3), so neither can drift from the runner.
_ARRAY = re.compile(
    r"^SUITE_ENV_UNSET=\(\n(?P<body>.*?)^\)\n"
    r"ENV_UNSET_ARGS=\(\)\n"
    r"(?P<loop>for .*?^done)\n",
    re.MULTILINE | re.DOTALL,
)


def _shipped_match():
    m = _ARRAY.search(RUNNER.read_text(encoding="utf-8"))
    if m is None:
        raise AssertionError(
            "scripts/run_tests.sh no longer defines SUITE_ENV_UNSET/ENV_UNSET_ARGS "
            "in the shape this guard parses — re-read the runner before believing "
            "any leg result is hermetic"
        )
    return m


def _shipped_block() -> str:
    return _shipped_match().group(0)


def _shipped_unsets():
    """The var names in the shipped array, comments and whitespace stripped."""
    body = _shipped_match().group("body")
    return [line.split("#", 1)[0].strip() for line in body.splitlines()
            if line.split("#", 1)[0].strip()]


# ── claims 1 + 2: the list, and that the leg consumes it ─────────────────────


def test_scrub_list_covers_the_marker_and_every_location_pin():
    unsets = _shipped_unsets()
    assert MARKER in unsets, (
        "the read fence must stay in the list — without it every fixture board "
        "opens read-only again, which is the reason the runner scrubbed anything"
    )
    for pin in LOCATION_PINS:
        assert pin in unsets, f"{pin} left the scrub list: legs would read the live board"
    # The deliberate half of the decision, in the other direction: identity stays.
    assert "HERMES_KANBAN_TASK" not in unsets, (
        "scrubbing worker identity would silently flip every worker-mode branch in "
        "the suite to its non-worker side — see the run_tests.sh block"
    )


def test_leg_invocation_actually_uses_the_scrub_list():
    """A list nothing consumes is documentation, not a guard.

    Pins the shipped two-line shape (`env "${...}" \\` continuing into the pytest
    leg). Reformatting the invocation onto one line is fine — update this pattern
    in the same commit so the guard keeps meaning "the leg is launched through
    the scrub list".
    """
    text = RUNNER.read_text(encoding="utf-8")
    assert re.search(
        r'env\s+"\$\{ENV_UNSET_ARGS\[@\]\}"'   # the scrub list is expanded ...
        r'[^\n]*\\\n'                           # ... as a line continuation ...
        r'\s*"\$PY"\s+-m pytest\b',             # ... straight into the pytest leg
        text,
    ), ('the pytest leg is no longer launched as `env "${ENV_UNSET_ARGS[@]}" "$PY" '
        '-m pytest ...` — the pins would be back in every leg')


# ── claim 3: the shipped block, executed, against a real child ───────────────


def _child_env(extra=None):
    """Run `/usr/bin/env` through the runner's OWN scrub block with `extra` armed."""
    script = Path(tempfile.mkdtemp(prefix="harness-env-")) / "leg.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -u\n" + _shipped_block() +
        '\nenv "${ENV_UNSET_ARGS[@]}" /usr/bin/env\n',
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ["PATH"],
        MARKER: "/fenced/board/root",
        "HERMES_KANBAN_TASK": "t_faux_worker",
        "HERMES_KANBAN_WORKSPACE": "/tmp/faux/workspace",
        "HERMES_KANBAN_DB": "/live/board/kanban.db",
        "HERMES_KANBAN_BOARD": "live-board",
        "HERMES_KANBAN_WORKSPACES_ROOT": "/live/board/workspaces",
        "HERMES_KANBAN_ATTACHMENTS_ROOT": "/live/board/attachments",
    }
    env.update(extra or {})
    out = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_child_of_the_shipped_scrub_sees_no_board_pin_but_keeps_identity():
    child = _child_env()
    for var in (MARKER, *LOCATION_PINS):
        assert var not in child, f"{var} survived the runner scrub: {child.get(var)!r}"
    assert child.get("HERMES_KANBAN_TASK") == "t_faux_worker"
    assert child.get("HERMES_KANBAN_WORKSPACE") == "/tmp/faux/workspace"
    # And it is a targeted scrub, not a blanked environment: a child still needs
    # PATH (pytest children shell out), so a regression to `env -i` is caught too.
    assert child.get("PATH"), "the leg child lost PATH — `env -i` would break every child"


# ── claim 4: non-vacuity against hermes' own resolver ────────────────────────

_PATH_RESOLVERS = {
    "HERMES_KANBAN_DB": lambda kb: str(kb.kanban_db_path(board="fixture-board")),
    "HERMES_KANBAN_WORKSPACES_ROOT": lambda kb: str(kb.workspaces_root(board="fixture-board")),
    "HERMES_KANBAN_ATTACHMENTS_ROOT": lambda kb: str(kb.attachments_root(board="fixture-board")),
}
_PINNED_VALUE = {
    "HERMES_KANBAN_DB": "/live/board/kanban.db",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/live/board/workspaces",
    "HERMES_KANBAN_ATTACHMENTS_ROOT": "/live/board/attachments",
    SLUG_PIN: "live-board",
}


def _hermes():
    try:
        from hermes_cli import kanban_db as kb
    except Exception as exc:  # pragma: no cover - bare checkout, no hermes on PATH
        pytest.skip(f"hermes_cli not importable here: {exc!r}")
    return kb


@pytest.mark.parametrize("pin", PATH_PINS)
def test_a_path_pin_outranks_an_explicit_board_until_the_scrub_runs(pin, monkeypatch):
    """The leak, then the fix — same armed env, only the scrub differs.

    ``fixture-board`` is the caller's explicit intent, which is precisely what a
    pin outranks while the worker fence is up.
    """
    kb = _hermes()
    root = tempfile.mkdtemp(prefix="fixture-kanban-home-")
    monkeypatch.setenv("HERMES_KANBAN_HOME", root)      # what the conftest pins
    monkeypatch.setenv(MARKER, root)                    # the dispatched-worker fence
    for var, value in _PINNED_VALUE.items():
        monkeypatch.setenv(var, value)

    leaked = _PATH_RESOLVERS[pin](kb)
    assert not leaked.startswith(root), (
        f"{pin}: the resolver ignored its pin even with the fence armed, so this "
        "test no longer proves the runner scrub matters (hermes precedence moved?)"
    )
    for var in (MARKER, *LOCATION_PINS):
        monkeypatch.delenv(var, raising=False)
    fixed = _PATH_RESOLVERS[pin](kb)
    assert fixed.startswith(root), (
        f"{pin}: after the runner's scrub the resolver still escaped the fixture "
        f"root ({fixed}) — the scrub list is incomplete"
    )
    assert fixed != leaked


def test_the_slug_pin_decides_which_board_a_boardless_call_opens(monkeypatch):
    """``HERMES_KANBAN_BOARD`` is scrubbed for a different reason than the paths.

    A fixture root neutralises it only while the pinned slug does not exist
    (``get_current_board`` requires ``board_exists`` under the pinned home), so
    for a board that DOES exist the slug pin silently redirects every resolver
    call that names no board — here, ``kanban_db_path()`` opens the sibling
    board's DB instead of the ``default`` one. Both boards live in a tmp root.
    """
    kb = _hermes()
    root = tempfile.mkdtemp(prefix="fixture-kanban-home-")
    monkeypatch.setenv("HERMES_KANBAN_HOME", root)
    for var in LOCATION_PINS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(MARKER, raising=False)

    # Sanctioned writer, and BEFORE any fence is armed (a fenced context may not
    # mutate boards, which is the point of the fence).
    from hermes_cli import kanban_db_boards as boards
    boards.create_board("sibling-board")
    default_db = str(kb.kanban_db_path())
    assert default_db.startswith(root) and default_db.endswith("kanban.db")

    monkeypatch.setenv(SLUG_PIN, "sibling-board")
    assert kb.get_current_board() == "sibling-board"
    pinned_db = str(kb.kanban_db_path())
    assert pinned_db != default_db and "sibling-board" in pinned_db, (
        "the slug pin changed nothing, so this test has stopped proving the pin leaks"
    )

    monkeypatch.delenv(SLUG_PIN)
    assert kb.get_current_board() == kb.DEFAULT_BOARD
    assert str(kb.kanban_db_path()) == default_db
