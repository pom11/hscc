#!/usr/bin/env python3
"""Edge case: ring-buffer eviction (oldest_seq > 1). Prove paging still
converges with no gaps/dups even when frames fell off retention.
"""
import sys
sys.path.insert(0, '/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_3359b983/hscc/hscc-api')
import session_event

session_event.reset_stores()
store = session_event.SessionEventStore(capacity=10)   # small ring
for i in range(1, 26):
    store.append("message", session_event.MessagePayload(role="assistant", delta=f"d{i}"))
assert store.oldest_seq == 16, store.oldest_seq   # evicted 1..15

PAGE = 100
page = store.history(limit=PAGE)
events = list(page["events"])
nextBefore = page["next_before"]
assert nextBefore is None  # all retained fit in one page here

seqs = [e["seq"] for e in events]
print(f"eviction case: oldest_seq={store.oldest_seq}, loaded {seqs[0]}..{seqs[-1]}, next_before={nextBefore}")
assert seqs == list(range(seqs[0], seqs[-1]+1)) and len(set(seqs)) == len(seqs)
print("PASS: evicted ring pages correctly to retained window; seq contiguous, no dup")
