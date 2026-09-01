#!/usr/bin/env python3
"""compare_fixtures_live.py — structural shape check between committed fixtures
and the latest live API captures.

model_decode_check proves the fixtures decode, but a fixture can LIE (it is
hand-written), which silently green-lights a model that does not match the real
wire. This compares the recursive JSON type-signature of each live capture
against its committed fixture, so a field that changed type (or was added /
dropped) in the real API shows up here instead of hiding behind the models.

Signal (not noise): differing VALUES are expected (fixtures are canned
examples). Differing KEYS or VALUE PRIMITIVE TYPES are the real signal — a
fixture that does not reflect the live shape is exactly the false-green this
tool exists to catch.

Usage: scripts/compare_fixtures_live.py [live_captures_dir]
    live_captures_dir defaults to the most recent scripts/live_captures/<ts>/.
Exit code 0 if every matched route's shape agree; 1 otherwise.
"""
import json
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
FIX_DIR = os.path.join(SCRIPTS, "model_decode_check", "fixtures")


def route_map():
    """live capture file -> committed fixture file (names differ in several)."""
    return {
        "v1_ping.json": "v1_ping.json",
        "v1_verify.json": "v1_verify.json",
        "v1_health.json": "v1_health.json",
        "v1_cluster_status.json": "v1_cluster_status.json",
        "v1_cluster_hosts.json": "cluster_hosts.json",
        "v1_cards.json": "cards.json",
        "v1_cards_detail.json": "card_detail_t_049d6986.json",
        "v1_standup.json": "v1_standup.json",
        "v1_review_queue.json": "v1_review_queue.json",
        "v1_qa_queue.json": "v1_qa_queue.json",
        "v1_fleet_stats.json": "fleet_stats.json",
        "v1_fleet_throughput.json": "fleet_throughput.json",
        "v1_fleet_streams.json": "fleet_streams.json",
        "v1_autoscale.json": "v1_autoscale.json",
        "v1_autodown_status.json": "autodown_status.json",
        "v1_projects.json": "v1_projects.json",
        "v1_projects_detail.json": "project_hscc.json",
        "v1_daemon_status.json": "daemon_status.json",
        "v1_triggers.json": "v1_triggers.json",
        "v1_escalate.json": "v1_escalate.json",
        "v1_profiles.json": "v1_profiles.json",
        "v1_kanban_blocked.json": "kanban_blocked.json",
        "v1_kanban_stale.json": "kanban_stale.json",
        "v1_activity_feed.json": "v1_activity_feed.json",
        "v1_project_session_events.json": "v1_session_events.json",
        "v1_template_list.json": "template_list.json",
        "v1_template_status.json": "template_status.json",
        "v1_template_preview.json": "template_preview_hscc-live.json",
        "v1_sessions.json": "v1_sessions.json",
    }


def sig(v):
    """Recursive type-signature: distinguishes structures by their key->type
    mapping and leaves by primitive type, so field presence/type changes appear
    while ordinary value differences (numbers, strings' content) do not."""
    if isinstance(v, dict):
        return {".dict": {k: sig(x) for k, x in v.items()}}
    if isinstance(v, list):
        if not v:
            return {".list": "EMPTY"}
        elems = sorted({json.dumps(sig(x), sort_keys=True) for x in v[:15]})
        return {".list": elems}
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "float" if isinstance(v, float) else "int"
    if isinstance(v, str):
        return "str"
    if v is None:
        return "null"
    return type(v).__name__


def main():
    if len(sys.argv) > 1:
        live_dir = sys.argv[1]
    else:
        matches = sorted(
            d for d in glob(os.path.join(SCRIPTS, "live_captures", "*"))
            if os.path.isdir(d)
        )
        if not matches:
            print("error: no captures found — run scripts/capture_live.sh first")
            return 1
        live_dir = matches[-1]

    prints = []
    diffs = 0
    for live_f, fix_f in route_map().items():
        lf, ff = os.path.join(live_dir, live_f), os.path.join(FIX_DIR, fix_f)
        if not os.path.exists(lf):
            prints.append(f"SKIP {live_f}: no live capture in {live_dir}")
            continue
        if not os.path.exists(ff):
            prints.append(f"SKIP {live_f}: no fixture ({fix_f})")
            continue
        l, f = json.load(open(lf)), json.load(open(ff))
        if sig(l) == sig(f):
            prints.append(f"OK   {live_f}  <-> {fix_f}")
            continue
        diffs += 1
        prints.append(f"DIFF {live_f}  <-> {fix_f}")
        for line in _explain(l, f, "", set()):
            prints.append("     " + line)

    for line in prints:
        print(line)
    print(f"\n{len([p for p in prints if p.startswith('OK')])} shapes match, "
          f"{diffs} differ")
    return 0 if diffs == 0 else 1


def _explain(l, f, path, seen):
    """Yield human-readable lines for structural diffs at `path`."""
    if isinstance(l, dict) and isinstance(f, dict):
        for k in set(l) - set(f):
            yield f"{path}/{k}: ONLY IN LIVE"
        for k in set(f) - set(l):
            yield f"{path}/{k}: ONLY IN FIXTURE"
        for k in sorted(set(l) & set(f)):
            yield from _explain(l[k], f[k], f"{path}/{k}", seen)
    elif isinstance(l, list) and isinstance(f, list) and l and f:
        # compare the first element's signature if both non-empty
        if sig(l[0]) != sig(f[0]):
            yield f"{path}: live[0]={_short(sig(l[0]))} vs fix[0]={_short(sig(f[0]))}"
    elif sig(l) != sig(f):
        yield f"{path}: live={_short(sig(l))} vs fix={_short(sig(f))}"


def _short(s):
    return json.dumps(s, sort_keys=True)[:120]


def glob(pattern):
    import glob as g
    return g.glob(pattern)


if __name__ == "__main__":
    sys.exit(main())
