"""Tests for `hscc node pause|resume|paused` (card t_c28f4f14).

Hermetic by construction — the same rule the overlay and executor tests follow
(``nolive-rule.txt``): every host is a documentation placeholder (192.0.2.x,
RFC 5737); ``serving.load_serving``, ``node_pause_exec.fetch_cluster_status``
and ``util.run_cmd`` are all patched, so no test here reads the operator's
serving.json, runs sparkrun's status query, or spawns a single subprocess. The
autouse ``_isolate_hscc`` conftest fixture already seals ``node_pauses.HSCC_DIR``
and ``heal_breaker.HEAL_BREAKER_FILE``, so every overlay/strike write lands in
this test's tmp dir.

What this file owns that the executor's tests do not: the CLI contract.
The confirmation question that replaced `--apply` (y / n / bare Enter /
no-terminal / `--yes`), the preview `--dry-run` leaves behind, the two guards
printed verbatim and in order (eligibility, then orchestrator),
overlay-written-AFTER-stop, `-h` never reaching a mutating verb (B10), and the
`--until` typo that ``parse_until`` would otherwise swallow as open-ended.
"""

import ast
import io
import json
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import heal_breaker, node_pause_exec as npe
from hscc_daemon import node_pauses, serving, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
ORCH = "192.0.2.11"
W1 = "192.0.2.12"
W2 = "192.0.2.13"
#: Two single-node models on one machine — the 11-22 rule's ALLOWED shape:
#: colocated is fine, both park together.
W3 = "192.0.2.14"
#: One single-node model — the plain allowed case.
W4 = "192.0.2.15"
#: Declared by no serving.json unit and running nothing — the empty node.
QUIET = "192.0.2.30"

ORCH_RECIPE = "/srv/recipes/orchestrator.yaml"
WORKER_RECIPE = "/srv/recipes/worker.yaml"
SPAN_RECIPE = "/srv/recipes/worker-tp.yaml"

SERVING = {
    "port": 8000,
    "units": [
        {"id": "orch", "role": "orchestrator", "nodes": [ORCH], "port": 8000,
         "recipe": ORCH_RECIPE, "model": "meta-llm/Orch-8B", "tp": 1},
        {"id": "w1", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "wspan", "role": "worker", "nodes": [W1, W2], "port": 8200,
         "recipe": SPAN_RECIPE, "model": "meta-llm/Work-8B", "tp": 2},
        {"id": "w3a", "role": "worker", "nodes": [W3], "port": 8300,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "w3b", "role": "worker", "nodes": [W3], "port": 8310,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-9B", "tp": 1},
        {"id": "w4", "role": "worker", "nodes": [W4], "port": 8400,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-7B", "tp": 1},
    ],
}


def _group(cid, recipe, hosts, port, model="meta-llm/Work-8B", served=None,
           tp=None):
    meta = {"recipe": recipe, "hosts": list(hosts), "port": port,
            "model": model}
    if served:
        meta["served_model_name"] = served
    if tp:
        meta["tensor_parallel"] = tp
    return {cid: {"meta": meta,
                  "containers": [{"host": h, "role": "worker",
                                  "status": "running", "image": "img"}
                                 for h in hosts]}}


def fleet_payload():
    """One status snapshot of the whole fixture fleet."""
    groups = {}
    groups.update(_group("cid_orch", ORCH_RECIPE, [ORCH], 8000,
                         model="meta-llm/Orch-8B",
                         served="meta-llm/Orch-8B orchestrator-model"))
    groups.update(_group("cid_w1", WORKER_RECIPE, [W1], 8100,
                         served="meta-llm/Work-8B worker-model"))
    groups.update(_group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2,
                        served="meta-llm/Work-8B worker-model"))
    groups.update(_group("cid_w3a", WORKER_RECIPE, [W3], 8300,
                        model="meta-llm/Work-8B",
                        served="meta-llm/Work-8B worker-model"))
    groups.update(_group("cid_w3b", WORKER_RECIPE, [W3], 8310,
                        model="meta-llm/Work-9B",
                        served="meta-llm/Work-9B worker-model"))
    groups.update(_group("cid_w4", WORKER_RECIPE, [W4], 8400,
                        model="meta-llm/Work-7B",
                        served="meta-llm/Work-7B worker-model"))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_ops": [],
            "total_containers": 0, "host_count": 0}


class Recorder:
    """Fake ``util.run_cmd``: records argv, answers ok unless told otherwise."""

    def __init__(self, results=()):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, timeout=None, **kw):
        self.calls.append([str(c) for c in cmd])
        if self._results:
            return self._results.pop(0)
        return {"ok": True, "output": "", "stderr": ""}


@pytest.fixture
def fleet(monkeypatch):
    """Patch the three seams a node verb can reach: config, status, exec."""
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        lambda: (fleet_payload(), None))
    monkeypatch.setattr(util, "run_cmd", rec)
    return rec


def run(argv):
    """Drive the verb group in-process; returns (rc, stdout)."""
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            rc = unified._handle_node(list(argv))
    except SystemExit as e:  # handlers must RETURN, never exit
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


