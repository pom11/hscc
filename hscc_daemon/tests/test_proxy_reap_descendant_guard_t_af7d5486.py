"""The orphan reaper must not SIGKILL the proxy that is serving :4000 (t_af7d5486).

WHY THIS EXISTS
---------------
``_reap_orphan_proxy_listener`` is the kill path of the F4 proxy repair: it
SIGTERMs, then SIGKILLs, a listener on ``:4000`` that it believes sparkrun does
not track. Its "is this mine?" guard compared two PID numbers
(``tracked_pid is not None and pid == tracked_pid``). This fleet launches the
gateway as ``uv tool uvx --from litellm[proxy]==… litellm …``, so sparkrun's
state file records the **uvx wrapper** while the socket is held by its
**litellm child** — measured on a perfectly healthy proxy: ``state.yaml``
pid 85087 vs ``lsof -iTCP:4000`` 85090, ``PPID(85090) == 85087``. The equality
is therefore *never* true for the live proxy, the very next guard only asks
whether the cmdline contains ``litellm`` (the real listener's does), and the
function reaches ``os.kill(pid, SIGTERM)`` and then ``SIGKILL`` — on the fleet's
only entrypoint, logged as "reaping ORPHAN proxy listener". Latent, not yet
fired: one unverified repair tick away from a self-inflicted outage
(docs/audits/2026-11-22-proxy-connection-error.md §4.4 corollary).

WHAT THIS FILE PINS
-------------------
1. THE TRI-STATE ANCESTRY PREDICATE — ``_pid_is_tracked_or_descendant`` returns
   True (is the tracked PID, or the bounded ``ppid`` walk reaches it), False
   (the walk COMPLETED at a root and never touched it), or None (the walk could
   not be completed). Callers must not have to guess which.
2. THE UVX SHAPE IS NOT AN ORPHAN (red-before-the-fix) — tracked = wrapper,
   listener = its litellm child ⇒ no signal at all, and the log names it as the
   tracked process's descendant instead of calling it an orphan.
3. UNRESOLVABLE ANCESTRY REFUSES TO KILL — a ``ps`` that cannot answer is
   *not evidence of foreignness*. Killing is the irreversible direction, so an
   incomplete walk fails closed. This is the half a "matched / not matched"
   boolean cannot express.
4. THE CONTROLS — a litellm listener whose chain reaches the root IS still
   reaped, and a listener is still reaped when sparkrun tracks nothing (the F4
   orphan shape the function was written for). Without these, 2-3 are satisfied
   by a reaper that never fires.

SAFETY (the no-live rule)
-------------------------
No real ``lsof``, no real ``ps``, no real PID table and no signal that could
reach a process: ``_proxy_listener``, ``_proc_ppid`` and ``os.kill`` are all
stubbed, and the helper under test is driven through an injected ``ppid_fn``.
The PIDs below are invented numbers, not this host's; the config path is a fake
under ``/tmp``.
"""

import signal

from hscc_daemon import health

# One gateway, two processes (the shapes measured live, numbers invented here).
WRAPPER_PID = 85087        # what sparkrun's state file tracks (`uv tool uvx …`)
CHILD_PID = 85090          # what actually holds :4000 (`python …/bin/litellm`)
GRANDCHILD_PID = 85091     # a deeper nesting of the same tree
FOREIGN_PID = 4242         # a litellm that is NOT under the tracked wrapper
LAUNCHD_PID = 1            # a chain that reaches this is provably not ours

LITELLM_CMD = ("python /tmp/fake-env/bin/litellm "
               "--config /tmp/fake-litellm_config.yaml --port 4000")
FOREIGN_CMD = "/usr/bin/some-other-server --port 4000"


# ── the predicate itself ─────────────────────────────────────────────────────

class _Chain:
    """A fake ``ppid_fn`` over an invented process tree, with a lookup ledger.

    ``parents`` maps pid → ppid. A pid that is absent from it has an
    UNREADABLE parent (the ``ps`` failure the tri-state rule exists for), which
    is counted in ``lookups`` so the hop budget can be pinned.
    """

    def __init__(self, parents):
        self.parents = dict(parents)
        self.lookups = []

    def __call__(self, pid):
        self.lookups.append(pid)
        return self.parents.get(pid)


def test_helper_true_for_the_tracked_pid_itself():
    """pid == tracked_pid needs no walk at all — and must not start one."""
    chain = _Chain({})

    def boom(pid):
        raise AssertionError("must not consult ps when the PIDs are equal")

    assert health._pid_is_tracked_or_descendant(
        WRAPPER_PID, WRAPPER_PID, ppid_fn=boom) is True
    assert chain.lookups == []


