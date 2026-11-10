"""/cluster-down must claim the stay-down intent and tell the truth per node.

t_3a0bdca9 (D1/D5/D6). This is the SECOND `cluster down` surface (the slash
verb, as opposed to the `hscc cluster down` CLI verb in hscc-cluster/hscc.py).
Before this card it ran `sparkrun stop --all` per node and cleared NOTHING, so
`check_workers`/`check_proxy` resurrected the fleet it had just stopped, and a
node with nothing running was reported as `❌ … stopped — <err>`.

Fakes follow this dir's existing style (test_commands.py): `cmdlib.read_units`
and `cmdlib._run` are monkeypatched on the module the plugin itself uses, and
the marker is reached through the plugin's own fail-soft seam. The autouse
sparkrun fence from conftest.py stays armed — no test here can reach the live
CLI, and no test writes the operator's real ~/.hscc/expected-up.json.
"""

import sys

import cmdlib
import __init__ as plugin


SERVING = {
    "version": 1, "port": 8000,
    "units": [
        {"id": "orch-a3b", "role": "orchestrator", "model": "orch",
         "recipe": "~/r/a3b.yaml", "nodes": ["192.0.2.1"]},
        {"id": "worker-2", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["192.0.2.2"], "keepalive": True},
        {"id": "worker-3", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["192.0.2.3"], "keepalive": True},
    ],
}

# Verbatim shapes the installed sparkrun stop path emits for "nothing to stop"
# (sparkrun/cli/_stop_logs.py:123-125 stderr+exit 1, :217 stdout+exit 0).
NOTHING_RUNNING_STDERR = ("Error: No running workload matches intent "
                          "all on hosts 192.0.2.2")
NOTHING_RUNNING_STDOUT = "No sparkrun containers running."
# The discovery-failure shape must NOT read as "nothing running" (:215 prints
# it next to `Error: could not query <host>`, and that host may still be up).
def query_failed(node):
    return (f"Error: could not query {node}: ssh: "
            "Connection timed out during banner exchange")