def main_run(monkeypatch, argv):
    """Drive ``main()`` so the dispatch branch itself is exercised."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


def seed_pause(host=W1, **kw):
    """Write a pause entry straight to the (sealed) overlay."""
    kw.setdefault("reason", "maintenance")
    kw.setdefault("units", ["w1", "wspan"])
    kw.setdefault("span_nodes", [W1, W2])
    kw.setdefault("stopped", True)
    return node_pauses.pause(host, **kw)


def overlay_bytes():
    """The overlay file's EXACT bytes (None when it does not exist yet).

    Byte-identity is the refusal proof: ``paused(host) is None`` only says this
    host is absent, while a refused pause must have changed nothing at all —
    not another host's entry, not a timestamp, not the file's existence.
    """
    try:
        with open(node_pauses.overlay_path(), "rb") as fh:
            return fh.read()
    except OSError:
        return None


@pytest.fixture
def tty(monkeypatch):
    """Put a terminal on stdin and TYPE the answers, one per verb call.

    Only ``_stdin_is_tty`` is patched. The answer goes in as typed characters,
    so the prompt travels through the real ``_ask_yes_no`` — these tests prove
    the exact question reaches the operator's terminal, which a fixture that
    patched the ask away could never show. An exhausted queue is EOF, which is
    an empty answer, which is NO.
    """
    def _attach(*answers):
        monkeypatch.setattr(unified, "_stdin_is_tty", lambda: True)
        buf = io.StringIO("".join(f"{a}\n" for a in answers))
        monkeypatch.setattr(sys, "stdin", buf)
        return buf
    return _attach


@pytest.fixture
def no_tty(monkeypatch):
    """No terminal: a pipe, cron, ssh -T, the agent tool. The refusal posture."""
    monkeypatch.setattr(unified, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(unified, "_ask_yes_no",
                        lambda prompt: (_ for _ in ()).throw(
                            AssertionError("asked with no terminal — "
                                           "_stdin_is_tty is not the gate")))


@pytest.fixture
def never_asks(monkeypatch):
    """A path that must not prompt: the question itself is the failure."""
    def boom(prompt):
        raise AssertionError(f"a preview/refusal must not ask (asked {prompt!r})")
    monkeypatch.setattr(unified, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(unified, "_ask_yes_no", boom)


# ── help is inert: it loads nothing, reads nothing, stops nothing ──────────
class TestHelpIsInert:
    """The B6/B10/B12 contract for this group: a help token can never reach a
    verb that stops live units."""

    @pytest.fixture(autouse=True)
    def _no_plan(self, monkeypatch):
        """Any status read at all is a failure on these paths."""
        def boom(*a, **kw):
            raise AssertionError("a help request must not read the fleet")
        monkeypatch.setattr(npe, "plan", boom)
        monkeypatch.setattr(npe, "fetch_cluster_status", boom)

    @pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
    def test_group_help_lists_all_three_verbs(self, argv):
        rc, out = run(argv)
        assert rc == 0, out
        assert "Node pause commands:" in out
        assert "hscc node pause <host> [flags]" in out
        assert "hscc node resume <host> [--yes] [--dry-run]" in out
        assert "hscc node paused [--json]" in out
        # The mutation safety is advertised, not implied: the verb asks before
        # it stops. And the two flags the 11-22 rule retired must be gone from
        # the help text entirely — a flag nobody parses must not be advertised.
        assert "asks" in out and "every model on it runs on it alone" in out
        for gone in ("--apply", "--span"):
            assert gone not in out, f"{gone} left the node verbs on 11-22"

    def test_a_leading_flag_is_not_a_subcommand(self):
        """Same refusal shape as `hscc cluster --whatever`: the token is not a
        verb, and a flag is never quietly read as a request for help."""
        rc, out = run(["--theme", "dark"])
        assert rc == 1, out
        assert "unknown node subcommand: --theme" in out
        assert "pause, resume, paused" in out

    @pytest.mark.parametrize("argv", [
        ["pause", "--help"], ["pause", "-h"],
        ["pause", W1, "--help"], ["pause", "--apply", "--force", "--help"],
        ["pause", "--apply", W1, "-h"],
        ["resume", "--help"], ["resume", W1, "--help"], ["resume", "-h"],
        ["paused", "--help"], ["paused", "--json", "-h"],
    ])
    def test_verb_help_wins_over_dispatch(self, argv):
        """A help token ANYWHERE in the tail prints usage and runs nothing."""
        rc, out = run(argv)
        assert rc == 0, out
        assert out.startswith("Usage: hscc node "), out
        assert not node_pauses.list_pauses(), "help must not record a pause"

    def test_short_flag_and_long_flag_print_the_same_bytes(self):
        for verb in ("pause", "resume", "paused"):
            long_rc, long_out = run([verb, "--help"])
            short_rc, short_out = run([verb, "-h"])
            assert (long_rc, long_out) == (short_rc, short_out)

    def test_unknown_subcommand_refuses_and_lists_the_verbs(self):
        rc, out = run(["bogus"])
        assert rc == 1
        assert "unknown node subcommand: bogus" in out
        assert "pause, resume, paused" in out

    def test_main_dispatches_the_group(self, monkeypatch):
        """`hscc node` resolves through main(), not only the handler."""
        rc, out = main_run(monkeypatch, ["node", "--help"])
        assert rc == 0, out
        assert "hscc node paused [--json]" in out

    def test_help_node_documents_the_confirmation(self, monkeypatch):
        """`hscc help node` prints the same COMMAND_HELP blurb the audit reads —
        and that blurb must describe the ask, not the retired `--apply`."""
        rc, out = main_run(monkeypatch, ["help", "node"])
        assert rc == 0, out
        assert "Subcommands: pause <host>  resume <host>  paused" in out
        assert "only y proceeds" in out
        assert "--dry-run previews" in out and "--yes acts" in out
        for gone in ("--apply", "--span"):
            assert gone not in out, f"{gone} left the node verbs on 11-22"

    def test_primary_help_names_the_verb(self, monkeypatch):
        """F5 surface honesty: a dispatched TOP_LEVEL_COMMAND is listed."""
        rc, out = main_run(monkeypatch, ["--help"])
        assert rc == 0, out
        assert "\n  node " in out


# ── `node paused` — read-only, and provably fleet-free ─────────────────────
class TestPaused:
    def test_empty_overlay_is_not_an_error(self, fleet):
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "No paused nodes" in out
        assert fleet.calls == []

    def test_never_reads_the_fleet(self, monkeypatch):
        """`paused` answers with sparkrun and serving both unreachable."""
        def boom(*a, **kw):
            raise AssertionError("paused must not read the fleet")
        monkeypatch.setattr(npe, "plan", boom)
        monkeypatch.setattr(serving, "load_serving", boom)
        seed_pause()
        rc, out = run(["paused"])
        assert rc == 0, out
        assert W1 in out

    def test_prints_the_recorded_fields(self):
        seed_pause(reason="disk swap", until="2h")
        rc, out = run(["paused"])
        assert rc == 0, out
        assert W1 in out
        assert "w1" in out and "wspan" in out
        assert "disk swap" in out
        assert "confirmed down" in out
        for field in ("since:", "until:"):
            assert field in out
        # The span names the node that is parked but not keyed.
        assert W2 in out

    def test_open_until_says_what_ends_it(self):
        seed_pause()
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "open-ended" in out

    def test_past_due_until_is_flagged(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        seed_pause(until=past)
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "PAST DUE" in out
        # The hint is a command the new grammar still accepts — `--apply` is
        # gone, so a hint that printed it would tell the operator to type a
        # flag the parser rejects.
        assert f"hscc node resume {W1}" in out
        assert "--apply" not in out

    def test_json_is_one_machine_readable_object(self):
        seed_pause(reason="disk swap", until="2h")
        rc, out = run(["paused", "--json"])
        assert rc == 0, out
        payload = json.loads(out)
        assert payload["count"] == 1
        row = payload["pauses"][0]
        assert row["host"] == W1
        assert row["units"] == ["w1", "wspan"]
        assert row["reason"] == "disk swap"
        assert row["stopped"] is True
        assert row["since"] and row["until"]
        assert row["span_nodes"] == [W1, W2]

    def test_json_of_nothing_is_a_zero_count(self):
        rc, out = run(["paused", "--json"])
        assert rc == 0, out
        assert json.loads(out) == {"count": 0, "pauses": []}

    def test_unknown_flag_is_refused_not_dropped(self):
        rc, out = run(["paused", "--verbose"])
        assert rc == 1, out
        assert "unknown flag: --verbose" in out


# ── `node pause` — the preview, the guards, the question, then stop + record ─
class TestPausePreview:
    """`--dry-run`: the same plan, no question, nothing recorded."""

    def test_dry_run_stops_nothing_and_records_nothing(self, fleet, never_asks):
        before = overlay_bytes()
        rc, out = run(["pause", W3, "--dry-run"])
        assert rc == 0, out
        assert fleet.calls == [], "a preview must not reach run_cmd"
        assert overlay_bytes() == before
        assert "Dry run" in out

    def test_dry_run_never_asks(self, fleet, never_asks):
        """Previewing is not an answer, so it must not consume one — and it
        must work with no terminal attached, which is where cron lives."""
        rc, out = run(["pause", W4, "--dry-run"])
        assert rc == 0, out
        assert "Pause " not in out

    def test_dry_run_prints_every_unit_even_for_a_refused_node(self, fleet,
                                                               never_asks):
        """The blast radius is on the page BEFORE the refusal, so the operator
        reads what the refusal is about."""
        rc, out = run(["pause", W1, "--dry-run"])
        assert rc == 1, out
        assert "w1" in out and "wspan" in out
        assert "every node this affects:" in out
        assert W2 in out, "the span sibling must be printed, not hidden"

    def test_dry_run_prints_the_reason_and_the_deadline(self, fleet, never_asks):
        """Everything the operator is being asked to approve: host, every model
        it will stop, the reason, the deadline."""
        rc, out = run(["pause", W4, "--dry-run", "--reason", "disk swap",
                       "--until", "2h"])
        assert rc == 0, out
        assert f"{W4}: 1 unit(s) on this node" in out
        assert "w4" in out
        assert "reason:  disk swap" in out
        assert "until:   open-ended" not in out
        assert out.splitlines()[-1].startswith("Dry run:")

    def test_dry_run_without_until_says_what_ends_the_pause(self, fleet,
                                                            never_asks):
        rc, out = run(["pause", W4, "--dry-run"])
        assert rc == 0, out
        assert "reason:  (none given)" in out
        assert "open-ended" in out

    def test_missing_host_is_a_usage_error(self, fleet):
        rc, out = run(["pause"])
        assert rc == 1, out
        assert "needs exactly one host" in out


class TestPauseEligibilityRefusals:
    """The rule at the CLI: exit 1, nothing stopped, nothing recorded."""

    def test_a_multi_node_model_refuses_and_names_it_and_its_peer(self, fleet,
                                                                  tty):
        """The case #561/#567 let through with --span. Now a plain refusal —
        and never a question: a `y` sitting in the queue must not be consumed,
        because an answer cannot unlock this."""
        answers = tty("y")
        before = overlay_bytes()
        rc, out = run(["pause", W1, "--yes"])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "wspan" in out, "the refusal names the model that blocks it"
        assert W2 in out, "and the other node that model lives on"
        assert fleet.calls == [], "a refused pause must not stop anything"
        assert overlay_bytes() == before
        assert "Pause " not in out, "a refusal is not a question to answer"
        assert answers.tell() == 0, \
            "the refusal never reached the question — nothing was read from stdin"

    def test_force_does_not_unlock_a_shared_model(self, fleet):
        """--force is the orchestrator guard's override and nothing else."""
        rc, out = run(["pause", W1, "--yes", "--force"])
        assert rc == 1, out
        assert "REFUSED" in out
        assert fleet.calls == [] and node_pauses.paused(W1) is None

    def test_two_colocated_single_node_models_are_not_a_refusal(self, fleet,
                                                                never_asks):
        """The 11-22 amendment at the CLI: a COUNT of units must not refuse.
        Both models park together — that is what parking the machine means."""
        rc, out = run(["pause", W3, "--dry-run"])
        assert rc == 0, out
        assert "REFUSED" not in out
        assert "w3a" in out and "w3b" in out

    def test_a_node_running_nothing_refuses_nothing_to_pause(self, fleet):
        before = overlay_bytes()
        rc, out = run(["pause", QUIET, "--yes"])
        assert rc == 1, out
        assert f"REFUSED: {QUIET} runs nothing — nothing to pause." in out
        assert fleet.calls == []
        assert overlay_bytes() == before
        assert node_pauses.paused(QUIET) is None

    def test_an_unreadable_node_is_refused_not_read_as_empty(self, fleet,
                                                             monkeypatch):
        """Fail-closed, and worded by the shared check rather than the verb:
        'could not enumerate' must never collapse into 'runs nothing'."""
        monkeypatch.setattr(npe, "fetch_cluster_status",
                            lambda: (None, "sparkrun status query failed"))
        rc, out = run(["pause", W1, "--yes"])
        assert rc == 1, out
        assert "could not be enumerated" in out
        assert "sparkrun status query failed" in out
        assert fleet.calls == [] and node_pauses.paused(W1) is None

    def test_the_span_flag_is_gone_from_the_grammar(self, fleet):
        """`--span` must not survive as an accepted no-op: muscle memory gets
        told the flag is unknown, not silently ignored."""
        rc, out = run(["pause", W1, "--span"])
        assert rc == 1, out
        assert "unknown flag: --span" in out
        assert fleet.calls == []

    def test_the_apply_flag_is_gone_from_the_grammar(self, fleet):
        """Same for `--apply`: the question replaced it. A flag the parser
        ignored would read as 'the operator never asked'."""
        rc, out = run(["pause", W4, "--apply"])
        assert rc == 1, out
        assert "unknown flag: --apply" in out
        assert fleet.calls == []

    def test_yes_and_dry_run_contradict_and_are_refused_together(self, fleet):
        rc, out = run(["pause", W4, "--yes", "--dry-run"])
        assert rc == 1, out
        assert "contradict" in out
        assert fleet.calls == []

    def test_unparseable_until_refuses_instead_of_parking_forever(self, fleet):
        """node_pauses.parse_until degrades garbage to open-ended; a CLI typo
        must not silently become a node that never comes back."""
        rc, out = run(["pause", W4, "--yes", "--until", "2days"])
        assert rc == 1, out
        assert "not a duration" in out
        assert fleet.calls == []
        assert node_pauses.paused(W4) is None

    def test_dangling_value_flag_is_refused(self, fleet):
        rc, out = run(["pause", W4, "--until"])
        assert rc == 1, out
        assert "--until needs a value" in out

    def test_theme_is_refused_because_it_would_do_nothing(self, fleet):
        rc, out = run(["pause", W4, "--theme", "dark"])
        assert rc == 1, out
        assert "--theme" in out


