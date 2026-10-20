"""THE APPLY-PATH OVERREACH GUARD (spec §7).

C1 pinned the RENDER helpers byte-for-byte. This file pins what C2 actually
risked: the whole APPLY pipeline. `_apply_proxy_declaration`, `_apply_endpoints`
and `_apply_caps_declaration` are new legs inserted into `apply_template`, and a
leg that fires on a template that never declared it would change an untouched v3
template's behavior while every render-level test stayed green.

Provenance is the whole point, so it is mechanically re-derivable rather than
asserted in prose: `_v3_nonuniform_apply_golden.json` was written by
`_capture_apply_golden.py` executed against commit 42924ce8 — the last commit
before ANY declaration implementation landed — through a throwaway worktree:

    git -C <repo> worktree add /tmp/hscc-pre-42924ce8 42924ce8
    cp hscc-cluster/tests/_capture_apply_golden.py /tmp/hscc-pre-42924ce8/hscc-cluster/tests/
    python /tmp/hscc-pre-42924ce8/hscc-cluster/tests/_capture_apply_golden.py \\
        <this dir>/_v3_nonuniform_apply_golden.json

The capture script lives in this repo and resolves its imports from its own
location, so re-running it at 42924ce8 reproduces the golden; a reviewer does not
have to trust this comment. Re-baselining the golden is a visible git diff to a
fixture, not a silent re-run of post-change code.

Fakes: fake topology, fake coster, injected _http_get, every state path pinned to
a tmp dir, subprocess/gateway-restart/provisioning stubbed. No live cluster, no
live sparkrun, no live ~/.config/sparkrun, no network.
"""
import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
# conftest puts the PLUGIN dir on sys.path (for `import clusterlib`); the tests
# dir itself is not on it, so the shared capture module needs its own entry.
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import cluster_template as ct            # noqa: E402

from _capture_apply_golden import (      # noqa: E402
    V3_NON_UNIFORM, capture_apply_render)

_GOLDEN = os.path.join(_HERE, "_v3_nonuniform_apply_golden.json")


@pytest.fixture()
def post_root(tmp_path):
    """A wired-up tmp root; the caller runs apply against it."""
    return str(tmp_path)


class TestApplyPathOverreachGuard:
    def test_full_apply_render_is_byte_identical_to_pre_change(self,
                                                               post_root,
                                                               monkeypatch):
        """Every byte `apply_template` produces for a v3 NON-uniform template —
        config.yaml, serving.json, models.json, applied_template.json, the step
        SEQUENCE and each step's report detail — is unchanged by the
        endpoints/proxy/caps work."""
        with open(_GOLDEN) as fh:
            golden = json.load(fh)
        rendered = capture_apply_render(V3_NON_UNIFORM, post_root, mp=monkeypatch)
        # Serialize before comparing: dict equality hides key REORDERING, and
        # key order is part of what a downstream reader (doctor, verify) sees.
        assert (json.dumps(rendered, indent=2, sort_keys=False)
                == json.dumps(golden, indent=2, sort_keys=False))

    def test_the_new_steps_are_not_reported_for_an_undeclared_template(
            self, post_root, monkeypatch):
        """A step entry is a CLAIM about the apply. The three new legs must not
        appear in the report of a template that declared none of them.

        Named rather than folded into the golden comparison so a regression has
        a diagnostic message: if the legs start reporting unconditionally the
        byte-comparison above fails with a 400-line diff and no hint of which
        leg did it.
        """
        rendered = capture_apply_render(V3_NON_UNIFORM, post_root, mp=monkeypatch)
        for step in ("proxy-declaration", "endpoints", "caps"):
            assert step not in [s["step"] for s in rendered["steps"]], (
                f"{step!r} reported for a v3 template that declared no such "
                f"block — §7 byte-identity broken and the step sequence changed")

    def test_applied_state_records_no_declarations_for_a_v3_template(
            self, post_root, monkeypatch):
        """§7: applied_template.json grows a `declarations` key ONLY when the
        template declared something — an old apply record keeps exactly the keys
        it always had, so doctor's reader never sees an empty block it must
        learn to tolerate."""
        rendered = capture_apply_render(V3_NON_UNIFORM, post_root, mp=monkeypatch)
        doc = json.loads(rendered["applied_template_json"])
        assert "declarations" not in doc
        assert set(doc) == {"template", "orchestrator_node", "families",
                            "units", "uniform_pool"}

    def test_the_proxy_write_path_stays_cold_for_a_v3_apply(self, post_root,
                                                            monkeypatch):
        """The most expensive thing C2 could do to an old template is rewrite
        the operator's proxy.yaml on an apply that never asked for it.

        Pin the CALL SITE, not the helper: `proxy_config_write` passing its own
        unit tests would still miss a leg that calls it unconditionally. The
        seam is `_sparkrun_proxy_write`, the one indirection the leg resolves
        its default writer through (conftest stubs it for hygiene; this patch
        lands after that fixture and wins).
        """
        calls = []

        def _spy(regions):
            calls.append(regions)
            return {"ok": True, "error": None, "mode": "direct",
                    "changed": False}

        monkeypatch.setattr(ct, "_sparkrun_proxy_write", _spy)
        rendered = capture_apply_render(V3_NON_UNIFORM, post_root, mp=monkeypatch)
        assert calls == [], (
            f"a v3 apply touched sparkrun's proxy.yaml write path: {calls}")
        assert rendered["success"] is True


class TestGoldenIsStillPinned:
    """Meta-pins: the guard passes vacuously if the fixture stops carrying the
    shapes that make it meaningful."""

    def test_the_golden_carries_a_real_non_uniform_apply(self):
        with open(_GOLDEN) as fh:
            golden = json.load(fh)
        assert golden["success"] is True
        # A routing block (the v3 consumer-ownership heuristic) and a
        # DIRECT-NODE custom provider — the two things a v4 overreach would most
        # plausibly have rewritten.
        assert "routing" in [s["step"] for s in golden["steps"]]
        assert "http://10.0.0.1:8000/v1" in golden["config_yaml"]
        assert "10.0.0.3:8000" in golden["config_yaml"]
        # The config step must have actually written something, or the golden is
        # a record of an apply that did nothing.
        cfg = [s for s in golden["step_detail"] if s["step"] == "config.yaml"]
        assert cfg and cfg[0]["changed"] is True

    def test_the_step_sequence_is_pinned_exactly(self):
        with open(_GOLDEN) as fh:
            golden = json.load(fh)
        assert [s["step"] for s in golden["steps"]] == [
            "serving.json", "models.json", "config.yaml", "proxies/",
            "worker-model-ids", "routing", "provision", "gateway-restart"]


def test_capture_module_imports_cleanly_from_this_repo():
    """Provenance defence: the capture script must be importable here, or the
    're-derivable' claim in the module docstring is decorative."""
    import _capture_apply_golden as cap
    assert cap.V3_NON_UNIFORM["version"] == 3
    assert callable(cap.capture_apply_render)
