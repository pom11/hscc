"""Tests for hscc-cluster/orch_message.py — orch-to-orch nudges.

Hermeticity contract (same doctrine as the rest of this suite, t_be4b94ca):

* **No real tmux, ever.** Delivery goes through a `repl` STUB injected via
  ``orch_message._load_repl_inject`` — the same seam `hscc-cluster/hscc.py`
  and `hscc_daemon/kanban_pause.py` tests use. The real `repl_inject` module
  is never reached, so no pane of the operator's can be discovered, let alone
  typed into.
* **No live ~/.hscc.** `HSCC_DIR` is pinned to the per-test tmp dir, so the
  JSONL audit log (and the rate limit computed from it) is always fixture state.
* **Fictional profile names only** (demo-orch / other-orch / worker-engineer),
  per the public-repo fixture rule.
"""
import json
import time

import pytest

import orch_message as om


# ── The repl_inject stub ────────────────────────────────────────────────────
#
# A FAKE module object with the same surface `orch_message` uses from the real
# repl_inject (inject / InjectError / VERB_PREFIX). It implements only what the
# tests need: profile-matched pane resolution (exactly-one or refusal) and the
# auto -> queue-while-busy / plain-when-idle decision. It is NOT a re-
# implementation of repl_inject — production still calls the real module; this
# exists so a test can neither reach tmux nor depend on the operator's panes.

class FakeInjectError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class FakeRepl:
    VERB_PREFIX = {"plain": "", "queue": "/queue add ", "steer": "/steer "}

    def __init__(self, panes=None, busy=False, raise_exc=None, legacy=False):
        self.panes = panes if panes is not None else [
            {"pane": "%0", "target": "s1:0.0", "profile": "demo-orch"},
            {"pane": "%1", "target": "s1:0.1", "profile": "other-orch"},
        ]
        self.busy = busy
        self.raise_exc = raise_exc
        #: True emulates a pre-parity repl_inject whose receipt has no `verb`.
        self.legacy = legacy
        self.calls = []

    def inject(self, profile, text, mode="auto", pane_override=None,
               sleep_fn=None, dry_run=False):
        self.calls.append({"profile": profile, "text": text, "mode": mode})
        if self.raise_exc is not None:
            raise self.raise_exc
        match = [p for p in self.panes if p["profile"] == profile]
        if len(match) != 1:
            raise FakeInjectError(
                "no_panes" if not match else "ambiguous",
                f"{len(match)} panes for {profile!r}")
        pane = match[0]
        busy = bool(self.busy)
        eff = ("queue" if busy else "plain") if mode == "auto" else mode
        verb = self.VERB_PREFIX[eff]
        receipt = {"pane": pane["pane"], "target": pane["target"],
                   "profile": profile, "busy": busy, "mode": eff,
                   "verb": verb,
                   "sent_text": verb + text}
        if self.legacy:
            receipt.pop("verb")
        return receipt


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """Every test: fictional demo-orch sender, audit log + config under tmp_path,
    and no ambient N override (a test opts into one explicitly)."""
    monkeypatch.setenv("HERMES_PROFILE", "demo-orch")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HSCC_DIR", str(tmp_path))
    monkeypatch.delenv("HSCC_ORCH_MSG_MAX_PER_HOUR", raising=False)
    yield tmp_path


@pytest.fixture
def repl(monkeypatch):
    """Install a FakeRepl on the loader seam and hand it to the test."""
    fake = FakeRepl()
    monkeypatch.setattr(om, "_load_repl_inject", lambda: fake)
    return fake


def send(to="other-orch", message="template applied, your card can move",
         ref_card=None, user_task=None, **kwargs):
    """Call the tool the way hermes dispatch does (args dict + context kwargs)."""
    return om.orch_message({"to": to, "message": message, "ref_card": ref_card},
                           user_task=user_task, **kwargs)


def audit_lines(tmp_path):
    path = tmp_path / "state" / "orch_messages.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ── Rule 1: orchestrators only ──────────────────────────────────────────────

