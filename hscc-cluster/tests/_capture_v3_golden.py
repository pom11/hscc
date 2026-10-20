"""Capture the PRE-CHANGE render of a v3 NON-UNIFORM template.

The overreach guard for t_401e3045 needs a golden produced by the code BEFORE
the endpoints:/proxy:/caps: work lands. Run this once, pre-change, and commit
the JSON it writes next to the tests.

Fakes only: fake topology, fake coster, injected _http_get, SERVING_JSON pinned
to a tmp dir. No live cluster, no live sparkrun, no network.
"""
import json
import os
import sys
import tempfile

# Resolves against THIS file, so a reviewer can re-run the capture in any
# checkout/worktree — including the pre-change commit, which is exactly the
# point (provenance must be re-derivable, not asserted in a comment).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_template as ct
import template_intent as ti
import recipe_cost as rc
from dataclasses import dataclass

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


def _topo():
    return FakeTopo(FakeNode(ORCH), [FakeNode(ip) for ip in W])


def _coster(recipe):
    return rc.RecipeCost(recipe, per_gpu_total_gb=30, fits=True, tensor_parallel=1)


# v3, NON-uniform, with a routing block + a proxied family: the exact shape
# §7 says must not change a single byte.
V3_NON_UNIFORM = {
    "name": "v3-nonuniform-guard",
    "version": 3,
    "description": "overreach-guard fixture",
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


def main():
    # Never let the capture touch the operator's real ~/.hscc state.
    tmp = tempfile.mkdtemp(prefix="v3golden-")
    ct.SERVING_JSON = os.path.join(tmp, "serving.json")
    ct.MODELS_JSON = os.path.join(tmp, "models.json")
    ct.APPLIED_STATE = os.path.join(tmp, "applied_template.json")

    tpl = ti.ClusterTemplate.from_dict(V3_NON_UNIFORM)
    plan = ti.resolve(tpl, _topo(), _coster=_coster)

    render = {
        "template_to_dict": tpl.to_dict(),
        "serving_json": ti.to_serving_json(plan),
        "hermes_config": ct._update_hermes_config(
            {}, plan, _http_get=_serving("orchestrator-model", "worker-model")),
        "routing_preview": ct._preview_routing(
            tpl, plan, _http_get=_serving("orchestrator-model", "worker-model")),
    }
    # insertions order preserved (byte-identity, not just value identity)
    text = json.dumps(render, indent=2, sort_keys=False)
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tmp, "golden.json")
    with open(out, "w") as fh:
        fh.write(text)
    print("wrote", out)
    print("sha256-ish len:", len(text))
    print(text[:1200])


main()
