"""One contract, two surfaces: the verb ``escalate`` and the route ``/v1/escalate``.

Audit F6 (docs/audits/2026-11-23-agent-surface.md line 421) recorded the
disagreement: ``POST /v1/escalate`` performs the escalation — it reassigns a
repeatedly-failing card to the strong tier, or alerts a human — while its CLI
twin advertised ``Show pending failure escalations (dry-run, no mutations)``.
Read as a property of the *verb*, that sentence gave an agent that had only ever
read ``hscc help escalate`` a false model of the route (and ``dry-run`` is a
false friend in this CLI, where ``--dry-run`` is a flag on a *mutating* verb —
``cluster up|down``, ``node pause|resume``, ``orch inject``).

The fix was the honest wording; this file is the gate that keeps the two
surfaces honest. It is deliberately ONE test with the two halves side by side:
a per-surface test can pass while the pair disagrees, which is exactly the
failure F6 is. Both halves must hold together:

  CLAIM  — the help printed wherever the verb is documented (``hscc help
           escalate``, ``hscc escalate --help``, the ``hscc --help`` entry) names
           ``POST /v1/escalate`` as the mutating half, names its confirm gate,
           and carries no unqualified "no mutations" claim about the verb;
  BEHAVIOUR — the two read surfaces (the CLI verb, ``GET /v1/escalate``) inject
           substitute no-op reassign/notify and write nothing, while
           ``POST /v1/escalate`` injects nothing at all (so the watcher's real
           ``_default_reassign`` / ``_default_notify`` bind) and is refused
           without ``"confirm": true`` by the SAME gate the other mutating
           POSTs use.

Hermetic by construction (the card's Nolive rule): ``scan_and_escalate`` is
replaced before either surface runs, so no live kanban board is opened, and
HOME / HERMES_HOME are relocated to ``tmp_path`` for the whole test so even a
bypass of the patch could not reach real state. No host, tenant or token is
read; the fake card id is an invented one.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import api_server
import routes_ops

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hscc_daemon import escalate_watcher as ew          # noqa: E402
from hscc_daemon import hscc as hscc_mod                # noqa: E402

TASK = "t_fake_escalate"          # invented id; never a real card


# --------------------------------------------------------------------------- #
# Seam: record what each surface injects into the watcher
# --------------------------------------------------------------------------- #

def _record_injections(monkeypatch):
    """Swap the watcher for a recorder of the acting callables it is handed.

    A surface that passes ``_reassign``/``_notify`` is declaring "do not act";
    one that passes neither lets ``scan_and_escalate`` bind its real
    ``_default_reassign`` / ``_default_notify`` — i.e. it acts. Recording the
    kwargs is what makes the read/mutate split observable instead of asserted.
    """
    calls = []

    def fake(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return []

    monkeypatch.setattr(ew, "scan_and_escalate", fake)
    return calls


def _injections_are_harmless(kwargs, tmp_path, monkeypatch):
    """Calling what a read surface injected must change nothing on disk.

    Identity against the real defaults is checked by the caller; this half is
    the empirical one — the injected pair is called for real and the sandboxed
    home must gain no file.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for env in ("HOME", "HERMES_HOME"):
        monkeypatch.setenv(env, str(home))
    before = sorted(os.listdir(home))
    assert kwargs["_reassign"](TASK, "architect") is None
    assert kwargs["_notify"]("a title", "a body") is None
    assert sorted(os.listdir(home)) == before, (
        "a read surface's injected acting call wrote to HOME — it is not a "
        "no-op, so the 'writes nothing' claim on the verb is false")


def _injected(calls):
    """The kwargs of the single recorded scan (empty when nothing was injected)."""
    assert len(calls) == 1, f"expected exactly one scan, got {len(calls)}"
    return calls[0]["kwargs"]


# --------------------------------------------------------------------------- #
# The one test: claim and behaviour, both surfaces, together
# --------------------------------------------------------------------------- #

