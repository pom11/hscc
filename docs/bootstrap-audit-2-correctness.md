# Bootstrap audit 2/3 — detect / doctor / enable_plugins / ensure_review_feature (correctness + silent-failure)

Task: t_ef341cfe · branch wt/t_ef341cfe · scope: audit ONLY these 4 files.

## GOAL (from card)
Correctness + silent-failure, specifically:
- swallows an exception and reports success (anti-pattern `except Exception: return None` — a failure to look is never a data fact)
- writes config without saying so
- reverts an operator's choice
- an ENVIRONMENT fault rendered as a fact about DATA

## Findings

### F1 — ensure_review_feature: unreadable marker file claimed `already_present` (ENV fault → data fact)
Evidence:
- `hscc-bootstrap/ensure_review_feature.py:50-55` (pre-fix) — `_feature_present` swallows `OSError` (missing/renamed/permission-denied `tools/kanban_tools.py`) and returns `True` ("treat as present so we never touch an unexpected tree").
- `ensure_review_feature.py:79` (pre-fix) — that `True` short-circuits to `{"status": "already_present", "ok": True}`.
- `hscc-bootstrap/bootstrap.sh:261-264` — `already_present` is treated as `ok "kanban review feature: already_present"`.
- Impact: if Hermes ever renames/moves `tools/kanban_tools.py`, or the file is unreadable, a fresh install never installs the review feature but bootstrap celebrates `already_present` — an environment fault (we never even looked) rendered as a data fact (the feature is present). Classic "a failure to look is never a data fact".
Fix: `_feature_present` → tri-state `_feature_state` returning `present|absent|unreadable`; `unreadable` → new status `{"status": "unreadable", "ok": False, detail}` so bootstrap warns instead of claiming success.

### F2 — enable_plugins._ensure_compaction reverts an operator's HIGH threshold_tokens (reverts operator's choice)
Evidence:
- `hscc-bootstrap/enable_plugins.py:388-391` (pre-fix) — `operator_set = (... and cur_tok <= cap and cur_tok not in legacy)`. The `cur_tok <= cap` clause means a deliberate HIGH threshold_tokens (e.g. 500000 when cap is 200000 — "compact even more rarely") is treated as NOT operator-set and overwritten down to `cap`.
- `enable_plugins.py:392-394` (pre-fix) — `if not operator_set and cur_tok != cap: comp["threshold_tokens"] = cap`.
- Reproduced: a config with `compression.threshold_tokens: 500000` is silently rewritten to `200000` on `enable()`.
- Impact: actor's explicit "compact even less frequently" tuning is silently reverted to the cap, forcing more-frequent compaction — a reversion, and also against the module's own "compact rarely (high threshold)" mission.
Fix: drop the `cur_tok <= cap` clause so only known-stale legacy caps (`legacy` tuple) and missing/non-numeric values are raised to the cap; any other operator numeric value is preserved.

### F3 — enable_plugins.enable wires hook commands even when the hook script wasn't installed (writes config without saying so / swallows a signal)
Evidence:
- `hscc-bootstrap/enable_plugins.py:860` (pre-fix) — `hooks_file_result = _ensure_hooks_file(hooks_source)` captured but NEVER used; a failed install (`installed: False`) was silently discarded.
- `enable_plugins.py:852` (pre-fix) `_ensure_hooks(cfg)` wired `pre/post/on_session_start` commands pointing at `~/.hermes/hooks/cluster-guard.py` unconditionally, regardless of whether the script landed.
- `hscc-bootstrap/bootstrap.sh:250-251` — `ok "config wired ($WIRED)"`; `WIRED` includes the hooks-key list, so a failed script install still reports `ok`.
- Impact: if the source `hooks/cluster-guard.py` is missing (partial checkout/deploy), `enable()` writes hook commands referencing a nonexistent script — every hook invocation silently fails at runtime, and nothing reports it.
Fix: install the hook script FIRST; only wire config hook commands when `installed` is True; surface the install status on a new `hooks_file` return key; warn to stderr when it fails. Config is never written a dangling reference.

