"""Tests for cmd_cluster_status() workload ``nodes`` (t_db6a32da).

The unit→node edge the iOS "Nodes & serving" screen needs exists ONLY in the
``sparkrun status`` text blob. ``cmd_cluster_status`` already parsed the blob
line by line for name/tp/pp/container_id; it now also keeps the host lines of
the ``Job:`` block in play and exposes them as an optional additive per-
workload key::

    {"name": ..., "tp": ..., "pp": ..., "container_id": ...,
     "nodes": ["192.0.2.11", "192.0.2.12"]}

Contract pinned here:
  * a tp=2 job legitimately reports BOTH nodes, in block order;
  * a solo job reports its one node (the ``solo`` line);
  * the Idle section never leaks into a job's nodes;
  * de-duplicated, order preserved;
  * the existing keys (workload four + engine four) are untouched — the
    change is additive only, old clients keep decoding.
"""

import hscc

# Real sparkrun capture shape (live_captures/20260927_001306), addresses
# replaced with RFC 5737 documentation ranges like the rest of the suite.
_TWO_JOBS_TP2 = """Cluster: hscc (default) — 4 host(s)

Job: /home/devuser/.sparkrun-local/recipes/local-fixed/ds4.yaml  (tp=2, pp=1)  [5c8555695aa50555_4e690f2e56f8]  (2 container(s))
  node_0     192.0.2.244                           Up 39 hours               scitrera/dgx-spark-vllm-jasl-ds4:20260609-hscc
  node_1     192.0.2.246                           Up 39 hours               scitrera/dgx-spark-vllm-jasl-ds4:20260609-hscc
  logs: sparkrun logs 5c8555695aa50555_4e690f2e56f8
  stop: sparkrun stop 5c8555695aa50555_4e690f2e56f8

Job: /home/devuser/.sparkrun-local/recipes/local-fixed/ds4.yaml  (tp=2, pp=1)  [79d915fa1d2c3348_f4de31453fdf]  (2 container(s))
  node_0     192.0.2.247                           Up 32 hours               scitrera/dgx-spark-vllm-jasl-ds4:20260609-hscc
  node_1     192.0.2.248                           Up 32 hours               scitrera/dgx-spark-vllm-jasl-ds4:20260609-hscc
  logs: sparkrun logs 79d915fa1d2c3348_f4de31453fdf
  stop: sparkrun stop 79d915fa1d2c3348_f4de31453fdf

Total: 4 container(s) across 4 host(s)
"""

_SOLO_AND_IDLE = """Job: @local-official/qwen3.6-27b-fp8-vllm  (tp=1, pp=1)  [294ec31919c6]  (1 container(s))
  solo       192.0.2.10                           Up About an hour          sparkrun-eugr-vllm
  logs: sparkrun logs 294ec31919c6
  stop: sparkrun stop 294ec31919c6

Idle hosts (no sparkrun containers):
  192.0.2.11
  192.0.2.12
  192.0.2.13

Total: 1 container(s) across 4 host(s)
"""


def _status(monkeypatch, blob):
    monkeypatch.setattr(hscc, "run_cmd",
                        lambda args, timeout=15, **kw: {"success": True,
                                                        "returncode": 0,
                                                        "output": blob})
    return hscc.cmd_cluster_status()


