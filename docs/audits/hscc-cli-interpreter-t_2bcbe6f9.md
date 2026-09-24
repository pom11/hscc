# hscc CLI interpreter bug — durable fix (card t_2bcbe6f9)

## Root cause (verified)

`hscc-cli/pyproject.toml` declares `hscc = "hscc_cli:main"`, so pip/uv
regenerates the console script with the shebang of whatever interpreter it is
installed INTO. The CLI was being installed into a bare conda p313
(`/Users/desac/miniconda3/envs/p313`), so `$(which hscc)` carried `#!…/p313/bin/python3.13`.

That is wrong on two independent axes, both confirmed by import probes:

| module | Hermes venv | conda p313 |
|--------|-------------|------------|
| `hscc_cli` | site-packages ✓ | source dir (cwd) ✓ |
| `hermes_cli` | `~/.hermes/hermes-agent/hermes_cli/` ✓ | **ModuleNotFoundError** |
| `hermes_state` | `~/.hermes/hermes-agent/hermes_state.py` ✓ | **ModuleNotFoundError** |

(Verified from a neutral cwd with a throwaway probe script.)

1. Every verb that reads session state — `project chat`, `project sessions`,
   `ensure_session` — imports `hermes_cli` / `hermes_state`, which live ONLY in
   the Hermes venv. Under p313 they raise `HermesRuntimeUnavailable`.
2. p313 lacks the macOS Local Network (TCC) grant the Hermes venv carries, so
   `hscc check` returned `Result: FAIL` (errno 65) against a healthy fleet.

The previous "fix" was a one-off hand-edit of the shebang: any `pip install
hscc-cli` or bootstrap re-run regenerates the script — with a p313 shebang —
silently undoing it. Not durable.

## Decision: install hscc-cli INTO the Hermes venv (Option A)

Of the three options in the card:

- **A. install into Hermes venv as part of bootstrap** — pip/uv bakes the venv
  interpreter into the shebang BY CONSTRUCTION. Survives every reinstall.
  Chosen: it is the only option that makes the correct shebang the DEFAULT
  output of the package manager, not a post-hoc patch. It also fixes the TCC
  grant axis, not just the import axis.
- **B. keep p313 install, rewrite shebang in bootstrap** — still a post-hoc
  rewrite of the exact kind the card forbids as "silent config rewrite"; and
  p313 fundamentally cannot import `hermes_cli`, so the CLI is broken under
  p313 no matter what the shebang says at `project chat` time. Rejected.
- **C. drop wrapper for a generated launcher hardcoding the venv** — more
  moving parts, and pip's own console-script generation (already correct with
  the right interpreter) is simpler and less surprising. Rejected.

### Installer bug found and fixed during verification

`venv/bin/python` is a symlink into the uv-managed store
(`~/.local/share/uv/python/cpython-3.11-…/bin/python3.11`). The first cut of
`install_cli.py` did `Path(venv_python).expanduser().resolve()`, which followed
the symlink to the BASE interpreter. `uv pip install --python <that>` then
failed with:

    The interpreter at <uv base> is externally managed … should not be modified.

Because `--python` must name the venv (it detects it via `pyvenv.cfg`), not the
resolved base. Fix: keep the literal venv path (expanduser only, no resolve); uv
detects the venv from its `pyvenv.cfg` and the console script lands in
`<venv>/bin/hscc` with the `#!<venv>/bin/python` shebang.

## What changed

- `hscc-bootstrap/install_cli.py` (NEW): idempotent installer. Plain install →
  verify `bin/hscc` shebang → force-reinstall (`--reinstall`) if drifted. Reports
  a JSON status `{action, entry, shebang, expected, decision, error}` so
  bootstrap can SAY what it changed.
- `hscc-bootstrap/tests/test_install_cli.py` (NEW): 5 tests, fake venv + mocked
  runner. Cover fresh-install/verify/repair/plain-fail/repair-fail. Green under
  BOTH interpreters (Hermes venv 3.11 and conda p313).
- `hscc-bootstrap/bootstrap.sh`: new HARD-STOP stage "Install: hscc CLI entry
  point" (with `--skip-cli` opt-out) that calls install_cli.py and prints
  action+shebang. Uses `$REPO_ROOT/hscc-cli` as the pip source (same
  repo-root-relative semantics as every other stage).
- `hscc-bootstrap/README.md`: documents the new stage + file + flag.

## Verify-by-execution results (on this host)

Shebang: `head -1 $(which hscc)` → `#!/Users/desac/.hermes/hermes-agent/venv/bin/python`. ✓
Idempotency: three consecutive install_cli.py runs → `verified` each time, shebang stable. ✓
Repair path: artificially set shebang to p313, ran install_cli.py → shebang back on venv. ✓

## Constraints honoured

- No edits to hscc-bootstrap profile `memory:` / `auxiliary.compression`
  (separate `profile-provisioning` milestone).
- Base on main → merge to main → push → run `python3 hscc-bootstrap/install_payload.py`.
- Public repo: no LAN addresses, no secrets. Working notes live here, not repo root.

## To be run after merge+push (card verify block)

- `head -1 $(which hscc)` → venv
- `hscc check` → `Result: OK`
- `hscc project project sessions hscc` → lists orchestrator session
- re-run bootstrap → shebang survives
