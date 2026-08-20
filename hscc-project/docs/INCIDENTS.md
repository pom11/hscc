# Incidents

Lessons from real incidents, newest first. Append a lesson with
`flightdeck incident "<what happened>" --fix "..." --apply`.

## 2026-08-12 — "ancestor of main" is not proof work landed
**Project:** flightdeck
**Symptom:** reconcile proposed closing three RUNNING cards; only dry-run-by-default prevented it.
**Cause:** an unstarted worktree branch points at the main tip it forked from, so once main advances it is an ancestor of main — indistinguishable from a merged branch by ancestry alone.
**Fix:** require a merge that actually carried the branch's commits (landed_via_merge) before closing; count own commits to distinguish "in flight" from "never started".
**Lesson:** "ancestor of main" proves the branch forked, not that work landed; verify a merge carried the work before closing any card.

## 2026-08-10 — a bare GET reports a POST-only service as down
**Project:** flightdeck
**Symptom:** a healthy service showed as down; the probe fired a false alarm three separate times (Telegram MCP daemon, then a vLLM endpoint, then again).
**Cause:** the probe sent a GET against an endpoint that only accepts POST — the method the endpoint rejects is not evidence of a dead service.
**Fix:** one shared probe helper that never probes an endpoint with a method it rejects; method-aware checks across every endpoint.
**Lesson:** probe with the method the endpoint accepts, or the failure you measure is your own probe's mistake, not the service's.

## 2026-08-06 — green tests prove nothing about the interpreter the console script runs under
**Project:** flightdeck
**Symptom:** main broke immediately after merge while every branch test passed.
**Cause:** `mcp` 2.0 renamed FastMCP and setuptools 70 rejected a PEP 639 license string — both only bit the installed console script, never the in-tree test import.
**Fix:** run the test suite against the installed entry point, not just the source tree; use the table-form license both old and new setuptools accept.
**Lesson:** tests that import the package from source never exercise the packaging that actually ships it; verify against the installed artifact.

## 2026-07-20 — empty worktree read as starved, healthy cards nearly archived
**Project:** flightdeck
**Symptom:** eight healthy cards were flagged for archive; the only signal was an empty worktree.
**Cause:** empty worktree was conflated with "no work being done" — but an empty STARRED (infrastructure) worktree is healthy, while files-but-no-commit means WORKING, neither of which is dead.
**Fix:** distinguish STARRED (infrastructure, empty is fine) from WORKING (files present, no commit) from the only genuinely dead state; never archive on an empty dir alone.
**Lesson:** an empty worktree is not a stalled card; classify by what "empty" means for that card's kind before ever proposing an archive.

## 2026-07-15 — a 45s timeout read as a wedged inference span
**Project:** flightdeck
**Symptom:** a healthy inference fleet was nearly restarted twice because one request exceeded a 45s client-side timeout and the run looked wedged.
**Cause:** the timeout measures QUEUE DEPTH, not liveness — a busy queue makes the response slow while the service is perfectly healthy.
**Fix:** probe liveness by sampling `vllm:generation_tokens_total` 60s apart: tokens increasing means serving, whatever the queue.
**Lesson:** a slow response is not a dead process; measure liveness with a signal that reflects actual progress, not latency under load.
