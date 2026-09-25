"""Tests for the rendered sparkrun serve command and its drift comparison.

Two behaviours are pinned here, both of which bit in practice:

1. ``--tp`` is ALWAYS emitted, including ``tp=1``. It used to be emitted only
   when ``tp > 1``, which meant a template could not pin a recipe whose own
   ``tensor_parallel`` default was 2 down to a single node — it silently tried
   to span two.

2. Making it explicit must NOT look like fleet drift. ``_render_serve_cmd`` is
   the shared source of truth for both what gets recorded at provision time and
   what a later apply compares against, so a naive change would make every
   already-running unit compare unequal and get recreated. Comparison is
   therefore normalized: a recorded command with no ``--tp`` is equivalent to a
   freshly rendered one carrying ``--tp 1``.
"""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import (  # noqa: E402
    _render_serve_cmd,
    _diff_serve_cmds,
    serve_cmds_equivalent,
)


def _render(tp=1, gpu_mem=None):
    return _render_serve_cmd("hscc", "10.0.0.2", 8000, "/r/model.yaml",
                             "worker-model", tp, "org/model", gpu_mem)


class TestTpAlwaysExplicit:
    def test_tp1_is_emitted(self):
        cmd = _render(tp=1)
        assert "--tp" in cmd
        assert cmd[cmd.index("--tp") + 1] == "1"

    def test_tp2_is_emitted(self):
        cmd = _render(tp=2)
        assert cmd[cmd.index("--tp") + 1] == "2"


class TestDriftNormalization:
    """A pre-change record (no --tp) must not read as drift against tp=1."""

    def test_legacy_record_without_tp_is_equivalent_to_tp1(self):
        legacy = [c for c in _render(tp=1)]
        i = legacy.index("--tp")
        del legacy[i:i + 2]                      # command as recorded before
        assert serve_cmds_equivalent(legacy, _render(tp=1)) is True
        assert _diff_serve_cmds(legacy, _render(tp=1)) == ""

    def test_legacy_record_without_tp_still_differs_from_tp2(self):
        legacy = [c for c in _render(tp=1)]
        i = legacy.index("--tp")
        del legacy[i:i + 2]
        assert serve_cmds_equivalent(legacy, _render(tp=2)) is False
        assert "--tp" in _diff_serve_cmds(legacy, _render(tp=2))

    def test_a_real_change_is_still_detected(self):
        a = _render_serve_cmd("hscc", "10.0.0.2", 8000, "/r/a.yaml", "m", 1)
        b = _render_serve_cmd("hscc", "10.0.0.2", 9000, "/r/a.yaml", "m", 1)
        assert serve_cmds_equivalent(a, b) is False
        assert "--port" in _diff_serve_cmds(a, b)

    def test_identical_commands_are_equivalent(self):
        assert serve_cmds_equivalent(_render(), _render()) is True


class TestGpuMemPassthrough:
    """--gpu-mem is emitted only when the template asked for it."""

    def test_absent_by_default_so_recipe_default_wins(self):
        assert "--gpu-mem" not in _render(gpu_mem=None)

    def test_emitted_when_set(self):
        cmd = _render(gpu_mem=0.45)
        assert cmd[cmd.index("--gpu-mem") + 1] == "0.45"

    def test_changing_it_reads_as_drift(self):
        assert serve_cmds_equivalent(_render(gpu_mem=0.45),
                                     _render(gpu_mem=0.8)) is False

    def test_adding_it_reads_as_drift(self):
        # A unit that previously ran on the recipe default and is now pinned
        # genuinely needs relaunching — this must NOT be normalized away.
        assert serve_cmds_equivalent(_render(gpu_mem=None),
                                     _render(gpu_mem=0.45)) is False
