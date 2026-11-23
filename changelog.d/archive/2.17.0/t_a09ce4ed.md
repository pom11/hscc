kind: Fixed
task: t_a09ce4ed

- **A fleet with no obtainable verify verdict re-ran the whole smoke test on
  every health poll.** `health_cache.get_snapshot` runs an inline verify when
  there is no snapshot anywhere, and when the verify seam cannot produce a
  verdict nothing gets cached — so single-flight deduped *concurrent* polls
  while every *sequential* one started a fresh full verify. It also nested:
  `hscc verify` → `check_api_routes` → the route sweep curls `/v1/health` (its
  probed set is read out of the Swift client) → that cold path → a second
  inline verify inside a verify, the nesting this lane exists to remove,
  surviving exactly while the operator is troubleshooting a broken fleet. The
  cold path now remembers its last failed attempt in-process (a monotonic
  deadline, `_RETRY_AFTER`) and starts no new verify within `TTL_SECONDS` (120
  s) of it: a broken fleet is probed once per window instead of once per poll,
  and a fix is noticed within one window of happening. The stamp is
  deliberately never persisted and never written to
  `~/.hscc/health_snapshot.json` — a failed attempt is not a verdict and must
  not come back next boot looking like one. A backed-off poll is answered with
  the same honest shape the route always used for "no verdict":
  `{"speak": "health check unavailable"}` with no `ok` key. `GET /v1/verify` is
  the explicit trigger and still runs a verify every time, window open or not.
---
kind: Verified
order: 1
task: t_a09ce4ed

- `hscc-api/tests/test_health_cache.py`: 9 new cases, all of them about how
  often the fake verify seam is reached on a fleet that can never produce a
  verdict. Four sequential polls cost ONE verify and every one of them is
  answered `{speak: "health check unavailable"}` with no `ok` key and no
  snapshot file written; the window defers a probe without stopping one, so a
  fleet that recovers is served within one window; eight concurrent failing
  polls share a single verify (they used to queue up eight); the window never
  gates `GET /v1/verify`, never hides a snapshot or its stale revalidation, and
  is ended by storing a verdict or by `reset_state()`. Mutation-checked against
  three edits of `health_cache.py` — drop the window check (5 of these cases
  fail), never arm the stamp (9 fail), never clear it on store (1 fails) —
  against a green 40-case control.
