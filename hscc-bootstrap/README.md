# hscc-bootstrap

The installer. A fresh `git clone ~/dev/hscc && hscc-bootstrap/bootstrap.sh`
turns an official Hermes + sparkrun machine into a fully-wired HSCC node.

## What it does (stages, in order)
1. **Prerequisites (doctor)** — `doctor.py` checks python / PyYAML / sparkrun +
   cluster / Hermes / NAS / disk and explains failures in plain language; a fatal
   miss hard-stops.
2. **Detect + Configure** — reads the cluster from `sparkrun cluster list`; picks
   orchestrator + recipe (defaults under `--yes`); suggests a node-count template.
3. **Install: plugin files** — copies the plugin tree into `~/.hermes/plugins/`
   (backup-then-overwrite; `--no-backup` to overwrite in place). The repo is the
   source of truth; the runtime is a copy. Hard-stops on copy failure.
3b. **Install: hscc CLI entry point** — installs `hscc-cli` INTO the Hermes venv
   (`install_cli.py`) so the console script's shebang points at the Hermes venv
   BY CONSTRUCTION. The `hscc` CLI reads `hermes_cli`/`hermes_state` (Hermes-venv
   only) and needs the venv's Local Network (TCC) grant, so a p313 shebang
   silently degrades `project chat/sessions` and `hscc check`. Idempotent:
   verifies the shebang each run and force-repairs on drift; reports what it
   changed. `--skip-cli` to opt out. Hard-stops on failure.
3c. **Install: git hooks (public-repo address guard)** — `install_hooks.py`
   points `core.hooksPath` at the repo's committed `.githooks/` so the address
   guard runs at **commit time**, not only under pytest (plain `.git/hooks` is
   not cloned, so it cannot be the mechanism). Writes to the COMMON git config,
   so every linked worktree — the dispatcher's worker worktrees included — is
   armed by ONE install. Idempotent (`verified` on re-run; repairs a missing exec
   bit; `skipped`, never faked, for a non-checkout or a hookless revision).
   Non-fatal: a skipped/failed stage warns that the guard is INACTIVE.
4. **Install: skills / role profiles / ~/.hscc + serving.json**.
5. **Install: hermes/sparkrun patches** — reapplies the curated upstream patches
   (`apply_patches.py`) so the kanban review + resume hooks land on official
   hermes. `--check`-gated + non-fatal; `--skip-patches` to opt out.
6. **Install: enable plugins + toolsets** — `enable_plugins.py` wires
   `plugins.enabled`, `toolsets`, kanban routing (`default_assignee`,
   `auto_review` reviewer pairing, `failure_limit=3`), delegation → the worker
   proxy `:4000`, compaction → `:4000` (off the orchestrator), and a fallback
   provider. Idempotent: fills empty fields, preserves operator choices.
7. **Install: SOUL + ops personality** — `install_soul.py` writes the topology-free
   HSCC identity + doc-driven/review-gate guidance (sentinel-managed blocks).
8. **Install: daemon** — launchd (macOS) / systemd --user (Linux); resolves a
   real python (venv-preferred), so it won't respawn-loop on Homebrew-only hosts.
   Then **restarts** the daemon via `restart_daemon.sh` so freshly-installed code
   is ALWAYS loaded (the setup script leaves an already-running daemon alone).
   With the service loaded that restart goes through the supervisor — `launchctl
   kickstart -k` on macOS, `systemctl --user restart` on Linux — the only restart
   that keeps the service's interpreter and supervisor, and the PID is verified
   from the supervisor itself; a failed restart warns, never aborts.

## Files
| File | Role |
|------|------|
| `bootstrap.sh` | the orchestrator script |
| `doctor.py` | preflight checks |
| `doctor_node_recovery.py` | the doctor's per-node node-recovery check (watchdog armed + panic sysctls in sync), read-only over sparkrun ssh; reuses `hscc-cluster/node_recovery.py` + `install_autoreboot.py` as its single source of truth |
| `detect.py` | parse `sparkrun cluster list` |
| `install_payload.py` | copy repo → runtime (backup/guard/exclude tests) |
| `install_cli.py` | durable `hscc` CLI entry-point install (into Hermes venv) — shebang correct by construction, verify/repair on drift |
| `enable_plugins.py` | idempotent config wiring |
| `install_soul.py` | SOUL + ops personality (sentinel blocks) |
| `serving_gen.py` | build serving.json from the detected cluster |
| `suggest_template.py` | suggest a node-count template |
| `apply_patches.py` | reapply the WS8 upstream patch set (see `../patches/`) |
| `restart_daemon.sh` | restart the daemon the supervised way — `launchctl kickstart -k` (macOS) / `systemctl --user restart` (Linux) when the service is loaded (PID verified from the supervisor, never the pid file), `hscc stop`/`hscc start` only when no service is loaded — and refuse to start a second loop beside an unsupervised daemon |

## Flags
`--yes` non-interactive · `--dry-run` print every planned write, perform none ·
`--force` regenerate serving.json · `--no-backup` ·
`--skip-skills|--skip-roles|--skip-daemon|--skip-patches|--skip-cli`.

## Running from a brew libexec tree (t_bc5a3fee, real layout pinned t_0f5616fd)

The homebrew-hscc tap installs the release tree at the version-STABLE path
`<prefix>/Cellar/hscc/<ver>/libexec/hscc/` and puts `hscc` on PATH through brew
itself. `bootstrap.sh` is location-relative (`BOOT_DIR`/`REPO_ROOT` from
`BASH_SOURCE`), so the payload stages work unchanged from that tree. Two
behaviours differ there:

* **CLI shim stage is skipped.** `install_cli.py` would write a second `hscc`
  console script into the Hermes venv, shadowing (or being shadowed by) the
  brew-owned one — surprising and invisible to `brew`. The skip prints why.
* **Detection** is a marker file `.hscc-brew-libexec` at the extracted tree
  root when one exists, with a path-shape fallback (`*/Cellar/*` or
  `*/libexec/hscc-*`) for taps that ship without it. The SHIPPED formula drops
  no marker and its payload dir is not named `hscc-*`, so on a real keg the skip
  rests on `*/Cellar/*` — pinned by the shipped-keg cases in
  `tests/test_bootstrap_dry_run.py`.

`hscc bootstrap [flags]` reaches the same script from any layout: the verb
resolves `bootstrap.sh` as a sibling of the resolved package dir (repo
checkout / in-tree copy), else under `$HERMES_HOME/plugins`, else by walking
ancestors for `libexec/hscc/` (the shipped tap) or `libexec/hscc-*/` (a
versioned tree; the brew venv case). `HSCC_BOOTSTRAP_SH`
overrides the lookup outright.

## Tests
`tests/` — 467 tests collected (verified 2026-10-23 against this tree; re-count
with `python -m pytest tests/ --collect-only -q`) incl. an end-to-end
stage-sequence test, the daemon-restart helper tests, and
`tests/test_bootstrap_dry_run.py` — fabricated brew-libexec trees proving `--dry-run`
writes nothing and the brew shim-skip fires (marker + path heuristic).
`python -m pytest tests/ -q`.