class TestJobNodes:
    def test_tp2_job_reports_both_nodes(self, monkeypatch):
        """The whole point of t_db6a32da: which machines does this job run on."""
        data = _status(monkeypatch, _TWO_JOBS_TP2)
        assert len(data["workloads"]) == 2
        assert data["workloads"][0]["nodes"] == ["192.0.2.244", "192.0.2.246"]
        assert data["workloads"][1]["nodes"] == ["192.0.2.247", "192.0.2.248"]

    def test_solo_job_reports_its_one_node(self, monkeypatch):
        data = _status(monkeypatch, _SOLO_AND_IDLE)
        assert data["workloads"][0]["nodes"] == ["192.0.2.10"]

    def test_idle_hosts_never_leak_into_a_job_block(self, monkeypatch):
        """Idle lines sit after a blank line + the `Idle hosts` header; the
        block must be closed by then (the pre-fix `idle_hosts` bucket held
        running containers; this pins the other half of that confusion)."""
        data = _status(monkeypatch, _SOLO_AND_IDLE)
        nodes = data["workloads"][0]["nodes"]
        assert "192.0.2.11" not in nodes
        assert "192.0.2.12" not in nodes
        assert "192.0.2.13" not in nodes

    def test_idle_header_closes_block_even_without_blank_line(self, monkeypatch):
        """The `Idle hosts` header itself must end the block: NO blank line
        before it here, and the idle lines use host-line format (label +
        IP), the one shape `_job_host_ip` would happily accept. Without the
        Idle-header reset this IP leaks into the running job (surviving
        mutant, t_db6a32da mutation run)."""
        blob = """Job: r  (tp=1, pp=1)  [cid]  (1 container(s))
  node_0     192.0.2.10                           Up 1 hour                 img
Idle hosts (no sparkrun containers):
  node_0     192.0.2.99                           Up 1 hour                 img
"""
        data = _status(monkeypatch, blob)
        assert data["workloads"][0]["nodes"] == ["192.0.2.10"]
        assert "192.0.2.99" not in data["workloads"][0]["nodes"]

    def test_blank_line_closes_block(self, monkeypatch):
        """A blank line alone (no Idle header, no Total line) ends the block:
        a host-format stray after it must not extend the running job
        (surviving mutant, t_db6a32da mutation run)."""
        blob = """Job: r  (tp=1, pp=1)  [cid]  (1 container(s))
  node_0     192.0.2.10                           Up 1 hour                 img

  node_9     192.0.2.99                           Up 1 hour                 img
"""
        data = _status(monkeypatch, blob)
        assert data["workloads"][0]["nodes"] == ["192.0.2.10"]

    def test_nodes_precedes_total_line_not_after(self, monkeypatch):
        """`Total:` closes the block too — no job may absorb a later stray.
        NO blank line before `Total:` here on purpose: the blank line alone
        would also close the block, which would leave the `Total:` reset
        untested (a surviving mutant, t_db6a32da mutation run)."""
        blob = """Job: r  (tp=1, pp=1)  [cid]  (1 container(s))
  node_0     192.0.2.10                           Up 1 hour                 img
  logs: sparkrun logs cid
Total: 1 container(s) across 1 host(s)
  node_9     192.0.2.99                           Up 1 hour                 img
"""
        data = _status(monkeypatch, blob)
        assert data["workloads"][0]["nodes"] == ["192.0.2.10"]

    def test_nodes_deduplicated_in_order(self, monkeypatch):
        blob = """Job: r  (tp=2, pp=1)  [cid]  (2 container(s))
  node_0     192.0.2.10                           Up 1 hour                 img
  node_0     192.0.2.10                           Up 1 hour                 img
  node_1     192.0.2.11                           Up 1 hour                 img
"""
        data = _status(monkeypatch, blob)
        assert data["workloads"][0]["nodes"] == ["192.0.2.10", "192.0.2.11"]

    def test_job_without_host_lines_gets_empty_nodes(self, monkeypatch):
        """Malformed/partial block: the key must still be present (a list),
        never missing or None — clients may index it unguarded after `in`."""
        data = _status(monkeypatch, "Job: r  (tp=1, pp=1)  [cid]  (1 container(s))\n")
        assert data["workloads"][0]["nodes"] == []


class TestAdditiveContract:
    """Old clients must keep decoding: every pre-existing key stays byte-
    identical in value; `nodes` is the only delta."""

    def test_engine_keys_unchanged(self, monkeypatch):
        data = _status(monkeypatch, _TWO_JOBS_TP2)
        assert set(data) == {"workloads", "idle_hosts", "total_hosts", "raw_output"}
        assert data["total_hosts"] == 4

    def test_workload_legacy_keys_unchanged(self, monkeypatch):
        data = _status(monkeypatch, _TWO_JOBS_TP2)
        for w in data["workloads"]:
            assert set(w) == {"name", "tp", "pp", "container_id", "nodes"}
        w0 = data["workloads"][0]
        assert w0["name"] == "/home/devuser/.sparkrun-local/recipes/local-fixed/ds4.yaml"
        assert (w0["tp"], w0["pp"]) == ("2", "1")
        assert w0["container_id"] == "5c8555695aa50555_4e690f2e56f8"

    def test_legacy_idle_hosts_bucket_untouched(self, monkeypatch):
        """idle_hosts keeps its exact prior semantics (every indented dotted
        line, INCLUDING running containers of Job blocks — the field the card
        says cannot be read as 'free nodes'). This test fails the moment
        someone 'fixes' idle_hosts and breaks old clients."""
        data = _status(monkeypatch, _TWO_JOBS_TP2)
        assert len(data["idle_hosts"]) == 4  # 4 running container lines
        assert "192.0.2.244" in data["idle_hosts"][0]