class TestPauseConfirmation:
    """The question IS the mutation safety (operator 11-22, replacing --apply).

    `--yes` is the scripted answer, never the scripted *absence* of one: a
    verb with no terminal and no `--yes` must refuse, or every cron entry on
    the fleet becomes a pause waiting to fire.
    """

    def test_the_question_is_asked_verbatim(self, fleet, tty):
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 0, out
        assert f"Pause {W3} and stop these 2 model(s)? [y/N]" in out

    def test_the_plan_is_printed_before_the_question(self, fleet, tty):
        """An operator answers against what they can read, so the order is the
        contract: plan first, then the ask."""
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 0, out
        assert out.index(f"{W3}: 2 unit(s) on this node") \
            < out.index(f"Pause {W3} and stop these 2 model(s)?")

    def test_y_proceeds_and_stops_both_models(self, fleet, tty):
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 0, out
        assert len(fleet.calls) == 2, fleet.calls
        assert all("stop" in " ".join(c) for c in fleet.calls)

    def test_capital_Y_proceeds_too(self, fleet, tty):
        tty("Y")
        rc, out = run(["pause", W4])
        assert rc == 0, out
        assert len(fleet.calls) == 1

    @pytest.mark.parametrize("answer", ["", "n", "N", "yes", "yEs", "y y", " "])
    def test_anything_but_y_aborts_and_records_nothing(self, fleet, tty, answer):
        """The default of [y/N] is N — including a bare Enter, and including
        strings that merely look like a yes."""
        before = overlay_bytes()
        tty(answer)
        rc, out = run(["pause", W3])
        assert rc == 1, (answer, out)
        assert "Aborted" in out, out
        assert fleet.calls == []
        assert overlay_bytes() == before
        assert node_pauses.paused(W3) is None

    def test_no_terminal_refuses_without_yes(self, fleet, no_tty):
        """cron, launchd, ssh -T, a pipe: nobody to ask, so no pause — and the
        refusal says how a script means it."""
        before = overlay_bytes()
        rc, out = run(["pause", W3])
        assert rc == 1, out
        assert "REFUSED" in out and "--yes" in out
        assert fleet.calls == [] and overlay_bytes() == before
        assert node_pauses.paused(W3) is None

    def test_yes_is_the_script_posture_and_says_so(self, fleet, no_tty):
        rc, out = run(["pause", W3, "--yes"])
        assert rc == 0, out
        assert "--yes given" in out, "the report must record nobody was asked"
        assert len(fleet.calls) == 2

    def test_a_refusal_never_reaches_the_question(self, fleet, never_asks):
        """Both guards are answered before the ask, so a refused node cannot
        be talked past a prompt."""
        rc, out = run(["pause", W1, "--yes"])
        assert rc == 1, out
        assert "Pause " not in out


