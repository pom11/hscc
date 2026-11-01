"""GAP-C-3 structural guard — exactly ONE pane-matcher predicate repo-wide.

Audit C (t_e73d41cf) measured what happens when a consumer copies the pane
rule instead of calling it: tmux_chat's twin of hscc-api/repl_inject's
matcher had already diverged (a per-line ``default`` contradicting the
canonical prefer-profiled-sibling rule), and the fix card (t_0e5a8660)
folded every copy into a delegation. A fix that only moves text is not
closed until a check makes re-copying fail: this sweep judges every tracked
production ``.py`` file and passes only while the predicate lives in the
ONE owner. A future twin fails here, at commit time, not in the next audit.

What counts as a copy (measured against the audit's own grep, then
whittened to remove one adjudicated false positive):

  * the chat-argv marker literal (``" chat"``) anywhere in the file — a
    copy of the matcher must test the chat marker, so this alone catches
    every whole-predicate twin (the audit-era copies at tmux_chat.py:150
    and :193 both carried it);
  * the profile-flag regex source (``-p|--profile``) anywhere — catches a
    re-copied pattern even without the rest of the predicate;
  * a quoted ``"gateway"`` membership test ON A LINE THAT ALSO MENTIONS
    chat — the matcher-shaped gateway clause. Line-scoped on purpose:
    ``enable_plugins.py`` legitimately writes ``if "gateway" not in cfg:``
    (a dict KEY on a config dict — not a command-line predicate), and a
    file-wide gateway fragment flagged it. A copy of the matcher's gateway
    clause necessarily sits on a chat-testing line; a config-key check
    does not.

Fragment strings are ASSEMBLED AT RUNTIME (the ``test_ledger_scrub.py``
``_addr`` idiom): this file quotes the rule it sweeps, and a contiguous
literal here would make the sweep's own file a self-hit.

Scope decisions (stated, not hidden):
  * TEST files are exempt — a test necessarily quotes the rule to pin it
    (test_repl_inject.py documents the marker in a comment today). The
    predicate is production code; the sweep judges production code.
  * ``hscc_daemon/kanban_live.py``'s ``_chat_profiles`` is NOT a twin and
    carries none of these fragments: it matches argv TOKENS of
    ``ps -Ao args=`` for the kanban monitor's rows (a different question
    with documented worker/wrapper skips), and audit C did not card it
    under GAP-C-3. Recorded in the PR.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The ONE owner of the pane matcher (acceptance: repo grep lands here only).
SOLE_MATCHER_FILE = "hscc-api/repl_inject.py"


def _frag(name: str) -> str:
    """Assemble a quoted fragment at runtime — see module docstring."""
    return '"' + name + '"'


#: file-level fragments: any occurrence outside the owner is a copy.
FILE_FRAGMENTS = {
    "chat-argv marker": _frag(" chat"),
    "profile-flag regex": "-p" + "|--" + "profile",
}

#: line-level rule: a matcher-shaped gateway clause (see module docstring).
GATEWAY_TEST_RE = re.compile(_frag("gateway") + r"\s+(?:not\s+)?in\b")


def _tracked_py_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"],
                         cwd=REPO, capture_output=True, text=True,
                         check=True).stdout
    return sorted(p for p in out.split("\0") if p)


def _is_exempt(path: str) -> bool:
    """Test suites, archived code and build output never own production
    predicates."""
    parts = path.split("/")
    return ("tests" in parts or Path(path).name.startswith("test_")
            or "_archive" in parts or parts[:1] == ["build"]
            or "/build/" in path)


def _find_copies(text: str):
    """(lineno, description) for every predicate-copy hit in ``text``."""
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        for name, frag in FILE_FRAGMENTS.items():
            if frag in line:
                hits.append((i, f"{name} ({frag!r})"))
        if GATEWAY_TEST_RE.search(line) and "chat" in line:
            hits.append((i, "matcher-shaped gateway clause"))
    return hits


def test_no_other_production_file_carries_a_predicate():
    """The acceptance grep, made structural: no tracked production file
    other than the matcher owner may contain the pane predicate."""
    offenders = []
    for rel in _tracked_py_files():
        if _is_exempt(rel) or rel == SOLE_MATCHER_FILE:
            continue
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        for line, why in _find_copies(text):
            offenders.append(f"{rel}:{line} carries the {why}")
    assert not offenders, (
        "more than one pane-matcher predicate in the repo — GAP-C-3 has "
        "regressed; call the canonical repl_inject matcher instead of "
        "copying it:\n  " + "\n  ".join(offenders))


def test_config_key_check_is_not_a_false_positive():
    """The one adjudicated near-miss, pinned: enable_plugins.py's config
    dict membership test (`if "gateway" not in cfg:`) is NOT a pane
    predicate and must stay green — otherwise the sweep gets disabled
    rather than fixed the first time it cries wolf."""
    text = (REPO / "hscc-bootstrap" / "enable_plugins.py")\
        .read_text(encoding="utf-8")
    assert _frag("gateway") + " not in cfg" in text, \
        "the config-key shape this exemption pins has changed"
    assert not _find_copies(text)


def test_sole_owner_actually_carries_the_whole_rule():
    """Positive control: the sweep cannot pass vacuously because the owner
    went missing or was gutted — the owner must still carry the chat
    marker, the gateway clause and the profile-flag regex."""
    text = (REPO / SOLE_MATCHER_FILE).read_text(encoding="utf-8")
    assert _frag(" chat") in text, "sole owner lost the chat marker"
    # The owner expresses the gateway clause through its named constant
    # (_GATEWAY_MARK) — either form proves the rule is still there.
    assert _frag("gateway") in text or "_GATEWAY_MARK" in text, \
        "sole owner lost the gateway clause"
    assert FILE_FRAGMENTS["profile-flag regex"] in text, \
        "sole owner lost the profile-flag regex"


def test_tmux_chat_delegates_and_holds_no_predicate():
    """The consumer folded by GAP-C-3: tmux_chat must keep calling the
    canonical matcher and keep holding none of the predicate itself."""
    text = (REPO / "hscc-project" / "flightdeck" / "core" / "tmux_chat.py")\
        .read_text(encoding="utf-8")
    assert "pane_chat_profile" in text and "pane_chat_command" in text, \
        "tmux_chat stopped delegating to the canonical matcher"
    assert not _find_copies(text), \
        "tmux_chat re-grew the predicate — the GAP-C-3 twin is back"


def test_chat_marker_used_by_the_canonical_rule_only_via_constant():
    """Within the owner, the chat marker is a named constant the public
    matcher uses (guards against a second inline copy reappearing *inside*
    the canonical file next to _CHAT_MARK)."""
    text = (REPO / SOLE_MATCHER_FILE).read_text(encoding="utf-8")
    assert text.count(_frag(" chat")) == 1, (
        "the chat marker literal appears more than once even inside the "
        "sole owner — use _CHAT_MARK")


def test_profile_regex_compiled_once():
    lines = (REPO / SOLE_MATCHER_FILE).read_text(encoding="utf-8").splitlines()
    n = sum(1 for line in lines
            if "re.compile" in line
            and FILE_FRAGMENTS["profile-flag regex"] in line)
    assert n == 1, ("the profile-flag regex must be compiled exactly once "
                    "(PROFILE_FLAG_RE)")


def test_exempt_paths_are_excluded_by_rule_not_by_luck():
    """The sweep's exemption is path-shaped — pin the boundaries so a
    re-naming cannot silently widen the sweep onto test text, and a
    production file moved into a 'tests' dir cannot hide from it (such a
    move would also take the file out of its plugin's suite)."""
    tracked = _tracked_py_files()
    assert SOLE_MATCHER_FILE in tracked
    # The audit-era twin file must still exist and be COVERED by the sweep
    # (if it is ever deleted, this test says so loudly instead of the
    # sweep quietly having nothing left to check there).
    assert "hscc-project/flightdeck/core/tmux_chat.py" in tracked
    assert not _is_exempt("hscc-project/flightdeck/core/tmux_chat.py")
    assert _is_exempt("hscc-api/tests/test_repl_inject.py")
