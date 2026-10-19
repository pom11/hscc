"""Capture the PRE-CHANGE full APPLY render of a v3 NON-UNIFORM template.

The C2 overreach guard (§7: "old templates apply BYTE-IDENTICAL") needs a
golden produced by the code BEFORE the apply-side declaration work landed —
C1's golden pins four render helpers; this one pins the whole apply pipeline
(config.yaml, serving.json, models.json, applied_template.json and the step
sequence), which is the surface the C2 write steps could have changed.

Run it ONCE, at the pre-change commit, and commit the JSON it writes:

    git -C ../.. worktree add /tmp/hscc-pre-42924ce8 42924ce8
    python3 /tmp/hscc-pre-42924ce8/hscc-cluster/tests/_capture_apply_golden.py \\
        hscc-cluster/tests/_v3_nonuniform_apply_golden.json

Fakes only: fake topology, fake coster, injected _http_get, every state path
pinned to a tmp dir, sparkrun subprocess and provisioning stubbed. No live
cluster, no live sparkrun, no live ~/.config/sparkrun, no network.
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass

# Resolves against THIS file, so a reviewer can re-run the capture in any
# checkout/worktree — including the pre-change commit, which is the point
# (provenance must be re-derivable, not asserted in a comment).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_template as ct          # noqa: E402
import template_intent as ti           # noqa: E402
import recipe_cost as rc               # noqa: E402

# t_ac795424 doctrine: documented placeholder addresses only.
ORCH = "10.0.0.1"
W = ["10.0.0.2", "10.0.0.3"]


@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


# v3, NON-uniform, routing block + one proxied family + one unproxied family:
# the exact shape §7 says must not change a single byte.
V3_NON_UNIFORM = {
    "name": "v3-nonuniform-apply-guard",
    "version": 3,
    "description": "C2 apply overreach-guard fixture",
    "orchestrator": {"recipe": "orch.yaml"},
    "families": [
        {"name": "coding",
         "models": [{"recipe": "work.yaml"}],
         "workers": 1,
         "proxy": True},
        {"name": "reasoning",
         "models": [{"recipe": "work.yaml"}],
         "workers": "remaining",
         "proxy": False},
    ],
    "routing": {"delegation": "family-coding",
                "compaction": "orchestrator",
                "auxiliaries": "family-reasoning"},
}


def _serving(*ids):
    def get(url, api_key=None):
        return json.dumps({"data": [{"id": m} for m in ids]})
    return get


def _wire_fakes(root, template_dict):
    """Point every state path at `root` and stub every external boundary.

    Shared with the guard test on purpose: the golden and the live comparison
    must run IDENTICAL setup, or a difference could come from the harness
    instead of from the code.
    """
    tdir = os.path.join(root, "templates")
    os.makedirs(tdir, exist_ok=True)
    import yaml
    with open(os.path.join(tdir, template_dict["name"] + ".yaml"), "w") as fh:
        yaml.safe_dump(template_dict, fh, sort_keys=False)

    cluster_json = os.path.join(root, "cluster.json")
    with open(cluster_json, "w") as fh:
        json.dump({"gateway": {"ip": ORCH}, "workers": [{"ip": w} for w in W]},
                  fh)

    hscc = os.path.join(root, "hscc")
    os.makedirs(hscc, exist_ok=True)
    profiles = os.path.join(root, "profiles")
    # Two role profiles: one worker-facing (points at :4000) and one ORCH-role.
    for name, base_url, default in (("worker", "http://localhost:4000/v1",
                                     "worker-model"),
                                    ("reviewer-orch", "http://localhost:4000/v1",
                                     "orchestrator-model")):
        d = os.path.join(profiles, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.yaml"), "w") as fh:
            yaml.safe_dump({"model": {"base_url": base_url,
                                      "default": default}}, fh)

    ct.TEMPLATE_DIR = ct.Path(tdir)
    ct.CLUSTER_JSON = ct.Path(cluster_json)
    ct.HSCC_DIR = ct.Path(hscc)
    ct.SERVING_JSON = ct.Path(hscc) / "serving.json"
    ct.MODELS_JSON = ct.Path(hscc) / "models.json"
    ct.CONFIG_YAML = ct.Path(hscc) / "config.yaml"
    ct.PROFILES_DIR = ct.Path(profiles)
    ct.PROXY_DIR = ct.Path(hscc) / "proxies"
    ct.APPLIED_STATE = ct.Path(hscc) / "applied_template.json"
    ct.ROLLBACK_DIR = ct.Path(hscc) / "rollback"

    ct._discover = lambda probe=False: FakeTopo(FakeNode(ORCH),
                                                [FakeNode(w) for w in W])
    # Every probe resolves the aliases — the post-config-refresh norm, and the
    # only way the probe-before-write gates write deterministically.
    ct._http_get_default = _serving("orchestrator-model", "worker-model")
    ti._rc.recipe_cost = lambda r: rc.RecipeCost(r, per_gpu_total_gb=30,
                                                 fits=True)
    ct.Path.is_file = lambda self: True   # recipes "exist"

    # External boundaries: no sparkrun subprocess, no provisioning. The write
    # path is recorded so the guard can assert it stayed COLD.
    proxy_writes = []

    def _fake_subprocess_run(*a, **k):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    import subprocess
    subprocess.run = _fake_subprocess_run
    ct._provision_models = lambda plan, **k: {"status": "ok",
                                              "provisioned": [],
                                              "note": "capture"}
    # The apply path's gateway restart must not run either.
    import gateway_restart
    gateway_restart.restart_gateway = lambda: {"success": True, "note": "stub"}
    return ct, hscc, proxy_writes


def capture_apply_render(template_dict, root):
    """Run one full apply with fakes and return every artifact it produced."""
    ct, hscc, _ = _wire_fakes(root, template_dict)
    result = ct.apply_template(template_dict["name"], confirm=True,
                               _http_get=_serving("orchestrator-model",
                                                  "worker-model"))

    def read(path):
        p = os.path.join(hscc, path)
        return open(p).read() if os.path.exists(p) else None

    # Volatile fields normalised: a timestamp and a tmp bundle path can never
    # be byte-equal across two runs, and their content is not a routing claim.
    state = read("applied_template.json")
    if state:
        doc = json.loads(state)
        doc.pop("applied_at", None)
        state = json.dumps(doc, indent=2, sort_keys=False)
    steps = [{"step": s.get("step"), "status": s.get("status")}
             for s in result.get("steps", [])]
    return {
        "success": result.get("success"),
        "config_yaml": read("config.yaml"),
        "serving_json": read("serving.json"),
        "models_json": read("models.json"),
        "applied_template_json": state,
        "steps": steps,
        # The step-level detail the guard must also pin: which consumers apply
        # says it wrote. Volatile-free by construction.
        "step_detail": [
            {k: v for k, v in s.items() if k in
             ("step", "keys_written", "changed", "notes", "model_id",
              "profiles_changed", "orch_profiles_changed", "config_changed")}
            for s in result.get("steps", [])],
    }


def main():
    root = tempfile.mkdtemp(prefix="apply-golden-")
    render = capture_apply_render(V3_NON_UNIFORM, root)
    text = json.dumps(render, indent=2, sort_keys=False)
    here = os.path.dirname(os.path.abspath(__file__))
    out = (sys.argv[1] if len(sys.argv) > 1
           else os.path.join(here, "_v3_nonuniform_apply_golden.json"))
    if not os.path.isabs(out):
        out = os.path.abspath(out)
    with open(out, "w") as fh:
        fh.write(text)
    print("wrote", out, len(text), "bytes")


if __name__ == "__main__":
    main()