### F4 — enable_plugins._probe_compaction_endpoint probes a doubled /v1 URL (correctness)
Evidence:
- `hscc-bootstrap/enable_plugins.py:689` (pre-fix) — `models_url = COMPACT_URL.rstrip("/") + "/v1/models"`. But `COMPACT_URL` default (line 104) already ends in `/v1` (`http://{ORCH_MODEL_HOST}:8000/v1`), so the probe URL became `.../v1/v1/models`.
- Impact: the compaction model probe always hit the WRONG path. A correctly-served model produced a spurious 404-probe warning on real installs, and the probe could never actually verify the compaction model was present — so its entire purpose (alert the operator that compression is silently dead) was defeated by a wrong URL.
Fix: factor out `_compact_models_url()` (mirrors `doctor._models_url`: preserve exactly one `/v1`) and probe that.

## Reviewed and accepted (no change)

- `detect.detect_cluster` (`detect.py:29-40`) collapses sparkrun-missing/subprocess-error/non-zero-exit and parse failure into `None`. In the bootstrap flow this is NOT silent: `bootstrap.sh:46-47` renders any `None` as `die "No sparkrun cluster configured"` (a hard stop), and `doctor.py` runs first and FATAL-gates missing/broken sparkrun and an unconfigured cluster, so the operator never reaches the collapse with a broken sparkrun. The `dict|None` return contract is documented and consumed by bootstrap JSON + `serving_gen.build_serving` (a `None` there is caught by the `|| warn "serving.json generation failed"` at bootstrap.sh:230). Changing the return shape would ripple across all callers for no reachable gain in the bootstrap flow. A richer failure REASON would help standalone debugging but is out of scope for this card's silent-failure goal (this is a loud hard-stop, not a silent lie). No change.
- `doctor._check_models_served` (`doctor.py:267-269`) returns `ok=True, detail="config unreadable (...); skipped"` when the config can't be read. This is an env fault surfaced in a passing, non-fatal check — but the detail names the fault explicitly, so it is not silent; and hard-failing the whole preflight because one advisory model check couldn't open config would be wrong. The reason is reported. No change.
- `doctor._check_models_served` probe-error vs unreachable distinction (`doctor.py:310-328`) — correctly separates a reachable-but-wrong-path (loud problem) from an unreachable endpoint (skip, no false alarm). Correct.
- `enable_plugins._probe_compaction_endpoint` suppresses its warning under pytest/unittest via `sys.modules` (`enable_plugins.py:719-722`). A test-detection smell in production code, but its return value is advisory-only (ignored by `enable()`) and it never writes config or asserts a data fact — leaving it avoids noisy test output. Noted; no change.

## Fixes
- [x] F1: `_feature_state` tri-state + `unreadable` status. Regression tests: `tests/test_ensure_review_feature.py::test_unreadable_marker_is_not_claimed_present`.
- [x] F2: drop `cur_tok <= cap` from the operator_set guard. Regression tests: `tests/test_enable_plugins.py::test_compaction_threshold_tokens_high_preserved` + `..._legacy_low_still_raised`.
- [x] F3: gate `_ensure_hooks` on successful script install; add `hooks_file` to `enable()` return; guard `run_doctor_fix` iteration. Regression tests: `tests/test_hooks.py::test_hooks_not_wired_when_script_uninstallable` + `..._wired_only_after_script_installed`.
- [x] F4: `_compact_models_url` preserves exactly one `/v1`. Regression test: `tests/test_enable_plugins.py::test_compact_models_url_preserves_v1_no_duplication`.

## Verification

- [ ] hscc-bootstrap suite green (host interpreter)
- [ ] hscc-bootstrap suite green (p313 interpreter)
- [ ] full `scripts/run_tests.sh` suite (host interpreter default)
- [ ] full `scripts/run_tests.sh` suite (p313 via HSCC_TEST_PY)
- [ ] merge to main + push
- [ ] deploy (install_payload.py)