def test_non_orch_sender_refused_and_never_reaches_delivery(monkeypatch):
    """A worker profile running the tool is refused as sender — and the refusal
    happens before any repl_inject lookup, so no pane can be touched."""
    monkeypatch.setattr(om, "running_profile", lambda: "worker-engineer")

    def boom():
        raise AssertionError("_load_repl_inject must not be reached")
    monkeypatch.setattr(om, "_load_repl_inject", boom)
    out = send()
    assert out["ok"] is False and out["sent"] is False
    assert out["reason"] == om.REASON_SENDER_NOT_ORCH
    assert out["from"] == "worker-engineer"


def test_non_orch_target_refused(repl, tmp_path):
    out = send(to="worker-engineer")
    assert out["reason"] == om.REASON_TARGET_NOT_ORCH
    assert repl.calls == [] and audit_lines(tmp_path)[0]["delivered"] is False


def test_default_target_refused_as_non_orch(repl):
    """The operator's default REPL is never reachable through this tool."""
    out = send(to="default")
    assert out["reason"] == om.REASON_TARGET_NOT_ORCH
    assert repl.calls == []


def test_default_sender_refused(monkeypatch, repl):
    """An unprofiled (default) session cannot sign a nudge either."""
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    out = send()
    assert out["reason"] == om.REASON_SENDER_NOT_ORCH
    assert repl.calls == []


def test_sender_identity_is_the_running_profile_not_the_caller(repl):
    """The sender is never an argument: HERMES_PROFILE decides it, and it is
    what the signature and the audit line carry."""
    out = send()
    assert out["ok"] is True
    assert out["from"] == "demo-orch"
    assert repl.calls[0]["text"].startswith("[from demo-orch] ")


def test_self_message_refused(repl):
    out = send(to="demo-orch")
    assert out["reason"] == om.REASON_SELF
    assert repl.calls == []


# ── Rule 1b: the CONFIGURED main orchestrator is accepted at both ends ───────
#
# The ONE main orchestrator's name is the chosen slug itself — no `-orch`
# suffix — so ORCH_PROFILE_RE refused it and the cluster's top orchestrator
# could not use the orchestrator nudge tool at all (it had to shell out to
# `hscc orch inject`). The tests below pin the allowance, and pin its *shape*:
# the name must come out of `hscc-roles/main_orch.py` — the resolver `hscc main
# status` reads — never from a copy of it kept in this plugin.
#
# Fictional main names only (ops / control), same fixture rule as the rest of
# this file. Every case runs against the sealed HSCC_DIR of `_hermetic_env`, so
# the state document being configured is always the tmp one, never ~/.hscc.

