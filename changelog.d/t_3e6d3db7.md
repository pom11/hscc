kind: Security
task: t_3e6d3db7

- **The address guard was blind to binary blobs, so a committed `.pyc` could
  carry a real address past all three gates.** `scan_blob()` returned `[]` for
  any NUL-bearing content, and the pre-commit hook, the pytest gate and the CI
  backstop all call that one function — so a stray `git add -f
  scripts/__pycache__/*.pyc`, or a vendored dir committed with an old build,
  shipped a blob whose embedded string constants hold whatever address the source
  had at build time, invisible to every control and unaffected by `.gitignore`
  (this repo had exactly such a blob in published history before the 2026-10-09
  rewrite). Policy now: **a build artefact is refused on its name, whatever it
  contains** — `*.pyc` `*.pyo` `*.pyd` `*.o` `*.a` `*.so` `*.dylib` `*.dll`
  `*.class` `*.jar` `*.egg` `*.whl` `*.pyz` and any `__pycache__/` segment are
  rejected before their bytes are read. Extract-and-scan was considered and
  declined: it has no stopping rule (`.zip`, `.gz`, `.jar`, PDFs are containers
  too), it costs the same on every commit as the refusal does, and if it finds an
  address the fix is still "do not track this file". `ALLOWED_BINARY_PATHS` is the
  escape hatch for a binary that genuinely belongs here — it exempts a path from
  the refusal only, and such a path is then decoded and text-scanned even when it
  carries NULs, so widening it can never hide a leak. `report()` splits the two
  offender classes, because "scrub to `10.0.0.x`" is not the fix for a `.pyc` —
  `git rm --cached` is. Hook and `--tracked` cannot disagree: the verdict is
  formatted in one function. Current exposure was measured at zero (0 tracked
  artefacts; 2 NUL-bearing tracked files, both `.png`), and `--tracked` timing is
  unchanged at 0.12 s. Reasoning + limits:
  `docs/audits/address-guard-binary-policy-t_3e6d3db7.md`.
---
kind: Verified
order: 1

- `scripts/tests/test_address_guard.py`: **10 new cases** — a `.pyc` carrying a
  real LAN address is refused; every artefact shape is refused *even when clean*
  (the verdict is about the path, not the content); case/backslash forms do not
  dodge the list; the tracked `.png` population and an unknown-extension binary
  are **not** broken; the allowlist exempts the refusal but not the scan;
  `scan_paths` refuses a deleted-but-tracked or unreadable artefact; `report()`
  names the artefact and `git rm --cached`; and the CLI's `--staged`/`--tracked`
  exit 1 on it (including after the working tree is scrubbed).
- `hscc_daemon/tests/test_precommit_address_hook.py`: **3 new cases**, including
  the card's agreement test — a real `git commit` through the real hook and the
  real `--tracked` CLI over the same tree emit **identical** verdict strings.
---
