#!/usr/bin/env python3
"""Prove SessionHistoryView's paging algorithm has no gaps/duplicates.

Fidelity note: this exercises the REAL server-side store/history() from
hscc-api/session_event.py, and re-implements the EXACT paging loop the Swift
view does (loadTail -> events = page.events; loadOlder -> filter seq <
events.first.seq, prepend, advance nextBefore = page.next_before). The Swift
source is at ios-app/Sources/HSCC/Views/SessionHistoryView.swift lines 173-219.
"""
import sys, os
sys.path.insert(0, '/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_3359b983/hscc/hscc-api')
import session_event

# Fresh store, seeded with N contiguous events mirroring real append semantics.
session_event.reset_stores()
store = session_event.get_store("hscc")
N = 250          # > pageLimit(100)*2 so we actually page
for i in range(1, N + 1):
    store.append("message", session_event.MessagePayload(role="assistant", delta=f"d{i}"))

PAGE = 100       # == the view's pageLimit (SessionHistoryView.swift:41)

# --- Swift SessionHistoryView.loadTail() equivalent (line 173-188) ---
page = store.history(limit=PAGE)
events = list(page["events"])            # seq-ASCENDING, newest page
nextBefore = page["next_before"]

# --- Swift loadOlder() loop (line 191-219) ---
fetches = 0
while nextBefore is not None:
    cursor = nextBefore
    page = store.history(before=cursor, limit=PAGE)
    newestHeld = events[0]["seq"] if events else None
    older = [e for e in page["events"] if newestHeld is None or e["seq"] < newestHeld]
    if older:
        events = older + events          # insert(contentsOf: older, at: 0)
    nextBefore = page["next_before"]
    fetches += 1
    assert fetches <= 50, "runaway pagination"

seqs = [e["seq"] for e in events]
print(f"seeded {N} events, pageLimit={PAGE}, fetches={fetches}, loaded={len(seqs)}")
print(f"seq range loaded: {seqs[0]} .. {seqs[-1]}")

# Gap / duplicate assertion
assert seqs == list(range(seqs[0], seqs[-1] + 1)), "NON-CONTIGUOUS (gap or dup)!"
assert len(set(seqs)) == len(seqs), "DUPLICATE seq present!"
assert seqs[0] == 1 and seqs[-1] == N, f"did not cover full range: {seqs[0]}..{seqs[-1]}"
assert sorted(seqs) == list(range(1, N + 1))
print("PASS: paging accumulates all events, strictly ascending, no gaps, no duplicates")