@pytest.fixture(scope="session")
def real_main_orch():
    """The REAL ``hscc-roles/main_orch.py`` — the resolver `hscc main status`
    reads (`hscc_daemon/main_cli.py::status` -> `main_orch.main_profile`).

    By file path for the same reason as `real_repl` above: independent of
    sys.path and of any stub, so a test comparing against it cannot drift from
    the module that owns the answer. Import only — no pane, no tmux.
    """
    import importlib.util
    from pathlib import Path
    path = (Path(om.__file__).resolve().parent.parent
            / "hscc-roles" / "main_orch.py")
    assert path.exists(), f"canonical main_orch not found at {path}"
    spec = importlib.util.spec_from_file_location("_main_orch_canonical", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_main(tmp_path, real_main_orch, name):
    """Store the main orchestrator's name in the sealed HSCC_DIR, the way
    bootstrap's `main_orch.py store` leaves it, and prove the resolver reads it.

    The state document is written directly rather than through
    ``main_orch.store_name`` so the test needs no roles dir, no registry and no
    profiles dir — the document *is* the storage contract. The filename comes
    from the resolver's own ``STATE_FILENAME``, so if that file is ever renamed
    these tests configure nothing and fail loudly instead of passing vacuously.
    """
    doc = tmp_path / real_main_orch.STATE_FILENAME
    doc.write_text(json.dumps({"name": name}) + "\n")
    assert real_main_orch.stored_name(str(tmp_path)) == name
    return doc


def add_pane(repl, profile, pane, target):
    """Give the fake tmux exactly one pane for `profile` (the stub refuses a
    missing or ambiguous one, so a target with no pane is not a delivery test)."""
    repl.panes.append({"pane": pane, "target": target, "profile": profile})
    return repl


def test_configured_main_orchestrator_accepted_as_target(repl, tmp_path, real_main_orch):
    configure_main(tmp_path, real_main_orch, "ops")
    add_pane(repl, "ops", "%2", "s1:0.2")
    out = send(to="ops")
    assert out["ok"] is True and out["sent"] is True
    assert out["reason"] == om.REASON_SENT
    assert repl.calls[0]["profile"] == "ops"
    # signed by the RUNNING profile, not by whoever was configured as main
    assert repl.calls[0]["text"].startswith("[from demo-orch] ")


def test_configured_main_orchestrator_accepted_as_sender(repl, tmp_path,
                                                        real_main_orch, monkeypatch):
    configure_main(tmp_path, real_main_orch, "ops")
    monkeypatch.setenv("HERMES_PROFILE", "ops")
    out = send(to="other-orch")
    assert out["ok"] is True and out["sent"] is True
    assert out["from"] == "ops"
    assert repl.calls[0]["text"].startswith("[from ops] ")


def test_main_orchestrator_to_itself_is_still_a_loop_not_a_message(
        repl, tmp_path, real_main_orch, monkeypatch):
    """The self-guard is identity-based, so the new door cannot be used to
    inject into one's own pane."""
    configure_main(tmp_path, real_main_orch, "ops")
    monkeypatch.setenv("HERMES_PROFILE", "ops")
    add_pane(repl, "ops", "%2", "s1:0.2")
    out = send(to="ops")
    assert out["reason"] == om.REASON_SELF
    assert repl.calls == []


def test_allow_follows_the_config_not_a_hardcoded_name(repl, tmp_path, real_main_orch):
    """The no-hardcode proof the card asks for: change the configured name and
    the tool follows — the new name is accepted, the previous one is refused
    again. A literal 'general' (or any other name) in the plugin could not do
    this, and the source guard below forbids one.
    """
    doc = configure_main(tmp_path, real_main_orch, "ops")
    add_pane(repl, "ops", "%2", "s1:0.2")
    add_pane(repl, "control", "%3", "s1:0.3")
    assert send(to="ops")["ok"] is True

    doc.write_text(json.dumps({"name": "control"}) + "\n")   # rename in place
    assert real_main_orch.main_profile(str(tmp_path)) == "control"
    assert send(to="control")["ok"] is True

    stale = send(to="ops")
    assert stale["ok"] is False and stale["reason"] == om.REASON_TARGET_NOT_ORCH
    assert [c["profile"] for c in repl.calls] == ["ops", "control"]


def test_allowance_answers_what_hscc_main_status_answers(repl, tmp_path, real_main_orch):
    """Same-source pin: the tool's answer equals the resolver's for a stored
    name AND for an un-stored install (where the resolver's own default applies
    — following it is what proves there is no copy of it here)."""
    assert om.main_orchestrator_profile() == real_main_orch.main_profile(str(tmp_path))
    configure_main(tmp_path, real_main_orch, "ops")
    assert om.main_orchestrator_profile() == "ops"
    assert om.is_orch_profile("ops") is True
    assert real_main_orch.main_profile(str(tmp_path)) == "ops"


def test_project_orchestrators_unaffected_by_the_new_door(repl, tmp_path, real_main_orch):
    """Non-main `*-orch` profiles keep the shipped behaviour, with and without a
    configured main: accepted at both ends, subject to every other guard."""
    assert om.is_orch_profile("demo-orch") is True
    assert om.is_orch_profile("other-orch") is True
    assert send(to="other-orch")["ok"] is True

    configure_main(tmp_path, real_main_orch, "ops")
    assert om.is_orch_profile("other-orch") is True
    second = send(to="other-orch")
    assert second["ok"] is True and second["from"] == "demo-orch"


def test_non_orch_names_still_refused_with_a_main_configured(repl, tmp_path, real_main_orch):
    """The allowance is one name wide, not a general loosening: workers stay out
    even when their name shares the main's prefix."""
    configure_main(tmp_path, real_main_orch, "ops")
    for bad in ("worker-engineer", "ops-engineer", "ops-worker", "general-orch-worker",
                "engineering", "other"):
        out = send(to=bad)
        assert out["reason"] == om.REASON_TARGET_NOT_ORCH, bad
    assert repl.calls == []


def test_empty_target_still_refused_as_bad_args(repl, tmp_path, real_main_orch):
    """Shipped argument-shape guard, unchanged by the new door."""
    configure_main(tmp_path, real_main_orch, "ops")
    assert send(to="  ")["reason"] == om.REASON_BAD_ARGS
    assert repl.calls == []


def test_default_cannot_be_made_injectable_by_configuring_it_as_main(
        repl, tmp_path, real_main_orch, monkeypatch):
    """`main_orch.validate_name` accepts any lowercase slug, and `default` is
    one — so the reserved-identity guard, not the config, is what keeps the
    operator's own REPL out of reach."""
    configure_main(tmp_path, real_main_orch, "default")
    add_pane(repl, "default", "%2", "s1:0.2")
    assert send(to="default")["reason"] == om.REASON_TARGET_NOT_ORCH
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert send()["reason"] == om.REASON_SENDER_NOT_ORCH
    assert repl.calls == []


def test_unreachable_resolver_degrades_to_the_orch_rule_only(
        monkeypatch, repl, tmp_path, real_main_orch):
    """No resolver is not 'no rule': the shipped `*-orch` rule keeps deciding,
    and the configured main is simply invisible until the resolver is back.
    Never a guessed name (that is how a wrong pane gets typed into)."""
    configure_main(tmp_path, real_main_orch, "ops")
    add_pane(repl, "ops", "%2", "s1:0.2")
    monkeypatch.setattr(om, "_load_main_orch", lambda: None)
    assert om.main_orchestrator_profile() is None
    assert om.is_orch_profile("ops") is False
    assert om.is_orch_profile("other-orch") is True
    out = send(to="ops")
    assert out["reason"] == om.REASON_TARGET_NOT_ORCH and repl.calls == []


def test_resolver_that_raises_is_treated_as_no_resolver(monkeypatch):
    """An exception from the resolver must not escape into the tool call and
    must not be read as 'anything goes'."""
    class Broken:
        @staticmethod
        def main_profile(hscc_dir=None):
            raise RuntimeError("state document unreadable")

    monkeypatch.setattr(om, "_load_main_orch", lambda: Broken())
    assert om.main_orchestrator_profile() is None
    assert om.is_orch_profile("ops") is False
    assert om.is_orch_profile("ops-orch") is True


def test_main_orch_module_and_sibling_dir_are_reached_not_reimplemented():
    """The loader resolves through the sibling plugin dir, so a deployed tree
    (hscc-cluster next to hscc-roles) works and the answer is the real module."""
    module = om._load_main_orch()
    assert module is not None
    assert module.STATE_FILENAME == "main-orchestrator.json"
    assert callable(module.main_profile)
    from pathlib import Path
    assert module.__file__ and om.ROLES_DIRNAME in Path(module.__file__).parts


def test_refusal_detail_names_the_configured_main(repl, tmp_path, real_main_orch):
    """A refusal that only says "must end in '-orch'" misdirects an operator
    whose main orchestrator has another name."""
    configure_main(tmp_path, real_main_orch, "ops")
    detail = send(to="worker-engineer")["detail"]
    assert "must end in '-orch'" in detail and "'ops'" in detail


def test_module_never_spells_the_main_orchestrator_identity():
    """Structural guard, same doctrine as the verb-literal guard above: the
    main orchestrator's name and its state filename belong to
    `hscc-roles/main_orch.py`. Either spelled in this plugin is a second source
    of truth that will silently survive a rename — which is the bug this card is
    about. Proven to fire: adding `if name == "general"` back fails this test.
    """
    import re
    from pathlib import Path
    src = Path(om.__file__).read_text()
    for pattern in (r"""['"]general['"]""", r"""['"]main-orchestrator\.json['"]"""):
        assert not re.search(pattern, src), (
            f"orch_message.py hardcodes {pattern!r} — the main orchestrator's "
            "identity belongs to hscc-roles/main_orch.py; reach it through "
            "_load_main_orch()/main_orchestrator_profile() instead."
        )


# ── Rule 3 + queue-only (rule 2): formatting and the verb decision ─────────

def test_prefix_and_ref_formatting(repl):
    out = send(message="please rebase", ref_card="t_deadbeef")
    body = repl.calls[0]["text"]
    assert body == "[from demo-orch] re: t_deadbeef please rebase"
    assert out["ok"] is True


def test_busy_target_queues_idle_target_sends_plain(repl):
    """auto -> /queue while busy, plain when idle; `steer` is NEVER chosen by
    this tool (the receipt would carry '/steer' if it were)."""
    repl.busy = True
    out = send(message="heads up")
    assert out["mode"] == "queue" and out["verb"] == "/queue add "
    assert repl.calls[0]["mode"] == "auto"  # the tool asks for auto, never steer

    repl.busy = False
    repl.calls.clear()
    out = send(message="heads up")
    assert out["mode"] == "plain" and out["verb"] == ""
    assert repl.calls[0]["mode"] == "auto"


def test_legacy_receipt_without_verb_still_reports_a_verb(repl):
    """An older repl_inject receipt has `mode` but no `verb`; the reported verb
    is still derived from the canonical table, not left None."""
    repl.legacy = True
    repl.busy = True
    out = send(message="queued on purpose")
    assert out["mode"] == "queue" and out["verb"] == "/queue add "


# ── GAP-C-4: the mode→verb table is repl_inject's, never copied here ────────
#
# Before this card, orch_message carried its own fallback table (with a
# queue entry) for the case where the loaded repl_inject exposes no table.
# A second source of truth that is already stale
# — the real one has a `steer` entry the copy never did. The four tests below
# pin the two against diverging again: two read the CANONICAL table and compare
# (one through the tool, one against the helper), one pins the pre-parity
# (v2.14.0) table name, and one forbids a verb-prefix literal in the module's
# source at all.

@pytest.fixture(scope="session")
def real_repl():
    """The REAL ``hscc-api/repl_inject`` module, loaded by file path.

    By path (not ``import repl_inject``) so it is independent of sys.path and
    of the monkeypatched ``_load_repl_inject`` seam, and NOT a stub: these
    tests compare against the module that owns the verb table. Import only —
    nothing here resolves a pane or calls tmux, so the hermeticity contract
    above still holds.
    """
    import importlib.util
    from pathlib import Path
    path = (Path(om.__file__).resolve().parent.parent
            / "hscc-api" / "repl_inject.py")
    assert path.exists(), f"canonical repl_inject not found at {path}"
    spec = importlib.util.spec_from_file_location("_repl_inject_canonical", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_receipt_verb_equals_the_canonical_entry(repl, real_repl):
    """e2e through the tool: a receipt without `verb` reports exactly what
    repl_inject's own table says for the resolved mode — the assertion reads
    from the canonical table, so it cannot drift with it."""
    repl.legacy = True
    for busy, mode in ((True, "queue"), (False, "plain")):
        repl.busy = busy
        out = send(message="heads up")
        assert out["mode"] == mode
        assert out["verb"] == real_repl.VERB_PREFIX[mode]


def test_verb_for_mode_matches_the_canonical_table_for_every_entry(real_repl):
    """Helper-level pin: every key of the canonical table answers identically
    through `_verb_for_mode`, so the two cannot disagree."""
    for mode, canonical in real_repl.VERB_PREFIX.items():
        assert om._verb_for_mode(real_repl, mode) == canonical, mode
    # An unresolvable mode answers None (what the pre-fix code did too) — the
    # tool never invents a verb for a mode repl_inject does not know.
    assert om._verb_for_mode(real_repl, "auto") is None
    assert om._verb_for_mode(real_repl, "teleport") is None
    assert om._verb_for_mode(real_repl, "") is None


def test_pre_parity_table_is_read_by_name_not_reimplemented(real_repl):
    """A deployed PRE-PARITY hscc-api (the shipped 2.14.0 payload) exposes the
    verb table as `_PREFIX` and its receipts carry no `verb` — which is exactly
    when this path runs in production. The derived verbs must equal the
    canonical ones, including `steer`, which the deleted copy lacked."""
    class PreParityRepl:
        # The pre-parity private table's shape (extra `interrupt` key), with
        # the current canonical verbs so the two tables can be compared.
        _PREFIX = {"queue": "/queue add ", "steer": "/steer ",
                   "interrupt": "", "plain": ""}

    for mode, canonical in real_repl.VERB_PREFIX.items():
        assert om._verb_for_mode(PreParityRepl(), mode) == canonical, mode
    # And the legacy `interrupt` alias resolves the way that table says,
    # rather than being guessed at here.
    assert om._verb_for_mode(PreParityRepl(), "interrupt") == ""


def test_module_without_a_table_reports_no_verb_rather_than_a_guess():
    """A repl_inject too old to expose either table name yields no verb — same
    answer as before the fix, and never a verb invented in this module."""
    class NoTable:
        pass
    assert om._verb_for_mode(NoTable(), "queue") is None
    assert om._verb_for_mode(object(), "queue") is None

    class NotADict:
        VERB_PREFIX = "/queue-ish"
        _PREFIX = None
    assert om._verb_for_mode(NotADict(), "queue") is None


def test_orch_message_source_carries_no_verb_prefix_literal():
    """Structural guard (GAP-C-4): a quoted verb prefix in this module's source
    IS a second source of truth, whether or not it is reached.

    The pre-fix state proves the point: the copy was behaviourally reachable
    against a deployed 2.14.0 hscc-api and was already missing `steer`. Prose
    about /queue is fine (a quoted string that merely STARTS with the verb is
    what the guard catches, so `'send /queue now'` passes and `"/queue "` does
    not). Proven to fire: restoring the deleted line verbatim fails this test
    and nothing else.
    """
    import re
    from pathlib import Path
    src = Path(om.__file__).read_text()
    offenders = re.findall(r"""['"]/(?:queue|steer)""", src)
    assert not offenders, (
        f"orch_message.py hardcodes {offenders} — the mode→verb table belongs "
        "to hscc-api/repl_inject.py (VERB_PREFIX); read it from the loaded "
        "module via _verb_for_mode instead of copying it (GAP-C-4)."
    )


def test_missing_pane_refused(monkeypatch):
    """No pane at all for the target profile: refuse, deliver nothing."""
    monkeypatch.setattr(om, "_load_repl_inject", lambda: FakeRepl(panes=[]))
    out = send()
    assert out["ok"] is False and out["sent"] is False
    assert out["reason"] == om.REASON_NO_PANE


def test_ambiguous_pane_refused(repl):
    """Two panes for the same profile: the exactly-one-pane rule refuses."""
    repl.panes = [
        {"pane": "%0", "target": "s1:0.0", "profile": "other-orch"},
        {"pane": "%1", "target": "s1:0.1", "profile": "other-orch"},
    ]
    out = send()
    assert out["reason"] == om.REASON_AMBIGUOUS
    assert repl.calls and out["sent"] is False


def test_transport_error_maps_to_transport_reason(repl):
    repl.raise_exc = FakeInjectError("tmux", "tmux paste-buffer failed")
    out = send()
    assert out["reason"] == om.REASON_TRANSPORT and out["sent"] is False


def test_missing_repl_inject_refused(monkeypatch):
    """hscc-api not deployed next to hscc-cluster -> refusal, never a crash."""
    monkeypatch.setattr(om, "_load_repl_inject", lambda: None)
    out = send()
    assert out["reason"] == om.REASON_NO_REPL and out["sent"] is False


# ── Rule 4b: the `[from ` reply-loop guard ──────────────────────────────────

def test_signed_inbound_turn_may_not_answer_with_another_nudge(repl, tmp_path):
    """The turn was opened BY a signed nudge: auto-answering it with another
    orch_message is the loop the guard exists to stop."""
    out = send(user_task="[from other-orch] re: t_deadbeef please rebase first")
    assert out["ok"] is False and out["sent"] is False
    assert out["reason"] == om.REASON_LOOP
    assert repl.calls == []
    assert audit_lines(tmp_path)[0]["reason"] == om.REASON_LOOP


def test_unsigned_turn_from_the_same_orch_may_nudge(repl):
    """The guard is per-turn: a later, unsigned turn may open a nudge."""
    out = send(user_task="work the card; tell the other orchestrator when done")
    assert out["ok"] is True and out["sent"] is True


def test_loop_guard_silence_when_no_turn_text_is_documented(repl):
    """No `user_task` kwarg -> the guard has no evidence and stays silent (the
    documented no-op; pinned here so the behaviour cannot drift)."""
    out = send()
    assert out["ok"] is True


# ── Rule 4a: the per-pair hourly rate limit ─────────────────────────────────

def test_rate_limit_allows_N_and_refuses_N_plus_one(monkeypatch, repl, tmp_path):
    monkeypatch.setenv("HSCC_ORCH_MSG_MAX_PER_HOUR", "2")
    first = send(message="one")
    second = send(message="two")
    assert first["ok"] and second["ok"]
    assert first["remaining"] == 1 and second["remaining"] == 0

    third = send(message="three")
    assert third["ok"] is False and third["sent"] is False
    assert third["reason"] == om.REASON_RATE and third["remaining"] == 0
    assert len(repl.calls) == 2                      # the third never reached a pane
    lines = audit_lines(tmp_path)
    assert [l["delivered"] for l in lines] == [True, True, False]


def test_rate_limit_is_per_pair_and_per_window(repl, tmp_path):
    """Budget is per (sender, to) and only counts the last hour."""
    path = om.audit_path()
    now = time.time()
    old = json.dumps({"ts": round(now - 7200, 3), "from": "demo-orch",
                      "to": "other-orch", "ref_card": None, "chars": 3,
                      "delivered": True, "verb": "", "reason": "sent",
                      "pane": "%0", "body_preview": "old"})
    other_pair = json.dumps({"ts": round(now, 3), "from": "demo-orch",
                             "to": "third-orch", "ref_card": None, "chars": 3,
                             "delivered": True, "verb": "", "reason": "sent",
                             "pane": "%2", "body_preview": "other"})
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(old + "\n" + other_pair + "\n")
    # 6 fresh same-pair lines would exhaust the default budget; here only an
    # OUT-OF-WINDOW line and a different pair exist, so a send must be allowed.
    out = send(message="still allowed")
    assert out["ok"] is True and out["remaining"] == 5


def test_rate_count_fails_closed_on_unreadable_timestamp(repl, tmp_path):
    """A delivered line with no usable ts cannot be placed outside the window,
    so it COUNTS — under-counting would hand out extra budget."""
    path = om.audit_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for _ in range(6):
            fh.write(json.dumps({"ts": "not-a-time", "from": "demo-orch",
                                 "to": "other-orch", "delivered": True}) + "\n")
    out = send()
    assert out["reason"] == om.REASON_RATE and out["sent"] is False


# ── Rule 4 config surface ───────────────────────────────────────────────────

def test_max_per_hour_reads_cluster_json(tmp_path):
    (tmp_path / "cluster.json").write_text(
        json.dumps({"orch_message": {"max_per_hour": 2}}))
    assert om.max_per_hour() == 2


def test_max_per_hour_env_and_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HSCC_ORCH_MSG_MAX_PER_HOUR", "3")
    assert om.max_per_hour() == 3
    (tmp_path / "cluster.json").write_text(
        json.dumps({"orch_message": {"max_per_hour": 2}}))
    assert om.max_per_hour() == 2          # file wins over env
    (tmp_path / "cluster.json").write_text(
        json.dumps({"orch_message": {"max_per_hour": "two"}}))
    assert om.max_per_hour() == 3          # garbage value falls through to env
    monkeypatch.setenv("HSCC_ORCH_MSG_MAX_PER_HOUR", "0")
    assert om.max_per_hour() == om.DEFAULT_MAX_PER_HOUR   # 0 is not a budget
    monkeypatch.setenv("HSCC_ORCH_MSG_MAX_PER_HOUR", "abc")
    assert om.max_per_hour() == om.DEFAULT_MAX_PER_HOUR


def test_default_limit_is_six():
    assert om.DEFAULT_MAX_PER_HOUR == 6


# ── Rule 5: the audit line ──────────────────────────────────────────────────

def test_audit_line_shape_and_preview_cap(repl, tmp_path):
    """Audit = ts, from, to, ref_card, chars, delivered, verb + a body preview
    capped at 200 chars. The full body is deliberately NOT stored."""
    long_body = "x" * 500
    send(message=long_body, ref_card="t_deadbeef")
    lines = audit_lines(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    for key in ("ts", "from", "to", "ref_card", "chars", "delivered",
                "verb", "reason", "pane", "body_preview"):
        assert key in line, f"audit line missing {key!r}"
    assert line["from"] == "demo-orch" and line["to"] == "other-orch"
    assert line["ref_card"] == "t_deadbeef"
    assert line["chars"] == len(long_body)
    assert line["delivered"] is True and line["reason"] == "sent"
    assert line["verb"] == ""
    assert len(line["body_preview"]) == om.BODY_PREVIEW_CHARS == 200
    assert line["body_preview"].startswith("[from demo-orch] re: t_deadbeef ")
    assert (tmp_path / "state" / "orch_messages.jsonl").exists()


def test_unwritable_audit_log_refuses_before_touching_a_pane(monkeypatch, repl):
    """Fail-closed: the audit log is the only rate-limit state, so a nudge that
    could not be recorded is a nudge that cannot be counted — no send."""
    monkeypatch.setattr(om, "audit_writable", lambda path=None: False)
    out = send()
    assert out["ok"] is False and out["sent"] is False
    assert out["reason"] == om.REASON_AUDIT
    assert repl.calls == []


# ── Argument shape ──────────────────────────────────────────────────────────

def test_empty_message_and_bad_ref_card_refused(repl, tmp_path):
    assert send(message="   ")["reason"] == om.REASON_EMPTY
    assert send(ref_card="CARD-17")["reason"] == om.REASON_BAD_REF
    assert repl.calls == []
    assert [l["delivered"] for l in audit_lines(tmp_path)] == [False, False]


def test_ref_card_must_be_a_task_id_shape(repl):
    out = send(ref_card="t_zzzzzz")           # hex-only, like the fragment tool
    assert out["reason"] == om.REASON_BAD_REF
    assert repl.calls == []


# ── Registration (the __init__.py line, same style as test_fleet) ───────────

def test_orch_message_is_registered_in_init():
    import os
    from pathlib import Path
    init_src = (Path(os.path.dirname(os.path.abspath(om.__file__)))
                / "__init__.py").read_text()
    assert '"orch_message"' in init_src, "orch_message not registered in __init__.py"
    assert "_ORCH_TOOLS" in init_src
    assert "+ _ORCH_TOOLS" in init_src, "_ORCH_TOOLS not in the register() loop"
    assert "from . import orch_message" in init_src


def test_schema_shape():
    schema = om.ORCH_MESSAGE_SCHEMA
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"to", "message"}
    assert set(schema["properties"]) == {"to", "message", "ref_card"}
    assert schema["additionalProperties"] is False