# ── the uniform pool: EVERY replica advertises the orchestrator alias ───────
# The defect this section pins (#567 → t_51a11d16, the operator's live dry-run):
# ``serving._served_model_name`` stamps BOTH role aliases on every unit of a
# uniform pool (hscc_daemon/serving.py:444-464), so a per-unit "serves the
# orchestrator model" flag is True on EVERY node — and the verb refused the
# whole fleet, making the feature's first real use require --force. The applied
# template declares ONE orchestrator_node; the rest are interchangeable behind
# the proxy. Every host here is an RFC 5737 placeholder (nolive-rule.txt).
POOL_A = "192.0.2.21"   # the applied template's declared orchestrator_node
POOL_B = "192.0.2.22"
POOL_C = "192.0.2.23"
POOL_D = "192.0.2.24"
POOL_NODES = (POOL_A, POOL_B, POOL_C, POOL_D)
POOL_RECIPE = "/srv/recipes/pool.yaml"
POOL_MODEL = "meta-llm/Pool-8B"
#: Both aliases on one unit — what a uniform-pool replica actually advertises.
#: ``hscc-cluster/templates/4node/flash-next-all.yaml`` says it outright:
#: "every unit advertises BOTH ``orchestrator-model`` AND ``worker-model``, so
#: each alias load-balances across ALL FOUR units and nothing cares which node
#: is nominally the orchestrator".
POOL_SERVED = f"{POOL_MODEL} worker-model orchestrator-model"
#: A uniform pool gives every unit a DISTINCT port (8000..8003) so proxy
#: discovery does not collapse the replicas into one backend — the fixture
#: carries that shape rather than four units sharing :8000.
POOL_PORTS = tuple(8000 + i for i in range(len(POOL_NODES)))

POOL_SERVING = {
    "port": POOL_PORTS[0],
    "uniform_pool": True,
    "units": [{"id": f"pool-{i}",
               "role": "orchestrator" if i == 0 else "worker",
               "nodes": [h], "port": p, "recipe": POOL_RECIPE,
               "model": POOL_MODEL, "tp": 1}
              for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS))],
}


def pool_payload(down=()):
    """One sweep of the pool; ``down`` hosts are absent from it entirely."""
    groups = {}
    for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS)):
        if h in down:
            continue
        groups.update(_group(f"cid_pool_{i}", POOL_RECIPE, [h], p,
                             model=POOL_MODEL, served=POOL_SERVED))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_ops": [],
            "total_containers": 0, "host_count": 0}


def write_applied_state(tmp_path, node, template="4node-flash-next-all"):
    """The applied-template record at the path the SANCTIONED reader uses.

    The conftest redirects ``~/.hscc`` to this test's tmp dir, so writing the
    real file here makes ``declaration_agreement.load_applied_state`` — the one
    reader the guard is allowed to use — find a real record. Nothing is patched
    on the reader itself, which is the point: a guard that re-parsed the file by
    hand would pass a patched-seam test and fail on a real apply. The template
    name is the one the operator's live dry-run was refused on.
    """
    path = tmp_path / "hscc" / "applied_template.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "template": template,
        "applied_at": "2026-11-22T00:00:00",
        "orchestrator_node": node,
        "uniform_pool": True,
        "units": len(POOL_NODES),
    }))
    return path


@pytest.fixture
def pool(monkeypatch, tmp_path):
    """Uniform-pool fleet + an applied record naming POOL_A the orchestrator."""
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: POOL_SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        lambda: (pool_payload(), None))
    monkeypatch.setattr(util, "run_cmd", rec)
    write_applied_state(tmp_path, POOL_A)
    return rec


def park(*hosts):
    """Hold nodes with the pause OVERLAY (parked, still answering the sweep).

    The distinction the guard must get right: a parked replica is not a healthy
    server, and it is not the same state as a node missing from the sweep. The
    autouse conftest fixture has already redirected ``node_pauses.HSCC_DIR`` to
    this test's tmp dir, so this writes an overlay entry and nothing else.
    """
    for h in hosts:
        node_pauses.pause(h, units=[f"pool-{POOL_NODES.index(h)}"],
                          reason="maintenance")


