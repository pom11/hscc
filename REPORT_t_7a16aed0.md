# Fix: memory endpoint profile resolution (t_7a16aed0)

## Problem
The `GET /v1/memory?profile=...` endpoint returned empty for every profile on
the live API, even though profiles had real memories on disk (ios-engineer had
7 entries).

Root cause: `_memory_dir` (hscc-api/routes_memory.py) resolved the profile's
memories dir through `routes_profile._hermes_profiles_dir()`, which honors the
`HERMES_HOME` env var verbatim. The live API process (PID 65129,
`hscc api start --tailscale`) runs with
`HERMES_HOME=/Users/desac/.hermes/profiles/hscc-orch` — the orchestrator's own
profile dir leaked into the env. So memory resolved to
`/Users/desac/.hermes/profiles/hscc-orch/profiles/<profile>/memories`, which
does not exist → `_memory_dir` returned None → every profile reported
"no memory store".

By contrast `GET /v1/sessions?profile=...` was POPULATED because sessions
resolves profiles via `hermes_cli.profiles` (routes_orchestrator.py:509-520),
which anchors on the real default home rather than the leaked HERMES_HOME.

## Fix
`_memory_dir` now resolves the profile dir through the same robust resolver
sessions uses — `hermes_cli.profiles` — instead of `_hermes_profiles_dir`:

- `hermes_cli.profiles.profile_exists(canon)` → False ⇒ no store ⇒ None
- `hermes_cli.profiles.get_profile_dir(canon)` → real profile dir (anchored on
  `hermes_constants.get_default_hermes_root()`, which returns `~/.hermes` even
  when HERMES_HOME is a profile dir under it)
- appends `/memories`, returns the dir only if it exists

Only `hscc-api/routes_memory.py` changed (20 insertions, 3 deletions). No
other route touched; `/v1/sessions`, `/v1/profiles` unchanged.

## Verification
1. Subshell with leaked env
   `HERMES_HOME=/Users/desac/.hermes/profiles/hscc-orch`:
   - ios-engineer → /Users/desac/.hermes/profiles/ios-engineer/memories
     (7 cards) ✓
   - backend-engineer → 7 cards ✓
   - hscc-orch → 5 cards ✓
   - nonexistent profile → None ✓
2. `pytest tests/test_routes_memory.py` → 17 passed ✓
3. Diff vs base 81d6de5 touches only routes_memory.py (sessions/profiles
   endpoints untouched by construction) ✓

Commit: `d0e9b41` on branch `wt/t_7a16aed0`.

## Note (out of scope, logged for follow-up)
The `_hermes_profiles_dir()` helper in routes_profile.py still honors the raw
HERMES_HOME, so any OTHER endpoint that reads profile lists through it
(notably a `/v1/profiles`-style directory listing) would also see 0 profiles
under the same leaked env. This task was scoped to the memory endpoint; a
follow-up should audit `_hermes_profiles_dir` callers and switch them to the
same `hermes_cli.profiles` resolver.
