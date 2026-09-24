# t_baef4e5f — REPORT: RICH CLI hscc-roles (17 raw sites -> themed, no-ANSI tests)

**Task:** PART 1 RICH CLI — hscc-roles (17 raw print/json.dumps, 0 themed).
Convert every raw `print(`/`json.dumps(` in NON-TEST hscc-roles python code to
the themed Rich CLI surface (`_theme` pattern from hscc-project).
**Worker:** worker
**Branch:** `wt/t_baef4e5f`
**Base:** `main`

## Status

DONE — implementation + tests committed, full suite green both interpreters,
merged to main + pushed + deployed.

## How ran

Same harness as the prior Rich epic cards:

```
export HOME=/Users/desac
HSCC_TEST_PY=<interp> bash scripts/run_tests.sh
```

| Interpreter | Path | Result |
|---|---|---|
| A (hermes venv) | `~/.hermes/hermes-agent/venv/bin/python` | **786 passed, 1 skipped — ALL GREEN** |
| B (conda p313) | `/Users/desac/miniconda3/envs/p313/bin/python` | **786 passed, 1 skipped — ALL GREEN** |

hscc-roles package alone: **114 passed** under BOTH interpreters (baseline 101
on main + 13 new no-ANSI / byte-identity regression tests added by this card).

Pre-merge verification on the merged tree (branch inherited the already-green
sibling hscc-cluster card t_e67544b1 via a clean ort merge) is the 786-figure
above. All 8 packages green.

## Merge / push / deploy status

- **Branch:** `wt/t_baef4e5f`
- **Commits ahead of main (mine, pre-merge):** 5 vs origin/main (2 source:
  `cb5e100` feat roles _theme + `af3f498` report skeleton; 2 merge commits
  bringing both generations of the sibling hscc-cluster card t_e67544b1 in;
  1 report `56c4f8d`).
- **Merge:** clean fast-forward `0c8c8b9..a04b5a3` into `main` (disjoint from
  the sibling hscc-cluster work). **YES.**
- **Push:** `git push origin main` → `0c8c8b9..a04b5a3 main -> main` on
  github.com/pom11/hscc. **YES.**
- **Deploy:** `python3 hscc-bootstrap/install_payload.py` from the primary
  checkout `/Users/desac/dev/hscc` → all payload entries installed with
  backups, `missing: []` (hscc-roles included). Verified in the deployed
  runtime `~/.hermes/plugins/hscc-roles/`: `_theme.py` present, 13
  `make_console` usages, only the 3 byte-identical `json.dumps` paths remain
  raw, `hscc.py list` renders the themed table with 0 ANSI bytes when piped.
  **YES (deployed).**
- **Suite on merged main:** `786 passed, 1 skipped` under BOTH interpreters.

## What changed (source)

- **`hscc-roles/_theme.py`** (NEW) — the hscc-roles handle on the shared palette.
  Mirrors `hscc-project/flightdeck/commands/_theme.py` exactly: deferred
  guarded `from hscc_daemon import cli_theme`, plain-Console fallback
  (`_FALLBACK_THEME` registering the semantic names as neutral styles so
  markup never raises MissingStyle), and `make_console` with the
  **width=None-if-TTY else 200** anti-collapse pattern. Exposes `escape`,
  `make_console`, `panel`, `status_panel`, `table`.
- **`hscc-roles/hscc.py`** — converted all 14 human/stderr raw sites to the
  themed Rich surface:
  - `create` success -> `panel("hscc-roles create", ...)`; missing-args usage
    -> `_usage()` error on stderr.
  - `list` -> themed `table` (role / profile columns).
  - `validate` -> `status_panel` (ok green / error red listing bad specs).
  - `autonomy` -> `panel("autonomy", "fleet autonomy is <state>")`.
  - `orch` failure -> `status_panel(e, "error", title="orch")`.
  - `orch-all` warnings (missing/unreadable registry) -> warn lines on stderr.
  - `main` help -> themed usage panel; unknown command -> error on stderr +
    usage panel.
  - All user/error strings routed through `escape()` so literal `[x]` brackets
    render literally (Rich markup is otherwise swallowed).
- The 3 machine `--json` paths (`generate` L59, `orch` L146, `orch-all` L220)
  stay **raw `print(json.dumps(..., indent=2, ensure_ascii=False))`** — their
  lines are byte-identical to main (verified via git diff: unchanged context
  lines). NEVER routed through a Console.

## No-ANSI regression tests (new)

`hscc-roles/tests/test_hscc_theme.py` — 13 tests using a named `_ESC` constant
(`assert the constant, never a literal`) and the flightdeck `_no_ansi` pattern
(redirect to non-TTY StringIO, assert no `\x1b`), plus byte-identity:

| Test | Covers |
|---|---|
| `test_generate_json_byte_identical` | generate `--json` byte-identical |
| `test_orch_json_byte_identical` | orch `--json` byte-identical |
| `test_orch_all_json_byte_identical` | orch-all `--json` byte-identical (per-project values survive) |
| `test_create_human_view_is_plain` | create themed view plain, no `\x1b` |
| `test_create_usage_error_is_plain` | create missing-args stderr plain |
| `test_list_human_view_is_plain` | list table plain |
| `test_validate_ok_is_plain` | validate ok plain |
| `test_validate_error_is_plain` | validate error plain |
| `test_autonomy_off_is_plain` | autonomy off plain |
| `test_autonomy_on_is_plain` | autonomy on plain |
| `test_orch_error_is_plain` | orch error plain |
| `test_unknown_command_is_plain` | unknown-command stderr plain |
| `test_cli_subprocess_list_is_plain` | end-to-end subprocess plain, no `\x1b` |

Byte-identity is proven by re-encoding the exact same payload from the same
inputs with `json.dumps(..., indent=2, ensure_ascii=False) + "\n"` and asserting
equality of stdout — not merely "no ANSI".

## Byte-identity proof

- git diff shows the 3 `print(json.dumps(...))` lines are context (unchanged).
- `orch-all --registry <missing>`: stderr carries the warn (plain), stdout is
  still valid parseable JSON (`json.load` succeeds).

## Files touched (worktree only)

- `hscc-roles/_theme.py` (ADDED)
- `hscc-roles/hscc.py` (MODIFIED)
- `hscc-roles/tests/test_hscc_theme.py` (ADDED)
- `docs/audits/t_baef4e5f_report.md` (this report)

No working notes at repo root; no LAN addresses / secrets committed (scrubbed);
no AI attribution.
