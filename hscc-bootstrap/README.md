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
4b. **Install: snap firmware-notifier mask (log-churn quarantine)** —
   `install_notifier_mask.py` masks snap firmware-updater's crash-looping desktop
   notifier **and the timer that starts it** for the desktop user on every
   **cluster node** — the hosts `sparkrun cluster list` resolves, whatever that
   list is today; HSCC keeps no host inventory of its own and never touches a
   node outside the resolved cluster. MASK, not disable: the unit is `static`
   (no `[Install]` section), so `disable` cannot affect it, while a mask
   symlinks the unit to `/dev/null` and survives snap refreshes and reboots.
   Unprivileged by construction — the mask lives in the user's own
   `~/.config/systemd/user/` and there is no `sudo` in the module or the
   generated script. Idempotent (already-masked → no-op, zero new `systemctl`
   calls), `--dry-run` plans without writing, and every mask is confirmed by
   reading the state back from the node. **The firmware-updater snap itself is
   untouched** — `fwupd` updates stay wanted and operator-run. Non-fatal
   (`--skip-node-recovery` to opt out of the node-recovery stages;
   `node_recovery: {firmware_notifier_mask: false}` in `~/.hscc/cluster.json`
   opts out of this one). The bootstrap doctor (`doctor.py`) has its own check
   (`snap firmware-notifier mask (node-recovery)`) that reads the mask back per
   node and goes red if it is gone, so a vanished mask cannot masquerade as
   protection.
4c. **Install: GPU clock cap** — `install_gpu_clock_cap.py` installs a systemd
   oneshot unit (`/etc/systemd/system/hscc-gpu-clock-cap.service`) on every node
   that runs `nvidia-smi -lgc 0,<cap>` at boot (default **2200 MHz**; undo
   `nvidia-smi -rgc`). Applies live, no model restart. A two-Spark sweep
   measured 99.6 % overall throughput (decode 100 %, uncached prefill 95 %) for
   -32 % GPU power and ~-5 C. `sudo -n` on the node, sparkrun transport,
   read-back verified (unit present with the wanted value + enabled + active +
   SM clock at or under the cap). Set the value with
   `node_recovery: {gpu_clock_cap_mhz: 1900}` or turn it off with `false` in
   `~/.hscc/cluster.json` (off does not remove an installed cap — run
   `install_gpu_clock_cap.py --uninstall --hosts ...`). Doctor check:
   `GPU clock cap (node-recovery)`.
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
   Then **restarts** the daemon via `hscc stop`/`hscc start` so freshly-installed
   code is ALWAYS loaded (the setup script leaves an already-running daemon
   alone) and verifies the PID turned over; a failed restart warns, never aborts.

## Files
| File | Role |
|------|------|
| `bootstrap.sh` | the orchestrator script |
| `doctor.py` | preflight checks |
| `doctor_node_recovery.py` | the doctor's per-node node-recovery check (watchdog armed + panic sysctls in sync), read-only over sparkrun ssh; reuses `hscc-cluster/node_recovery.py` + `install_autoreboot.py` as its single source of truth |
| `doctor_role_soul.py` | the doctor's role-SOUL identity check (each worker SOUL's `## Role:` header vs its profile dir name and its spec, plus any heading `compose_soul()` cannot emit). Bridge only — the audit lives in `hscc-roles/role_soul_check.py`, which owns `PROFILES_DIR`/`ROLES_DIR`/`compose_soul`; read-only, WARNING-severity, honest ERROR when the hscc-roles sibling is absent |
| `detect.py` | parse `sparkrun cluster list` |
| `install_payload.py` | copy repo → runtime (backup/guard/exclude tests) |
| `install_cli.py` | durable `hscc` CLI entry-point install (into Hermes venv) — shebang correct by construction, verify/repair on drift |
| `enable_plugins.py` | idempotent config wiring |
| `install_soul.py` | SOUL + ops personality (sentinel blocks) |
| `serving_gen.py` | build serving.json from the detected cluster |
| `suggest_template.py` | suggest a node-count template |
| `apply_patches.py` | reapply the WS8 upstream patch set (see `../patches/`) |
| `restart_daemon.sh` | restart the daemon via `hscc stop`/`hscc start` + verify PID turned over |

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