class TestUniformPoolOrchestratorGuard:
    """The four acceptance cases the operator's dry-run could not get past.

    All four replicas here are single solo units, so the ELIGIBILITY check
    allows every one of them and these tests are about the guard they compose
    with (11-22: eligibility first, then the orchestrator guard, either refusal
    stopping everything).
    """

    def test_pausing_a_replica_is_allowed_and_says_who_still_serves_it(
            self, pool, never_asks):
        rc, out = run(["pause", POOL_B, "--dry-run"])
        assert rc == 0, out
        assert "REFUSED" not in out, out
        # 4 healthy replicas, this one goes down → 3 others keep the alias up.
        assert "orchestrator-model stays served by 3 other node(s)" in out, out
        for other in (POOL_A, POOL_C, POOL_D):
            assert other in out, f"{other} must be named as a remaining server"
        assert pool.calls == [], "a preview must not shell out"
        assert node_pauses.paused(POOL_B) is None

    def test_declared_orchestrator_node_refuses_with_the_declared_reason(
            self, pool):
        rc, out = run(["pause", POOL_A])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "declared orchestrator node" in out, out
        # The WHY cites the record the reader actually read, not a model name.
        assert "orchestrator_node=" in out and POOL_A in out, out
        assert "4node-flash-next-all" in out, "the refusal names the applied template"
        assert pool.calls == []
        assert node_pauses.paused(POOL_A) is None

    def test_sole_healthy_server_refuses_even_though_replicas_exist(
            self, pool, monkeypatch):
        """Others DOWN (missing from the sweep), not paused: this host is the
        only healthy alias server left, so the pause must be refused."""
        monkeypatch.setattr(
            npe, "fetch_cluster_status",
            lambda: (pool_payload(down={POOL_A, POOL_C, POOL_D}), None))
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        assert pool.calls == []
        assert node_pauses.paused(POOL_B) is None

    def test_parked_replicas_are_not_counted_as_healthy_servers(self, pool,
                                                                monkeypatch):
        """Others PAUSED (still in the sweep), not down: the overlay wins."""
        park(POOL_A, POOL_C, POOL_D)
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        # The refusal must explain that the visible replicas are parked, or the
        # operator reads a healthy fleet and an unexplained refusal.
        assert "paused and therefore not counted" in out, out
        assert POOL_C in out and POOL_D in out, out
        assert pool.calls == []

    def test_force_overrides_the_declared_node_refusal(self, pool):
        """`--force` answers the ORCHESTRATOR guard, and only it — and it does
        not skip the confirmation question, so this is `--yes --force`."""
        rc, out = run(["pause", POOL_A, "--yes", "--force"])
        assert rc == 0, out
        assert "note: --force given" in out, out
        assert "declared orchestrator node" in out, out
        assert pool.calls, "--yes --force must stop"
        entry = node_pauses.paused(POOL_A)
        assert entry is not None and entry["stopped"] is True

    def test_force_overrides_the_no_healthy_server_refusal(self, pool,
                                                          monkeypatch):
        monkeypatch.setattr(
            npe, "fetch_cluster_status",
            lambda: (pool_payload(down={POOL_A, POOL_C, POOL_D}), None))
        rc, out = run(["pause", POOL_B, "--yes", "--force"])
        assert rc == 0, out
        assert "note: --force given" in out, out
        assert "would leave no healthy unit" in out, out
        assert pool.calls, "--yes --force must stop"

    def test_the_note_prints_for_a_preview_too(self, pool, never_asks):
        """The ALLOW note is part of the plan's answer, not of the stop: it
        must print with no confirmation at all (the operator's dry-run is what
        was broken in #567, and --dry-run is where it lives now)."""
        rc, out = run(["pause", POOL_C, "--dry-run"])
        assert rc == 0, out
        assert "Dry run: nothing stopped" in out
        assert "orchestrator-model stays served by" in out

    def test_a_non_orchestrator_replica_still_parks_when_confirmed(self, pool):
        """The stays-served note is not a refusal in disguise: a plain replica
        parks on `--yes`, and the note is still in the report."""
        rc, out = run(["pause", POOL_B, "--yes"])
        assert rc == 0, out
        assert "orchestrator-model stays served by" in out
        assert len(pool.calls) == 1, pool.calls
        entry = node_pauses.paused(POOL_B)
        assert entry is not None and entry["stopped"] is True

    def test_a_plain_pause_prints_no_orchestrator_note(self, fleet, never_asks):
        """Non-uniform template, worker node: unchanged behaviour — no note, no
        refusal, and no claim about a model this node does not serve."""
        rc, out = run(["pause", W4, "--dry-run"])
        assert rc == 0, out
        assert "orchestrator" not in out.lower(), out

    def test_non_uniform_sole_orchestrator_still_refuses(self, fleet):
        """Non-uniform template (one dedicated orchestrator unit, no applied
        record): the healthy-count rule reproduces the old refusal."""
        rc, out = run(["pause", ORCH])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        assert fleet.calls == []

    def test_no_applied_record_is_reported_not_silently_ignored(self, pool,
                                                                monkeypatch,
                                                                tmp_path,
                                                                never_asks):
        """A fleet with no applied-template record (hscc-bootstrap writes
        serving.json by hand) must not lose the healthy-count guard, and must
        say out loud that the declaration could not be consulted."""
        (tmp_path / "hscc" / "applied_template.json").unlink()
        rc, out = run(["pause", POOL_B, "--dry-run"])
        assert rc == 0, out
        assert "orchestrator-model stays served by 3 other node(s)" in out, out
        assert "applied-template record could not be consulted" in out, out


class TestPauseConfirmed:
    """The confirmed path: stop everything the node owns, THEN record.

    W3 carries two single-node models on purpose — a pause takes the whole
    machine down in one call, which is what the old span-only framing got
    wrong, and it is the only place in this file that proves `stop_units`
    really loops.
    """

    def test_confirmation_stops_every_unit_then_records_the_overlay(self, fleet,
                                                                    tty):
        tty("y")
        rc, out = run(["pause", W3, "--reason", "disk swap", "--until", "2h"])
        assert rc == 0, out
        assert len(fleet.calls) == 2, fleet.calls
        assert all("stop" in " ".join(c) for c in fleet.calls)
        entry = node_pauses.paused(W3)
        assert entry is not None
        assert entry["units"] == ["w3a", "w3b"]
        assert entry["span_nodes"] == [W3]
        assert entry["reason"] == "disk swap"
        assert entry["stopped"] is True
        assert entry["since"] and entry["until"]
        # --until 2h reached the overlay as a deadline ~2h out, not open-ended.
        until = datetime.fromisoformat(entry["until"])
        lead = (until - datetime.now(timezone.utc)).total_seconds()
        assert 3400 < lead < 7400, entry["until"]

    def test_the_report_names_every_model_it_stopped(self, fleet, tty):
        """The agent tool reports this text verbatim, so the operator's own
        report has to carry the list — not just a count."""
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 0, out
        line = [ln for ln in out.splitlines() if ln.startswith("paused ")][-1]
        assert "w3a" in line and "w3b" in line, line

    def test_a_single_solo_unit_parks_with_one_stop(self, fleet, tty):
        tty("y")
        rc, out = run(["pause", W4])
        assert rc == 0, out
        assert len(fleet.calls) == 1, fleet.calls
        entry = node_pauses.paused(W4)
        assert entry is not None and entry["units"] == ["w4"]
        assert entry["span_nodes"] == [W4] and entry["stopped"] is True

    def test_reason_survives_spaces_as_one_token(self, fleet, tty):
        tty("y")
        rc, _ = run(["pause", W3, "--reason", "replace the disk"])
        assert rc == 0, _
        assert node_pauses.paused(W3)["reason"] == "replace the disk"

    def test_stop_happens_before_the_overlay_is_written(self, fleet, tty,
                                                        monkeypatch):
        """Ordering is the overlay's documented contract: a pause recorded
        before the stop would let a reader believe units are down that are not.
        """
        seen = {}
        real_pause = node_pauses.pause

        def spy_pause(*a, **kw):
            seen["argv_at_record_time"] = list(fleet.calls)
            return real_pause(*a, **kw)
        monkeypatch.setattr(node_pauses, "pause", spy_pause)
        tty("y")
        rc, _ = run(["pause", W3])
        assert rc == 0
        assert len(seen["argv_at_record_time"]) == 2, \
            "recorded before both models stopped"

    def test_a_failing_stop_records_stopped_false_and_exits_nonzero(self, fleet,
                                                                    tty,
                                                                    monkeypatch):
        monkeypatch.setattr(util, "run_cmd",
                            Recorder([{"ok": False, "output": "",
                                       "stderr": "sparkrun stop failed"}]))
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 1, out
        assert "did not stop cleanly" in out
        entry = node_pauses.paused(W3)
        # The pause IS in force (the heal paths must skip the node); only the
        # "units confirmed down" claim is withheld.
        assert entry is not None and entry["stopped"] is False

    def test_overlay_write_failure_is_reported_as_failure(self, fleet, tty,
                                                          monkeypatch):
        def boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(node_pauses, "save", boom)
        tty("y")
        rc, out = run(["pause", W3])
        assert rc == 1, out
        assert "did not reach disk" in out
        assert len(fleet.calls) == 2, "the stop really did happen — say so"
        # The hint must be a command the new grammar still accepts.
        assert "--apply" not in out and f"hscc node pause {W3}" in out


