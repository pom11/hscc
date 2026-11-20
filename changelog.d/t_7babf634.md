kind: Fixed
task: t_7babf634

- **`GET /v1/health` ran the whole fleet smoke test on the request thread,
  once per poll.** `handle_health` called `verify.run_all()` — every one of its
  17 checks, real chat round-trips to every serving GPU unit included — and the
  API is a `ThreadingHTTPServer` with no cache, no dedupe and no
  single-flight, so every concurrent client paid for its own verify. It was
  also self-referential: `run_all` includes `check_api_routes`, whose route
  sweep curls both `/v1/health` and `/v1/verify`, so one verify started two
  more inside the API. `/v1/health` now serves the snapshot kept by the new
  `hscc-api/health_cache.py`: TTL 120 s, stale-while-revalidate,
  single-flight (concurrent polls share one verify), persisted atomically to
  `~/.hscc/health_snapshot.json` with an in-process copy so the hot path never
  reads the disk. It runs a verify inline only when no snapshot exists
  anywhere, and it never invents a verdict. The payload stays a strict superset
  for the iOS `HealthResponse` — `ok`, `checks` and `speak` unchanged — plus
  `age_seconds`, `generated_at`, `stale` and `source`; a stale verdict says
  `Health from 4m ago.` rather than implying a check that just ran.
  `GET /v1/verify` keeps its inline semantics as the explicit trigger and now
  records its result into the same snapshot, so a manual verify refreshes the
  health screen and the two routes cannot drift.
---
kind: Verified
order: 1
task: t_7babf634

- `hscc-api/tests/test_health_cache.py`: 31 new cases, every one asserting how
  often the fake verify seam is reached rather than a wall clock — a fresh
  snapshot answers with the seam hit zero times, eight concurrent first polls
  produce exactly one verify, a stale snapshot is returned while its single
  background refresh is still blocked, and a verify that raises stores nothing
  and cannot overwrite the last good verdict. `hscc-api/tests/conftest.py`
  gained an autouse reset of the process-global snapshot, so one test's verdict
  cannot answer a sibling test's `/v1/health`.