def test_helper_true_for_the_direct_child_of_the_tracked_pid():
    """THE PRODUCTION SHAPE: the port owner is the tracked wrapper's child."""
    chain = _Chain({CHILD_PID: WRAPPER_PID, WRAPPER_PID: LAUNCHD_PID})
    assert health._pid_is_tracked_or_descendant(
        CHILD_PID, WRAPPER_PID, ppid_fn=chain) is True


def test_helper_true_for_a_deeper_descendant_within_the_budget():
    chain = _Chain({GRANDCHILD_PID: CHILD_PID, CHILD_PID: WRAPPER_PID})
    assert health._pid_is_tracked_or_descendant(
        GRANDCHILD_PID, WRAPPER_PID, max_hops=3, ppid_fn=chain) is True


def test_helper_false_when_the_walk_completes_at_the_root():
    """An unrelated tree is a PROVEN no, not a shrug — the reaper must still
    be able to call it foreign and act."""
    chain = _Chain({FOREIGN_PID: 4243, 4243: 4244, 4244: LAUNCHD_PID})
    assert health._pid_is_tracked_or_descendant(
        FOREIGN_PID, WRAPPER_PID, ppid_fn=chain) is False


def test_helper_none_when_a_parent_cannot_be_read():
    """ps gave no answer ⇒ the walk did not complete ⇒ NOT EVALUABLE."""
    chain = _Chain({})            # FOREIGN_PID's parent is unreadable
    assert health._pid_is_tracked_or_descendant(
        FOREIGN_PID, WRAPPER_PID, ppid_fn=chain) is None


def test_helper_none_when_the_hop_budget_runs_out_first():
    """A chain that reaches neither the tracked PID nor a root is unfinished,
    so the budget is a cap on lookups, not a verdict of foreignness."""
    chain = _Chain({p: p - 1 for p in range(5000, 4990, -1)})   # 5000→4999→…
    assert health._pid_is_tracked_or_descendant(
        5000, WRAPPER_PID, max_hops=3, ppid_fn=chain) is None
    assert len(chain.lookups) <= 3, chain.lookups


def test_helper_none_when_there_is_nothing_to_compare_against():
    assert health._pid_is_tracked_or_descendant(
        CHILD_PID, None, ppid_fn=lambda pid: LAUNCHD_PID) is None
    assert health._pid_is_tracked_or_descendant(
        None, WRAPPER_PID, ppid_fn=lambda pid: LAUNCHD_PID) is None


def test_helper_uses_the_proc_ppid_seam_by_default(monkeypatch):
    """No injected callable ⇒ the walk goes through ``_proc_ppid``, the same
    ``ps`` seam the rest of the module already trusts."""
    monkeypatch.setattr(health, "_proc_ppid",
                        lambda pid: WRAPPER_PID if pid == CHILD_PID else None)
    assert health._pid_is_tracked_or_descendant(CHILD_PID, WRAPPER_PID) is True


# ── the kill path ────────────────────────────────────────────────────────────

class Reaper:
    """Wire ``_reap_orphan_proxy_listener`` to fakes: no lsof, no ps, no signal
    that could reach a real process."""

    def __init__(self, monkeypatch, *, listener, parents=None):
        self.listener = listener
        self.killed = []
        self.logs = []
        monkeypatch.setattr(health, "_proxy_listener", lambda: self.listener)
        monkeypatch.setattr(
            health, "log",
            lambda msg, level="INFO": self.logs.append((str(msg), level)))
        chain = _Chain(parents or {})
        monkeypatch.setattr(health, "_proc_ppid", chain)
        # The SAME tree, also seen DOWNWARD. The guard in this file answers
        # "is this PID ours?" with `_proc_ppid` (an upward walk); the guard it
        # replaced on main answers it with `_proc_children` (a downward BFS).
        # Faking only the upward seam would make the pre-fix leg go red for a
        # reason that has nothing to do with the guard — the BFS would read the
        # conftest default (an empty table), fail to find the proxy's own child,
        # and call a HEALTHY listener foreign. With both directions derived from
        # one `parents` map, a red here is the guard's verdict and nothing else.
        children = {}
        for kid, parent in chain.parents.items():
            children.setdefault(int(parent), []).append(int(kid))
        monkeypatch.setattr(health, "_proc_children",
                            lambda pid: list(children.get(int(pid), [])))

        def fake_kill(pid, sig):
            self.killed.append((pid, sig))
            self.listener = (None, None)     # the process dies on first signal

        monkeypatch.setattr(health.os, "kill", fake_kill)

    def messages(self):
        return [m for m, _ in self.logs]