# ── `node resume` — restart the recorded span, then clear ─────────────────
class TestResume:
    def test_unpaused_host_is_a_no_op(self, fleet):
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert "not paused" in out
        assert fleet.calls == []

    # ── the same question, the same default ────────────────────────────────
    def test_the_resume_question_is_asked_verbatim(self, fleet, tty):
        seed_pause(units=["w1", "wspan"])
        tty("y")
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert f"Resume {W1} and restart these 2 model(s)? [y/N]" in out

    def test_the_resume_list_is_printed_before_the_question(self, fleet, tty):
        seed_pause(units=["w1", "wspan"])
        tty("y")
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert out.index("pause recorded 2 unit(s)") \
            < out.index(f"Resume {W1} and restart these 2 model(s)?")

    def test_n_keeps_the_pause_and_restarts_nothing(self, fleet, tty):
        seed_pause()
        before = overlay_bytes()
        tty("n")
        rc, out = run(["resume", W1])
        assert rc == 1, out
        assert "Aborted" in out
        assert fleet.calls == []
        assert overlay_bytes() == before, "the record is the node's shield"

    def test_bare_enter_keeps_the_pause_too(self, fleet, tty):
        seed_pause()
        tty("")
        rc, out = run(["resume", W1])
        assert rc == 1, out
        assert fleet.calls == [] and node_pauses.paused(W1) is not None

    def test_no_terminal_refuses_a_resume_without_yes(self, fleet, no_tty):
        seed_pause()
        rc, out = run(["resume", W1])
        assert rc == 1, out
        assert "REFUSED" in out and "--yes" in out
        assert fleet.calls == [] and node_pauses.paused(W1) is not None

    def test_a_declined_answer_rebuilds_nothing_at_all(self, fleet, tty,
                                                      monkeypatch):
        """Answering n must cost the fleet NOTHING — not even the status sweep
        the command rebuild needs. That is why the gate sits ahead of plan()."""
        seed_pause()
        seen = []
        monkeypatch.setattr(npe, "plan",
                            lambda host, **kw: seen.append(host) or {
                                "ok": False, "units": [], "span_nodes": [],
                                "error": "must not be reached"})
        tty("n")
        rc, out = run(["resume", W1])
        assert rc == 1, out
        assert seen == [], "the fleet was swept for an answer nobody gave"

    def test_yes_and_dry_run_contradict_for_resume_too(self, fleet):
        seed_pause()
        rc, out = run(["resume", W1, "--yes", "--dry-run"])
        assert rc == 1, out
        assert "contradict" in out
        assert fleet.calls == []

    def test_span_sibling_points_at_the_owner_instead_of_acting(self, fleet):
        seed_pause()  # keyed W1, spans W2
        rc, out = run(["resume", W2, "--yes"])
        assert rc == 1, out
        assert "span of the pause on" in out and W1 in out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is not None, "must not clear the owner"
        assert "--apply" not in out, "the hint must use the new grammar"

    def test_dry_run_prints_the_ensure_command_and_changes_nothing(
            self, fleet, never_asks):
        seed_pause()
        rc, out = run(["resume", W1, "--dry-run"])
        assert rc == 0, out
        assert fleet.calls == [], "a preview must not restart anything"
        assert "WOULD RESUME" in out
        assert "--ensure" in out, "resume re-issues the declared workload"
        assert "Resume " not in out, "a preview must not ask"
        assert node_pauses.paused(W1) is not None

    def test_yes_restarts_then_clears_pause_and_strikes(self, fleet):
        # Past the breaker's cap, so the node is genuinely SUSPENDED before the
        # resume — clearing three strikes would prove nothing (HEAL_BREAKER_MAX
        # is 6; suspended() is 0.0 below it). The slate is earned BEFORE the
        # pause: #568 made note_heal a no-op on a node the overlay holds, so a
        # post-pause slate could never trip the breaker. That a slate earned
        # while healing survives the pause is exactly the contract resume clears.
        for _ in range(7):
            heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 7
        assert heal_breaker.suspended(W1) > 0.0, \
            "the fixture must trip the breaker for this to mean anything"
        seed_pause()
        # The pause must be in force at the moment of the resume, so this is a
        # real "clear strikes THROUGH a pause" case, not a plain unparked resume.
        assert node_pauses.paused(W1) is not None
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 0, out
        assert len(fleet.calls) == 2, fleet.calls
        assert all("--ensure" in " ".join(c) for c in fleet.calls)
        assert node_pauses.paused(W1) is None, "pause must be cleared"
        assert heal_breaker.strike_count(W1) == 0, "strikes must be cleared"
        assert heal_breaker.suspended(W1) == 0.0
        assert "healing resumes now" in out

    def test_a_failing_restart_keeps_the_pause_and_the_strikes(self, fleet,
                                                               monkeypatch):
        # One strike earned BEFORE the pause (note_heal is a no-op on a node the
        # overlay holds since #568). This test guards that a FAILED restart keeps
        # BOTH the pause and that strike — heal_breaker.clear() runs only after a
        # successful restart, so clearing the strike here would be the bug.
        heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 1, (
            "the strike must be EARNED before the pause — without this pin "
            "the test cannot show the slate the failed resume keeps was "
            "actually accrued pre-pause")
        seed_pause()
        # The pause must be in force at the moment of the resume, or "keeps the
        # pause" would be vacuous.
        assert node_pauses.paused(W1) is not None
        monkeypatch.setattr(util, "run_cmd",
                            Recorder([{"ok": False, "output": "",
                                       "stderr": "ensure failed"}]))
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 1, out
        assert "failed to restart" in out
        assert node_pauses.paused(W1) is not None, (
            "a half-started node stays parked")
        assert heal_breaker.strike_count(W1) == 1, (
            "a failed resume must NOT clear the strike it did not earn")

    def test_a_unit_with_no_rebuildable_command_refuses_and_keeps_pause(
            self, fleet):
        seed_pause(units=["w1", "vanished"])
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "vanished" in out
        assert fleet.calls == [], "never guess a launch command"
        assert node_pauses.paused(W1) is not None

    def test_a_pause_with_no_units_clears_on_yes(self, fleet, never_asks):
        seed_pause(units=[], span_nodes=[])
        rc, out = run(["resume", W1, "--dry-run"])
        assert rc == 0, out
        assert "nothing to restart" in out
        assert node_pauses.paused(W1) is not None, "a preview keeps it"
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 0, out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is None

    def test_failed_status_read_keeps_the_pause(self, fleet, monkeypatch):
        seed_pause()
        monkeypatch.setattr(npe, "fetch_cluster_status",
                            lambda: (None, "sparkrun status query failed"))
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 1, out
        assert "cannot rebuild" in out
        assert node_pauses.paused(W1) is not None

    def test_resume_without_a_recorded_reason_still_clears_strikes(self,
                                                                   fleet):
        """Clearing is unconditional on a successful restart: a paused node
        that comes back must not still be carrying old strikes."""
        # Strike BEFORE the park, and prove it landed. With the pause seeded
        # first this case was green on main while proving nothing: note_heal is
        # a no-op on a node the overlay holds (#568), so the slate it "cleared"
        # had never been written and both assertions below held on an empty
        # ledger. Same law as the two tests above, same reason.
        heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 1, (
            "the strike must be EARNED before the pause — without this pin the "
            "resume below clears a slate nobody recorded")
        seed_pause()
        assert node_pauses.paused(W1) is not None, \
            "the pause must be in force for the resume, or this is no resume"
        rc, _ = run(["resume", W1, "--yes"])
        assert rc == 0
        assert heal_breaker.strike_count(W1) == 0
        assert heal_breaker.suspended(W1) == 0.0