class FakePostBoot:
    """Stands in for hscc_daemon.post_boot, recording every clear_expected_up."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def clear_expected_up(self, reason, path=None):
        self.calls.append(reason)
        if self.raises is not None:
            raise self.raises


def _fakes(monkeypatch, per_node, raises=None, marker=True):
    """Install read_units/_run/post_boot fakes. Returns (runs, post_boot).

    ``marker=False`` leaves the plugin's REAL fail-soft import seam untouched —
    for the test that wants to prove the tolerance path itself works.
    """
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    runs = []

    def fake_run(args, timeout=0):
        runs.append(list(args))
        return per_node(args[args.index("--hosts") + 1])

    monkeypatch.setattr(cmdlib, "_run", fake_run)
    pb = FakePostBoot(raises=raises)
    if marker:
        monkeypatch.setattr(plugin, "_post_boot_module", lambda: pb)
    return runs, pb


# ── the marker (D1/D5) ───────────────────────────────────────────────────────

def test_confirmed_down_stamps_the_record_once_with_the_card_reason(monkeypatch):
    def per_node(node):                       # everything really did stop
        return True, "Stopped 1 job(s), 1 container(s) across 1 host(s).", ""

    runs, pb = _fakes(monkeypatch, per_node)
    out = plugin.cmd_cluster_down("confirm")

    assert pb.calls == ["/cluster-down"]      # D5's verbatim reason, ONE stamp
    assert "fleet marked deliberately down" in out
    assert len(runs) == 3                     # orch + 2 workers, one stop each


def test_summary_line_names_the_way_back_up_in_the_same_line(monkeypatch):
    _fakes(monkeypatch, lambda node: (True, "Stopped", ""))
    line = [l for l in plugin.cmd_cluster_down("confirm").splitlines()
            if "fleet marked deliberately down" in l][0]
    # Pinned wording (card body), plus the escape hatch in the SAME line —
    # there is no /cluster-up verb, so saying only "cluster up" would mislead.
    assert ("the daemon will not relaunch workers, the proxy or the notifier "
            "until `hscc cluster up`") in line
    assert "/cluster-up" in line


def test_record_is_stamped_even_when_a_node_stop_failed(monkeypatch):
    """Fail-safe direction: a deliberate down wins over a failed stop.

    Same reading hscc-cluster/hscc.py:530-535 documents for the CLI verb —
    "down, no relaunch" is always the safe state after the operator asked for
    a fleet down, even if one node refused to stop.
    """
    def per_node(node):
        if node == "192.0.2.2":
            return False, "", "Error: failed to stop containers on 192.0.2.2: docker: error"
        return True, "Stopped", ""

    _fakes(monkeypatch, per_node)
    out = plugin.cmd_cluster_down("confirm")

    assert "❌ 192.0.2.2: stopped — Error: failed to stop containers" in out
    assert "fleet marked deliberately down" in out


# ── truthful per-node marks (D6) ─────────────────────────────────────────────

def test_all_nodes_nothing_running_reports_no_failure_anywhere(monkeypatch):
    """The bug's everyday shape: sparkrun exits nonzero when nothing matched.

    Every node used to read `❌ … stopped — Error: No running workload matches
    intent …`, which is both a lie about a failure and a lie about a stop.
    """
    def per_node(node):
        return False, "", NOTHING_RUNNING_STDERR.replace("192.0.2.2", node)

    _fakes(monkeypatch, per_node)
    out = plugin.cmd_cluster_down("confirm")

    assert "❌" not in out
    for node in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
        assert f"○ {node}: nothing running" in out
    assert "fleet marked deliberately down" in out


def test_exit_zero_no_containers_shape_is_also_not_a_stop(monkeypatch):
    """sparkrun's other already-down spelling (`--all`, stdout, exit 0).

    `✅ … stopped` there claims a stop that never happened, so the honest mark
    is ○ on the success path too — not only on the failing one.
    """
    _fakes(monkeypatch, lambda node: (True, NOTHING_RUNNING_STDOUT, ""))
    out = plugin.cmd_cluster_down("confirm")

    assert "❌" not in out and "✅" not in out
    assert "○ 192.0.2.1: nothing running" in out


def test_a_real_error_keeps_the_cross_and_the_error_tail(monkeypatch):
    """Already-down detection must never swallow a genuine failure."""
    def per_node(node):
        if node == "192.0.2.3":
            return False, "", query_failed(node)
        return True, "Stopped", ""

    _fakes(monkeypatch, per_node)
    out = plugin.cmd_cluster_down("confirm")

    assert "❌ 192.0.2.3: stopped — Error: could not query 192.0.2.3: ssh" in out
    assert "○ 192.0.2.3" not in out            # unqueried != nothing running
    assert "✅ 192.0.2.1: stopped" in out


# ── the unconfirmed preview still touches nothing ────────────────────────────

def test_unconfirmed_invocation_issues_no_stop_and_writes_no_marker(monkeypatch):
    runs, pb = _fakes(monkeypatch, lambda node: (True, "Stopped", ""))
    out = plugin.cmd_cluster_down("")          # no `confirm`

    assert runs == []                          # cmdlib._run never called
    assert pb.calls == []                      # marker untouched
    assert "fleet marked deliberately down" not in out
    assert "Confirm" in out and "192.0.2.1" in out


# ── fail-soft: the marker must never break the verb ──────────────────────────

def test_unimportable_post_boot_still_completes_and_warns(monkeypatch):
    """The REAL tolerance path (not a patched seam): poison the import.

    `hscc_daemon` pinned to None in sys.modules makes `from hscc_daemon import
    post_boot` raise on the first attempt AND on the repo-root fallback
    (hscc-cluster/hscc.py:501-519's shape), so _post_boot_module() genuinely
    returns None. The verb must still stop the fleet and say it could not
    claim the stay-down intent.
    """
    runs, _pb = _fakes(monkeypatch, lambda node: (True, "Stopped", ""),
                       marker=False)          # keep the REAL import seam
    monkeypatch.setitem(sys.modules, "hscc_daemon", None)

    out = plugin.cmd_cluster_down("confirm")

    assert plugin._post_boot_module() is None  # the tolerance really is reached
    assert "⚠️" in out and "stay-down marker could NOT be written" in out
    assert "fleet marked deliberately down" not in out   # never claim what failed
    assert len(runs) == 3                      # the stops still happened
    assert "All units stopped." in out


def test_a_raising_clear_expected_up_warns_instead_of_breaking(monkeypatch):
    _fakes(monkeypatch, lambda node: (True, "Stopped", ""),
           raises=OSError("Read-only file system: '/Users'"))
    out = plugin.cmd_cluster_down("confirm")

    assert "⚠️" in out and "Read-only file system" in out
    assert "fleet marked deliberately down" not in out
    assert "✅ 192.0.2.1: stopped" in out