def test_reap_refuses_the_uvx_child_of_the_tracked_wrapper(monkeypatch):
    """RED BEFORE THE FIX (t_af7d5486).

    The 85087/85090 pair, live and healthy: sparkrun tracks the uvx wrapper,
    its litellm child owns :4000. The old guard asked ``pid == tracked_pid``,
    got False, passed the ``"litellm" in cmdline`` check, and SIGTERM'd the
    proxy that was answering every request — then SIGKILL'd it if it was slow
    to die. The function must now refuse and say WHY in the log: this is the
    tracked process's own descendant, i.e. sparkrun's proxy, not an orphan.
    """
    reaper = Reaper(monkeypatch, listener=(CHILD_PID, LITELLM_CMD),
                    parents={CHILD_PID: WRAPPER_PID, WRAPPER_PID: LAUNCHD_PID})
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is False
    assert reaper.killed == [], f"must not signal our own proxy: {reaper.killed}"
    assert any("descendant" in m for m in reaper.messages()), reaper.messages()
    assert not any("ORPHAN" in m for m in reaper.messages()), \
        "a healthy proxy must never be logged as an orphan it was killed for"


def test_reap_refuses_when_the_ancestry_cannot_be_read(monkeypatch):
    """An unreadable ``ps`` is not evidence of foreignness.

    A boolean "matched / not matched" collapses *unknown* into *not mine* and
    then kills. The guard treats an unfinished walk as unproven and refuses:
    the not-killed state is recoverable on the next tick, a killed gateway is
    not.
    """
    reaper = Reaper(monkeypatch, listener=(CHILD_PID, LITELLM_CMD),
                    parents={})               # no parent readable at all
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is False
    assert reaper.killed == [], f"must not kill what it cannot prove: {reaper.killed}"
    assert not any("ORPHAN" in m for m in reaper.messages()), reaper.messages()


def test_reap_still_reaps_a_genuinely_foreign_litellm_listener(monkeypatch):
    """CONTROL for the two refusals above: a litellm on :4000 whose chain
    completes at the root without the tracked PID is still an orphan and is
    still reaped — the F4 heal that this function exists to perform."""
    reaper = Reaper(monkeypatch, listener=(FOREIGN_PID, LITELLM_CMD),
                    parents={FOREIGN_PID: 4243, 4243: 4244, 4244: LAUNCHD_PID})
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is True
    assert reaper.killed and reaper.killed[0] == (FOREIGN_PID, signal.SIGTERM)


def test_reap_still_reaps_when_sparkrun_tracks_nothing(monkeypatch):
    """CONTROL: with a hollow or absent state file there is no "mine" to
    compare against, so the identity guard stays out of the way and the
    cmdline vetting decides — the shape t_90958eb8 depends on."""
    reaper = Reaper(monkeypatch, listener=(FOREIGN_PID, LITELLM_CMD),
                    parents={FOREIGN_PID: LAUNCHD_PID})
    assert health._reap_orphan_proxy_listener(None) is True
    assert reaper.killed and reaper.killed[0] == (FOREIGN_PID, signal.SIGTERM)


def test_reap_still_refuses_an_unrecognised_cmdline(monkeypatch):
    """BLAST RADIUS (unchanged contract): a foreign listener that is not
    litellm is reported and left alone, however confidently we can place it in
    a PID tree."""
    reaper = Reaper(monkeypatch, listener=(FOREIGN_PID, FOREIGN_CMD),
                    parents={FOREIGN_PID: LAUNCHD_PID})
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is False
    assert reaper.killed == []
    assert any("refusing to kill" in m for m in reaper.messages())


def test_reap_still_reaps_the_orphan_the_repair_itself_creates(monkeypatch):
    """CONTROL for refusal #1, and the reason it is safe to refuse.

    `_proxy_repair_orphan` runs `sparkrun proxy stop` BEFORE reaping, and for
    `uvx litellm` the stop signals the wrapper (sparkrun's hollow tracked PID)
    while the child keeps the socket — the hollow-PID shape
    :func:`_proxy_kill_hollow_tracked` documents. The orphan is then reparented
    to the root, so its chain COMPLETES without the tracked PID: proven foreign
    even though it is the very same process the tick refused to touch a moment
    earlier, while its wrapper was alive. Without this, "do not kill the child"
    would quietly mean "the repair can never free the port".
    """
    reaper = Reaper(monkeypatch, listener=(CHILD_PID, LITELLM_CMD),
                    parents={CHILD_PID: LAUNCHD_PID})   # wrapper gone, reparented
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is True
    assert reaper.killed and reaper.killed[0] == (CHILD_PID, signal.SIGTERM)


def test_reap_still_refuses_the_tracked_pid_itself(monkeypatch):
    """Non-regression on the guard the fix widens: the tracked PID is still
    left to sparkrun, and still described as the tracked PID."""
    reaper = Reaper(monkeypatch, listener=(WRAPPER_PID, LITELLM_CMD),
                    parents={WRAPPER_PID: LAUNCHD_PID})
    assert health._reap_orphan_proxy_listener(WRAPPER_PID) is False
    assert reaper.killed == []
    assert any("tracked PID" in m for m in reaper.messages())
