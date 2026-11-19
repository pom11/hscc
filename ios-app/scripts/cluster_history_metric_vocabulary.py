#!/usr/bin/env python3
"""Vocabulary leg of cluster_history_chart_check.sh: the Swift metric keys are
the recorder's own column names, and stay that way.

Why this leg exists: `ClusterHistoryMetric` in
ios-app/Sources/Shared/ClusterHistorySeries.swift is a Swift enum whose raw
values are the strings that go into `?metrics=` on GET /v1/cluster/history. The
route validates them against `metrics_history.HOST_KEYS` / `HOST_COLUMNS` /
`ENGINE_KEYS` and answers 400 for any other name, so a key that drifts is a
runtime failure no Swift gate can see. The route card made the same choice
server-side (`_parse_history_metrics` reads the recorder's tuples rather than a
copied list, "so a rename in the recorder shows up here as a rejected name
rather than as a silently missing column"); this is that rule applied to the
client half, across the language boundary.

It also checks the committed fixtures against the same tuples: a fixture whose
host row carries a key the recorder never stores is an invented payload, which
is the failure mode the route's own parity test guards on the server side.

stdlib only, no server, no network. Run from ios-app/ (the gate does that) or
from anywhere — paths resolve from this file.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

IOS_APP = Path(__file__).resolve().parents[1]
REPO_ROOT = IOS_APP.parent
sys.path.insert(0, str(REPO_ROOT))

from hscc_daemon import metrics_history as mh                       # noqa: E402

SWIFT = IOS_APP / "Sources" / "Shared" / "ClusterHistorySeries.swift"
FIXTURES = IOS_APP / "scripts" / "model_decode_check" / "fixtures"

CASE_RE = re.compile(r'^\s*case\s+[A-Za-z0-9_]+\s*=\s*"([a-z0-9_]+)"\s*$')
#: The declaration anchor. It must carry the colon: `enum ClusterHistoryMetric`
#: is a prefix of `enum ClusterHistoryMetricGroup`, and slicing on the shorter
#: string stops at the FIRST enum's closing brace — i.e. it reads zero metric
#: keys and looks like an emptied enum (t_b6512c2e hit this on the first run).
METRIC_ANCHOR = "enum ClusterHistoryMetric:"

#: Keys that identify a row rather than measure it — routes_cluster.py's
#: _HISTORY_HOST_IDENTITY / _HISTORY_ENGINE_IDENTITY.
HOST_IDENTITY = {"tier", "ts", "host", "res", "n", "error"}
ENGINE_IDENTITY = {"tier", "ts", "node", "port", "model", "res", "n", "error"}

#: What the card's metric list promises the widget can chart, so a rename that
#: silently drops one of these is a failure rather than a shorter menu.
REQUIRED = ("cpu_temp_c", "gpu_temp_c", "cpu_temp_c_max", "gpu_temp_c_max",
            "gpu_util_pct", "cpu_usage_pct", "gpu_power_w", "gpu_power_w_max",
            "mem_used_pct", "gpu_mem_used_mb",
            "kv", "gen_tps", "prompt_tps", "run", "wait")

failures: list[str] = []
passed = 0


def ok(label: str) -> None:
    global passed
    passed += 1
    print(f"  ok: {label}")


def bad(label: str) -> None:
    failures.append(label)
    print(f"FAIL: {label}")


def swift_metric_keys() -> list[str]:
    """The raw values of `enum ClusterHistoryMetric`, in declaration order."""
    text = SWIFT.read_text()
    if METRIC_ANCHOR not in text:
        return []
    start = text.index(METRIC_ANCHOR)
    keys: list[str] = []
    for line in text[start:].splitlines():
        if line.startswith("}"):
            break
        match = CASE_RE.match(line)
        if match:
            keys.append(match.group(1))
    return keys


def main() -> int:
    stored_host = set(mh.HOST_COLUMNS)                 # averages + the *_max peaks
    stored_engine = set(mh.ENGINE_KEYS)
    stored = stored_host | stored_engine

    keys = swift_metric_keys()
    if not keys:
        bad("ClusterHistoryMetric declares no wire keys — the enum was emptied "
            "or its `case x = \"key\"` shape changed")
        return report()

    unknown = sorted(set(keys) - stored)
    if unknown:
        bad(f"metric keys the recorder does not store (the route 400s these): "
            f"{', '.join(unknown)}")
    else:
        ok(f"every one of the {len(keys)} metric keys is a real recorder column")

    missing = sorted(set(REQUIRED) - set(keys))
    if missing:
        bad(f"metric keys the card promises that the enum no longer offers: "
            f"{', '.join(missing)}")
    else:
        ok(f"all {len(REQUIRED)} promised metric keys are offered")

    bad_peaks = sorted(k for k in keys
                       if k.endswith("_max") and k[:-4] not in set(mh.MAX_KEYS))
    if bad_peaks:
        bad(f"peak keys for a field the recorder keeps no peak for: {', '.join(bad_peaks)}")
    else:
        ok(f"peaks are exactly the recorder's MAX_KEYS {list(mh.MAX_KEYS)}")

    if "gpu_mem_used_pct" in keys:
        bad("the metric set offers a VRAM percentage, but the recorder stores "
            "gpu_mem_used_mb with no total to divide by — a percent would be "
            "invented, not measured")
    else:
        ok("no VRAM percentage is offered (no gpu_mem_total_mb is stored)")

    duplicates = [k for k in keys if keys.count(k) > 1]
    if duplicates:
        bad(f"duplicate metric keys: {', '.join(sorted(set(duplicates)))}")
    else:
        ok("metric keys are distinct")

    # The fixtures must be recorder rows, not invented shapes.
    host_rows, engine_rows = 0, 0
    for name in ("v1_cluster_history_6h.json", "v1_cluster_history_7d.json",
                 "v1_cluster_history_6h_engine_only.json"):
        path = FIXTURES / name
        if not path.exists():
            bad(f"{name} is missing — regenerate with make_cluster_history_fixture.py")
            continue
        payload = json.loads(path.read_text())
        for row in payload.get("hosts", []):
            host_rows += 1
            extra = set(row) - HOST_IDENTITY - stored_host
            if extra:
                bad(f"{name}: host row carries keys the recorder never stores: "
                    f"{', '.join(sorted(extra))}")
        for row in payload.get("engines", []):
            engine_rows += 1
            extra = set(row) - ENGINE_IDENTITY - stored_engine
            if extra:
                bad(f"{name}: engine row carries keys the recorder never stores: "
                    f"{', '.join(sorted(extra))}")
    if host_rows and engine_rows:
        ok(f"{host_rows} host and {engine_rows} engine fixture rows are all "
           "recorder-shaped (identity keys plus stored columns)")
    elif host_rows or engine_rows:
        bad("the fixtures cover only one series — both host and engine rows "
            "must be pinned")

    # A measurement, never a verdict: what the recorder keeps that no chart can
    # ask for. It grows when the recorder grows, and that is information.
    unplotted = sorted(stored - set(keys))
    print(f"  note: {len(unplotted)} stored columns are not chartable by this "
          f"metric set: {', '.join(unplotted) if unplotted else 'none'}")

    return report()


def report() -> int:
    print("")
    if failures:
        print(f"CLUSTER HISTORY VOCABULARY FAILED — {len(failures)} problem(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"CLUSTER HISTORY VOCABULARY PASSED — {passed}/{passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