# ── the fixture-ordering class that made these tests red ───────────────────
class TestNoStrikeAccrualBehindAPause:
    """Guard, not a behaviour test: no fixture in THIS file may expect heal
    strikes to accrue for a node its own setup has already parked.

    ``heal_breaker.note_heal()`` deliberately accrues NOTHING while the
    node-pause overlay covers the node — no strike, no ledger write, ``tripped``
    False (#568; its own docstring, pinned by
    ``test_heal_breaker_node_pause_t_e6ee7e60.py``). A fixture that seeds the
    pause BEFORE the strikes therefore builds no slate at all, and every later
    ``strike_count``/``suspended`` assertion reads a ledger nothing wrote: two
    tests here were red on main that way and a third passed vacuously, and the
    tool tests picked up the same shape from the same merge order. This guard is
    why the class cannot come back quietly.

    It reads THIS file's own source, so the pin travels with the fixtures and
    needs no production seam, and it fires only where a test DEMANDS a non-empty
    slate (``strike_count(...) == n`` with n non-zero, ``["count"] == n``, or
    ``suspended(...) > x``). A test asserting the ABSENCE of accrual for a parked
    node is the contract rather than a violation, so ``strike_count(...) == 0``
    stays perfectly legal to write — that is what keeps this guard from ever
    eating the pin it is standing on.

    Matching is host-keyed, so a fixture may legitimately park one node and keep
    striking another. A pause seeded with no host argument (``seed_pause()``
    parks this file's default) matches any host rather than nothing.
    """

    # Leaf names: what parks a node, what earns a strike, what reads the slate.
    PARKING = ("pause", "seed_pause", "park", "node_pause")
    ACCRUING = "note_heal"
    READS = ("strike_count", "suspended")

    @staticmethod
    def _leaf(call):
        """``heal_breaker.note_heal`` / ``seed_pause`` -> the last dotted name."""
        node, leaf = call.func, ""
        while isinstance(node, ast.Attribute):
            leaf, node = node.attr, node.value
        if not leaf and isinstance(node, ast.Name):
            leaf = node.id
        return leaf

    @classmethod
    def _host(cls, call, source):
        """Source text of the host a call parks or strikes: the first positional
        argument, else a ``host=`` keyword, else ``"*"``. A pause seeded with
        neither (``seed_pause()``) parks this file's default host, so it has to
        match any host rather than none."""
        if call.args:
            return ast.get_source_segment(source, call.args[0])
        for kw in call.keywords:
            if kw.arg == "host":
                return ast.get_source_segment(source, kw.value)
        return "*"

    @staticmethod
    def _same(a, b):
        """Same host? Compares NAME TOKENS, not substrings, so `W1` is never
        mistaken for a `W10`/`W12`, while a dict argument such as
        ``{"host": W1}`` still matches `W1`."""
        if "*" in (a, b):
            return True
        tokens = lambda text: {  # noqa: E731 - tiny local helper, used twice
            t for t in re.findall(r"\d+\.\d+\.\d+\.\d+|[A-Za-z_]\w*", text)}
        return bool(tokens(a) & tokens(b))

    @staticmethod
    def _demands(comparison):
        """True when the comparison REQUIRES a non-empty slate: ``> x``, ``!= n``,
        or ``== n`` with a truthy n. ``== 0`` is the suppression contract."""
        consts = [c.value for c in comparison.comparators
                  if isinstance(c, ast.Constant)
                  and isinstance(c.value, (int, float))]
        if not consts:
            return False
        if any(isinstance(op, (ast.Gt, ast.GtE)) for op in comparison.ops):
            return True
        if any(isinstance(op, (ast.Lt, ast.LtE)) for op in comparison.ops):
            return False
        return (any(isinstance(op, ast.NotEq) for op in comparison.ops)
                or any(consts))

    def _demanded_hosts(self, fn, source):
        """Hosts this test demands a non-empty slate for."""
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare) or not self._demands(node):
                continue
            left = node.left
            if isinstance(left, ast.Call) and self._leaf(left) in self.READS:
                out.append(self._host(left, source))
            elif (isinstance(left, ast.Subscript)
                  and isinstance(left.slice, ast.Constant)
                  and left.slice.value == "count"
                  and isinstance(left.value, ast.Call)
                  and self._leaf(left.value) == self.ACCRUING):
                out.append(self._host(left.value, source))
        return out

    def test_no_fixture_accrues_strikes_behind_its_own_pause(self):
        source = Path(__file__).read_text(encoding="utf-8")
        bad = []
        for fn in (n for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.FunctionDef)
                   and n.name.startswith("test_")):
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
            parks = [(self._host(c, source), c.lineno) for c in calls
                     if self._leaf(c) in self.PARKING]
            heals = [(self._host(c, source), c.lineno) for c in calls
                     if self._leaf(c) == self.ACCRUING]
            if not parks or not heals:
                continue
            demanded = self._demanded_hosts(fn, source)
            for park_host, park_line in parks:
                for heal_host, heal_line in heals:
                    if heal_line <= park_line:
                        continue
                    if not self._same(park_host, heal_host):
                        continue
                    if any(self._same(h, heal_host) for h in demanded):
                        bad.append(
                            f"{fn.name}:{park_line} parks the node, then expects "
                            f"note_heal at line {heal_line} (host {heal_host}) to "
                            f"accrue — a parked node accrues nothing (#568); "
                            f"accrue the slate first, then park")
        assert bad == [], "\n".join(bad)