def test_escalate_verb_help_and_route_semantics_agree(monkeypatch, capsys,
                                                      tmp_path):
    # ── CLAIM (CLI): the verb's own help must name the mutating half ───────
    # COMMAND_HELP["escalate"] is the single source both `hscc help escalate`
    # and `hscc escalate --help` print (hscc.py:2657 / the DAEMON_COMMANDS
    # help intercept), so this one string is what an agent reads.
    help_text = hscc_mod.COMMAND_HELP["escalate"]
    assert "POST /v1/escalate" in help_text, (
        "the verb's help no longer names the mutating route — F6 returns: the "
        "same word means 'read' on the CLI and 'act' on the API")
    assert "confirm" in help_text, (
        "the confirm gate of the mutating half is not discoverable from the "
        "surface an agent reads")
    assert "no mutations" not in help_text, (
        "an unqualified 'no mutations' about the VERB is back — it is false of "
        "POST /v1/escalate (F6); scope any such claim to this CLI verb")

    # The primary surface too: `hscc --help` is the other place the verb is
    # described, and a one-line entry has to carry the same honesty.
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(sys, "argv", ["hscc", "--help"])
    with pytest.raises(SystemExit) as exc:
        hscc_mod.main()
    assert exc.value.code == 0
    top = [ln for ln in capsys.readouterr().out.splitlines()
           if ln.strip().startswith("escalate")]
    assert top, "the escalate entry disappeared from `hscc --help`"
    assert any("POST /v1/escalate" in ln for ln in top), (
        f"`hscc --help` entry does not name the mutating half: {top}")

    # ── BEHAVIOUR (CLI read): `hscc escalate` must earn "writes nothing" ──
    calls = _record_injections(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hscc", "escalate"])
    with pytest.raises(SystemExit) as exc:
        hscc_mod._handle_escalate()
    assert exc.value.code == 0
    cli = _injected(calls)
    assert callable(cli.get("_reassign")) and callable(cli.get("_notify")), (
        "`hscc escalate` no longer injects substitutes: the CLI verb would "
        "reassign cards and notify a human, i.e. it is the mutating surface "
        "while the help says it writes nothing")
    assert cli["_reassign"] is not ew._default_reassign
    assert cli["_notify"] is not ew._default_notify
    _injections_are_harmless(cli, tmp_path, monkeypatch)

    # ── BEHAVIOUR (API read): GET must hold the same line as the CLI ──────
    calls = _record_injections(monkeypatch)
    status, payload = routes_ops.handle_escalate(None, None, {}, None)
    assert status == 200 and "escalations" in payload
    get = _injected(calls)
    assert callable(get.get("_reassign")) and callable(get.get("_notify")), (
        "GET /v1/escalate no longer injects substitutes — the read twin of the "
        "CLI verb became the mutating one")
    _injections_are_harmless(get, tmp_path, monkeypatch)

    # ── BEHAVIOUR (API mutation): POST is the acting half, and it is gated ─
    # Without confirm: refused before the watcher is touched, by the same
    # gate the other mutating POSTs use (an equal ApiError from the fleet-down
    # handler proves it is one contract, not a private one).
    calls = _record_injections(monkeypatch)
    with pytest.raises(api_server.ApiError) as refused:
        routes_ops.handle_escalate_run(None, None, {}, b"{}")
    assert refused.value.status == 409
    assert refused.value.code == "confirm_required"
    with pytest.raises(api_server.ApiError) as same_gate:
        routes_ops.handle_cluster_down(None, None, {}, b"{}")
    assert (same_gate.value.status, same_gate.value.code) == (
        (refused.value.status, refused.value.code)), (
        "POST /v1/escalate no longer shares the confirm contract of the other "
        "mutating routes")
    assert calls == [], "the gate fired AFTER the watcher ran"

    # With confirm: the acting path — no substitutes injected, so the
    # watcher's real reassign/notify bind. This is the half the CLI help now
    # points at, and it is what makes the verb a mutation verb.
    status, payload = routes_ops.handle_escalate_run(
        None, None, {}, json.dumps({"confirm": True}).encode("utf-8"))
    acted = _injected(calls)
    assert status == 200 and payload["performed"] is True
    assert "_reassign" not in acted and "_notify" not in acted, (
        "POST /v1/escalate injects no-op substitutes: it no longer mutates, so "
        "the help's 'mutating half' wording is now the false half")
