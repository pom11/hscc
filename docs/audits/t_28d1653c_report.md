# RICH CLI — sparkrun-hermes (t_28d1653c) final report

Card: RICH CLI sparkrun-hermes (1 raw site → themed, no-ANSI test)
Assignment: worker
Status: complete (merged + pushed)
Date: 2026-09-25

## Scope of this card

The card asked to convert the single raw `print(`/`json.dumps(` in NON-TEST
sparkrun-hermes code to the themed Rich CLI surface (`_theme` pattern from
hscc-project), with the epic's HARD INVARIANTS (byte-identical machine JSON,
no ANSI on non-TTY, no-ANSI regression test).

## Key finding: there is no human CLI to theme here

sparkrun-hermes is NOT a human CLI. Its plugin.yaml declares `kind: backend`
and `provides_tools: [sparkrun_exec]`. It exposes ONE tool to the agent runtime
via `register(ctx)`; there is no `main()`, no argparse, no terminal surface.

Evidence (verified by grep across every non-test file in the package):

- ZERO `print(` calls in `__init__.py` / `execlib.py` (and across all files,
  including skills).
- The single `json.dumps` is `__init__.py:_stringify`, a decorator wrapper
  that converts the tool's RETURN DICT into a JSON string for the vLLM/OpenAI
  tool layer (its docstring: "vLLM/OpenAI wire format requires role:\"tool\"
  content to be a string; a raw dict makes pydantic reject the request").
- That output is consumed by the agent runtime, NOT rendered to a human or a
  chat. It is the machine wire format itself, not a "render site".

Contrast with the sibling card t_eaef219a (hscc-commands): its single
json.dumps was inside `cmd_template`, whose return string IS the human-visible
chat reply — a genuine render site worth a `_theme.py` + `render_json` (which
kept the JSON byte-identical). sparkrun-hermes has no such site.

## Why NOT a _theme.py

The epic's own HARD INVARIANT states the machine JSON paths must stay
BYTE-IDENTICAL and never be themed ("daemon, scripts, iOS console parse it").
`_stringify` IS that machine path — it serializes the exact bytes the agent
tool layer parses. Routing it through a themed Rich Console (or even through a
`render_json` helper) would add pure abstraction with no functional value:
there is no human reader to benefit, and the result would be identical JSON.
Per "simple over clever" and the no-scope-creep rule, no `_theme.py` was added.

## Deliverable

A no-ANSI + byte-identity regression test that pins the invariants which DO
apply to this package's only non-test output surface (the `_stringify` wire
JSON):

- `sparkrun-hermes/tests/test_theme.py` (4 tests):
  - `_stringify` output is byte-identical to a raw
    `json.dumps(..., ensure_ascii=False, default=str)` re-encoded from the
    same payload.
  - `_stringify` output carries no ANSI escape (`_ESC` named constant, never a
    literal escape byte).
  - `default=str` still serializes non-JSON values, no ANSI.
  - Non-test modules (`__init__.py`, `execlib.py`) contain no `print(` /
    `rich` / `_theme` — there is no terminal surface to introduce regressions
    into (a guard against a future raw `print` being added).

## Verification (both interpreters)

sparkrun-hermes package:
- hermes-agent venv: 12 passed
- p313 conda:        12 passed

Full 8-package suite (scripts/run_tests.sh), run serially under each
interpreter:

- hermes-agent venv: 786 passed, 1 skipped — ALL GREEN (8/8 dirs ✓)
- p313 conda:        786 passed, 1 skipped — ALL GREEN (8/8 dirs ✓)

## Git / merge / push facts

- Worktree branch: wt/t_28d1653c (WORKTREE kind, never scratch).
- Based on main (16a653c) at creation.
- Feature commit: f658399 (test(sparkrun-hermes): pin no-ANSI + byte-identity
  for tool wire format). 1 commit ahead of origin/main pre-merge.
- Rebased onto origin/main (b3df3ac) after a sibling card (t_eaef219a,
  hscc-commands) merged concurrently — clean, no conflicts (different dirs).
- Merge: `git merge --no-ff wt/t_28d1653c` → merge commit 686603c on main.
- Push: `git push origin main` → b3df3ac..686603c. origin/main == main @
  686603c.
- Diff is additive only: sparkrun-hermes/tests/test_theme.py (87 lines).
  No runtime code changed, no addresses, no secrets (verified by grepping the
  commit diff).

## Deploy: NOT required (report as no-op, by design)

`install_payload.py` `_EXCLUDE = {"__pycache__", ".pytest_cache", "tests",
".git"}` — tests are deliberately NOT deployed to the runtime copy. This
card's change is a TEST ONLY; the deployed runtime (`__init__.py`,
`execlib.py`) is byte-for-byte unchanged. So the deployed sparkrun-hermes
runtime is already correct and needs no redeploy. Re-running install_payload
would be a no-op for this card (it copies runtime, excludes tests).

## Exact commands

    # package suite (per interpreter)
    ~/.hermes/hermes-agent/venv/bin/python -m pytest -q sparkrun-hermes/tests -p no:cacheprovider
    /Users/desac/miniconda3/envs/p313/bin/python -m pytest -q sparkrun-hermes/tests -p no:cacheprovider
    # full suite (per interpreter, serial to avoid CPU contention)
    HSCC_TEST_PY=~/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh
    HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python bash scripts/run_tests.sh
    # commit / merge / push
    git commit -m "test(sparkrun-hermes): ..."
    git rebase origin/main
    git merge --no-ff wt/t_28d1653c -m "merge(t_28d1653c): ..."
    git push origin main