# ── one resume policy, two callers (card t_110adc1a) ────────────────────────
class TestResumePolicyIsShared:
    """`hscc node resume` and the daemon's expiry tick must run the SAME code.

    The tick resumes what the operator scheduled, so any policy the verb holds
    that the tick lacks (or the other way round) is a node that comes back under
    one caller and stays parked under the other. Both were therefore pointed at
    :func:`node_pause_exec.resume_host`, and these tests are what keep that
    arrangement from quietly decaying back into two copies:

    * the behavioural one drives the verb with the executor's policy replaced, so
      if the verb ever resumes for itself again the fake never fires and the test
      goes red — and it pins the exit-code mapping (``ok`` → rc) in both
      directions, which is the only thing the verb still decides;
    * the source one reads the verb's own body and refuses to let the resume
      primitives come back into it.
    """

    def test_the_verb_calls_the_executors_resume_policy(self, fleet,
                                                        monkeypatch):
        seen = []

        def fake_resume(host, entry, **kw):
            seen.append((host, dict(entry), kw))
            return {"ok": True, "action": npe.RESUME_RESUMED}

        monkeypatch.setattr(npe, "resume_host", fake_resume)
        seed_pause()
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 0, out
        assert len(seen) == 1, seen
        host, entry, kw = seen[0]
        assert host == W1
        assert entry["units"] == ["w1", "wspan"], (
            "the verb hands over the overlay entry it selected, not a re-read")
        assert kw["apply"] is True, (
            "--yes is how the verb says 'do it'; the tick passes apply too")
        assert callable(kw["confirm"]), (
            "the question is handed INTO the shared policy — a second copy of "
            "it in the verb would drift from the tick's no-ask posture")
        assert set(kw) == {"apply", "confirm"}, kw
        assert fleet.calls == [], (
            "the verb no longer restarts anything by itself — resume_host does")

    def test_a_dry_run_ask_is_passed_through_as_apply_false(self, fleet,
                                                           monkeypatch,
                                                           never_asks):
        seen = []
        monkeypatch.setattr(
            npe, "resume_host",
            lambda host, entry, **kw: seen.append(kw) or {
                "ok": True, "action": npe.RESUME_DRY_RUN})
        seed_pause()
        rc, out = run(["resume", W1, "--dry-run"])
        assert rc == 0, out
        assert seen == [{}], seen

    def test_a_kept_pause_exits_nonzero(self, fleet, monkeypatch):
        """The verb's whole remaining decision is ok → exit code, so pin both
        directions: a kept pause (restart failed / refused) must be a red rc, or
        a script that parks and resumes would believe a failed restart."""
        monkeypatch.setattr(
            npe, "resume_host",
            lambda host, entry, **kw: {"ok": False,
                                       "action": npe.RESUME_KEPT})
        seed_pause()
        rc, out = run(["resume", W1, "--yes"])
        assert rc == 1, out

    def test_the_policy_primitives_are_not_in_the_verb(self):
        """Source pin: the verb owns argument grammar, the span-owner answer and
        the exit code — nothing else. If ``resume_units`` / ``heal_breaker.clear``
        / ``node_pauses.clear`` show up in its body again, someone re-inlined the
        policy and the daemon tick is now running a different one."""
        import inspect
        body = ast.parse(inspect.getsource(unified._node_resume))
        banned = {"resume_units", "stop_units", "plan", "clear",
                  "reset_alert", "resume_host"}
        called = {node.func.attr for node in ast.walk(body)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr in banned}
        assert called == {"resume_host"}, (
            f"_node_resume calls {sorted(called)}; only resume_host belongs "
            f"there — the rest is policy and lives in node_pause_exec")


# ── ONE orchestrator guard, both entry points (card t_51a11d16) ────────────
class TestOrchestratorGuardIsShared:
    """`hscc node pause` owns NO orchestrator decision of its own.

    #567 put the refusal inline in the verb. The agent tool inherited it by
    calling the verb — one copy, but a copy that could only see one host, which
    is why it refused a whole uniform pool. The replacement is ONE function in
    ``node_pause_exec`` and the verb prints its verdict; these two tests are the
    teeth behind that "one shared check" requirement. Delete the call and the
    behavioural refusal tests go red; re-inline any part of the decision and the
    source pin goes red.
    """

    def test_the_verb_asks_the_guard_and_prints_only_its_lines(self, pool,
                                                               monkeypatch):
        seen = []
        monkeypatch.setattr(
            npe, "orchestrator_pause_guard",
            lambda host, **kw: seen.append((host, kw)) or {
                "verdict": npe.GUARD_REFUSE, "reason": "pinned", "forced": False,
                "lines": ["REFUSED: pinned by the test"], "others": []})
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert out.endswith("REFUSED: pinned by the test\n"), out
        assert len(seen) == 1, seen
        host, kw = seen[0]
        assert host == POOL_B
        assert kw["force"] is False
        assert kw["plan"]["host"] == POOL_B, (
            "the verb hands the guard the plan it already computed — a guard "
            "that swept the fleet again could disagree with the plan printed "
            "above it, and doubles the sparkrun read")
        assert pool.calls == [] and node_pauses.paused(POOL_B) is None, \
            "a refused pause stops nothing, whoever decided the refusal"

    def test_the_verb_holds_no_orchestrator_policy_of_its_own(self):
        """Source pin: the verb reads the guard's verdict and prints its lines.

        The word ``orchestrator`` may not appear in any string the verb decides
        on, and the guard's own primitives may not be called from it — the moment
        either returns, there are two answers to "may I park this node?" and the
        agent tool is running whichever one the verb forgot to update.
        """
        import inspect
        body = ast.parse(inspect.getsource(unified._node_pause))
        called = {n.func.attr for n in ast.walk(body)
                  if isinstance(n, ast.Call) and isinstance(n.func,
                                                            ast.Attribute)}
        assert "orchestrator_pause_guard" in called, \
            "the verb no longer asks the guard — the guard is now dead code"
        banned = {"orchestrator_flag", "orchestrator_alias_units",
                  "orchestrator_nodes", "paused_nodes"}
        assert not (called & banned), (
            f"_node_pause calls {sorted(called & banned)}; that is policy and "
            "lives in node_pause_exec")
        read = {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}
        assert "serves_orchestrator_model" not in read, \
            "the plan flag is advisory; the guard is the decision"
        doc = ast.get_docstring(body) or ""
        # Comments are not in the AST, so this scans the strings the verb could
        # print or compare against — prose about the delegation is welcome.
        said = [n.value for n in ast.walk(body)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value != doc]
        offending = [s for s in said if "orchestrator" in s.lower()]
        assert not offending, (
            f"the verb strings {offending}; the refusal wording belongs to the "
            "guard so both entry points say the same thing")
