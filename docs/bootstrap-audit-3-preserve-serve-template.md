# Bootstrap audit 3/3 — preserve_autodown / serving_gen / suggest_template / install_triggers (correctness + silent-failure)

Task: t_2fdff7bd · branch wt/t_2fdff7bd · scope: audit the bootstrap files NOT
covered by cards 1/2: preserve_autodown.py, serving_gen.py, suggest_template.py,
and install_triggers.py (card 1 covered install_payload/install_scripts/
install_soul/apply_patches — install_triggers.py was NOT in that group, so per
the card body it is reviewed here).

## GOAL (from card)
Correctness + silent-failure, specifically:
- swallows an exception and reports success (anti-pattern `except Exception: return None`)
- writes config without saying so
- reverts an operator's choice
- an ENVIRONMENT fault rendered as a fact about DATA

## Findings

### F1 — preserve_autodown: read OSError silently clobbers operator autodown config (ENVIRONMENT fault → data fact + reverts operator choice + writes config without saying so)
Evidence:
- `hscc-bootstrap/preserve_autodown.py:62-75` — the existing-file read catches
  `FileNotFoundError` → seed, and `(json.JSONDecodeError, OSError)` → `exists =
  False` → seed. A read **OSError** (permission denied / transient I/O — an
  ENVIRONMENT fault, not proof the content is bad) is collapsed into the same
  branch as "file does not exist / is corrupt" (a DATA fact).
- Consequences: `exists=False` seeds `DEFAULT_CONFIG` (line 84), then the atomic
  write `os.replace(tmp, autodown_path)` (line 97) **overwrites the operator's
  existing file** with the DISABLED default, and the function reports
  `action="seeded", ok=True` (line 109-115).
- `hscc-bootstrap/bootstrap.sh:200-208` — on a 0-exit script it parses `action`
  and, for `seeded`, prints `ok "autodown: seeded disabled (opt-in)"` — a green
  checkmark over a destroyed operator decision (`enabled: true` → `false`,
  `idle_minutes: 60` → `10`).
- Classification: an ENVIRONMENT fault rendered as a fact about DATA ("file is
  fresh/corrupt"), which simultaneously REVERTS the operator's choice AND writes
  config while reporting it as an innocuous seed. The JSONDecodeError case is a
  deliberate, documented fails-closed design (§8 config corrupt, test
  `test_corrupt_existing_rewritten_disabled_not_enabled`) — but OSError is
  materially different: we have NO evidence the content is bad, so destroying it
  is unjustifiable. Reproduced live (PermissionError on read → on-disk
  `enabled: true` replaced by `enabled: false, idle_minutes: 10`, `ok=True`).

### F2 — install_triggers: read OSError on existing triggers.json silently re-seeds all defaults (ENVIRONMENT fault → data fact)
Evidence:
- `hscc-bootstrap/install_triggers.py:72-73` — `except (json.JSONDecodeError,
  OSError): existing_rules = []` treats an UNREADABLE existing file as empty,
  then merges every default rule and writes them over the file (lines 78-105),
  reporting `ok=True` (line 106).
- The unreadable original IS backed up to `.bak` (line 97-98), so it's
  recoverable — but the behavior is still wrong: a transient read fault rewrites
  the operator's state and re-adds every default we could not compare against,
  and renders an environment fault as the data fact "existing rules are empty".
  (In the simple permission-denied case the failure is masked only because
  `shutil.copy2` re-raises the same OSError into the outer handler — a
  coincidental second fault, not deliberate handling.)
- Classification: an ENVIRONMENT fault rendered as a fact about DATA, writing
  config while reporting success. Same class as F1; fixed for consistency.

## Reviewed and accepted (no change)
- `serving_gen.build_serving` (serving_gen.py:4-50) — pure, raises on no input,
  returns a correct structure. Edge cases exercised: orchestrator-not-in-hosts
  (all hosts become workers), duplicated worker host (distinct `-N` disambiguated
  ids), empty hosts (orch only), IPv6 host (slug keeps colons). None are silent
  failures in the four categories. No I/O, no exception swallow. No change.
- `suggest_template.pick_template` / `suggest` (suggest_template.py:17-42) — pure;
  `int(host_count or 0)` is only ever called with an int (`len(hosts)` /
  `${#HOST_ARR[@]}`), so the non-int ValueError is unreachable in practice. A
  missing template returns None → bootstrap.sh:97 prints no suggestion (fine).
  No silent failure. No change.

## Fixes
- [x] F1 fix: `preserve_autodown.ensure_autodown` separates `OSError` from
  `json.JSONDecodeError` in the existing-file read. A read OSError now returns
  `{"action": "error", "enabled": None, "idle_minutes": None, "ok": False,
  "error": "could not read <path>: ..."}` WITHOUT touching the file —
  bootstrap.sh:210's existing warn branch fires ("autodown install failed —
  ~/.hscc/autodown.json left untouched"). Genuine JSONDecodeError still fails
  closed (rewrite disabled) per design. Regression test
  `test_read_oserror_fails_loud_without_touching_file` in
  `tests/test_preserve_autodown.py` (RED→GREEN verified).
- [x] F2 fix: `install_triggers.install_triggers` separates `OSError` from
  `json.JSONDecodeError`. A read OSError now returns `ok=False` + `error` and
  leaves the file untouched (no re-seed, no `.bak`). Genuine JSONDecodeError
  still fails open (re-add defaults, keep `.bak`) to preserve alert coverage.
  Docstring updated to match. Regression test
  `test_read_oserror_of_existing_fails_loud_without_touching_file` in
  `tests/test_install_triggers.py` (RED→GREEN verified).

## Verification
- [x] hscc-bootstrap suite green (host interpreter): 260 passed in 395.48s (258 baseline + 2 new regression tests)
- [x] hscc-bootstrap suite green (p313 interpreter): 260 passed in 395.85s
- [x] full `scripts/run_tests.sh` suite (host interpreter): ALL GREEN — bootstrap 260, commands 69, roles 114, cluster 422, project 1351, hscc_daemon 1136, sparkrun-hermes 12, api 786 (1 skipped)
- [x] full `scripts/run_tests.sh` suite (p313 interpreter): ALL GREEN — bootstrap 260, commands 69, roles 114, cluster 404+14 skipped, project 1351, hscc_daemon 1136, sparkrun-hermes 12, api 786 (1 skipped)
- [x] merge to main + push + deploy: pending after merging
