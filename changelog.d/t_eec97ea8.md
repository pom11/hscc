kind: Fixed
task: t_eec97ea8

- **Parking the keepalive node switched the fleet's idle teardown off until
  `hscc node resume`.** `_default_keepalive_ok` — the §6.4 interlock that aborts
  an idle teardown when a keepalive unit is unhealthy or unverifiable — had no
  pause seam, so it read a unit the operator had parked with `hscc node pause`
  as a *down* one and `cycle()` logged `interlock-aborted` every tick forever.
  The operator action that is documented as the overlay's first real use ("take
  a single worker GPU out of the fleet for a while — first real use: a training
  run") therefore silently kept the ENTIRE serving layer powered: the GPU memory
  and power autodown exists to free stayed allocated, and the only signal was a
  WARN per tick. Reading (b) decided on the card, and the gate's own §6.4
  rationale is what settles it — "do not tear the whole fleet down **mid-problem
  while a worker is mid-flight relying on it**". A park is neither half of that
  premise: the operator stopped the unit, so nothing is relying on it, and its
  absence is a stated intention rather than a fault. It is the same
  affirmative-operator-statement argument the fleet already runs four times over
  — #565 (a parked node accrues no strike and pages nobody), #566 (the wake
  never ensures a parked unit), #576 (`hscc cluster up` skips one), #569 (health
  projects `paused`, never `down`). Until now the interlock was the one place
  where "the operator parked it" reads as "the cluster is broken", and the place
  with the largest blast radius.
- **Span-aware, and `paused_nodes()` — never `paused()`, never the entry's own
  `node`.** A keepalive unit spans nodes (tensor-parallel across two GPUs) and
  a pause record is keyed by the host it was issued against with the rest of the
  span in `span_nodes`, so a keyed lookup sees the unit's peer as free. The
  exemption is `set(unit["nodes"]) ∩ paused_nodes()`, one overlay read per pass
  (two reads could disagree across a pause the CLI wrote in between), and the
  whole span goes when any member of it does — a TP span with one parked member
  is down as a WHOLE, so probing its head would abort teardown forever, which is
  the same bug one node over. Pin: a pause keyed on a host no unit of the fleet
  lives on, naming the keepalive node only in `span_nodes`.
- **Nothing that is merely unverifiable loosened.** An unparseable
  `serving.json`, an unresolvable unit set, failed TP-peer resolution, an
  unhealthy head, or a probe that raises on a unit the operator did NOT park all
  still return False (abort), byte-for-byte as before the seam; a park on a node
  no keepalive unit spans exempts nothing; a parked unit never exempts a sick
  sibling, so one park cannot become a teardown master switch. An **unreadable
  overlay exempts NOTHING** — the deliberate opposite polarity to the wake
  seam's fail-open, and it is on purpose: for a start, failing open risks one
  bad start; for an interlock, failing open would permit a teardown on an
  unverified signal, so the conservative action here is "no exemption". Each
  direction is a test, and the two seams' ERROR lines now name the risk they
  actually run (`_parked_nodes` took a `context=` label; the wake's wording is
  unchanged).
- **The pre-stop re-verify reads the SAME overlay the timer read, and the stop
  plan still carries the parked unit.** `teardown()` step 1 re-runs the whole
  idle conjunction, so the seam is threaded through `teardown()` →
  `_teardown_locked` → `_is_idle` and not just `cycle()`: threading only
  `cycle()` would let the timer decide and then abort in the pre-stop re-check,
  a worse half-fix than no seam at all. The exemption lives in the interlock and
  nowhere else — a parked unit is still stopped by the `sparkrun stop --all` and
  still appears in the plan's verify set, because refusing a stop would leave it
  RUNNING on a node the operator parked, the mirror image of the bug #566
  closes. That property had only a source-substring pin on the sibling file; it
  now has the behavioural one (a plan built with the keepalive unit filtered out
  reddens a test).
- **The exemption is logged at INFO, not WARN.** A park lasts hours or days and
  this gate runs every daemon tick, so a per-tick WARN would train the operator
  to ignore WARNs; the file write is unconditional, so the exemption is never
  invisible.
---
kind: Verified
task: t_eec97ea8

- New file `hscc_daemon/tests/test_autodown_paused_keepalive.py`, **18 items**,
  driving the real `_default_keepalive_ok` / `_is_idle` / `cycle()` /
  `teardown()` with the overlay read through `node_pauses`' `directory=` seam at
  a temp root, fakes for serving, TP-peer resolution and the health probe, and
  documentation-range hosts only. **Full `hscc_daemon` leg: 4519 passed,
  1 skipped in 176.18s** at `c6805e33` on the runner's interpreter
  (`~/.hermes/hermes-agent/venv/bin/python`, one pytest process per leg,
  `PYTHONDONTWRITEBYTECODE=1`), skip count unchanged at the one pre-existing
  §3.1 wake-claim skip. Targeted trio `test_autodown.py` +
  `test_autodown_paused_wake.py` + the new file: **239 passed** (221 pre-existing
  + 18 new), including `test_only_the_wake_path_consults_the_overlay`, which was
  left intact rather than relaxed. The delta is exactly the new file: the same
  leg on a detached `origin/main` worktree at `abcc9004` measures **4501 passed,
  1 skipped in 180.13s**, so 4501 + 18 = 4519 with the skip count unchanged.
- **Red at base: 18 of 18.** The new file run against a detached `origin/main`
  worktree at `abcc9004` fails every item (`TypeError: unexpected keyword
  argument 'pauses_directory'` / the exemption absent) — the whole file is the
  new seam, so nothing in it could pass for the wrong reason.
- **Mutation probes: 10 of 10 caught**, on the committed tree, on a copy of the
  worktree (provenance gated from inside the run by a `-p` plugin that asserts
  `autodown.__file__` is under the copy and writes a sentinel; pristine control
  green first at 35 passed; pristine bytes restored and re-checked after every
  probe; caches purged per run). M1 exemption disabled → 9 items. M2 exemption
  keyed on the entry's `node` instead of its span → 1
  (`test_a_parked_tp_peer_exempts_the_whole_span`). M3 interlock never reads the
  overlay → 10. M4 overlay read labelled with the wake seam's context → 1
  (`test_an_unreadable_overlay_exempts_nothing_and_says_so`, which also asserts
  the wake's wording does not leak onto this seam's line). M5 `_is_idle` drops
  the seam → 4. M6 `cycle()` drops it → 1. M7 `_teardown_locked` drops it → 2.
  M8 `teardown()` drops it → 2 (M6-M8 are the threading evidence: a defaulted-out
  kwarg is the same bug as no seam). M9 the stop plan quietly filters keepalive
  units → 1. M10 `_parked_nodes` returns keyed hosts only (a `paused()`-shaped
  seam) → 3, two of them the span pins in this file and the sibling wake file.
- `scripts/changelog_fragments.py check` → OK; `scripts/readme_lint.py` →
  `findings: 0` (23 files, docs-index 222/222); `scripts/address_guard.py` →
  clean. Fences clean on every run: **SPARKRUN FENCE** no real exec, **GH PROBE
  FENCE** 0 live calls, hosts all 192.0.2.0/24.
