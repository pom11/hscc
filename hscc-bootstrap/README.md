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
   Then **restarts** the daemon via `hscc stop`/`hscc start` so freshly-installed
   code is ALWAYS loaded (the setup script leaves an already-running daemon
   alone) and verifies the PID turned over; a failed restart warns, never aborts.

**Telegram is optional.** During configuration bootstrap asks
`Add Telegram integration? [y/N]` (default **No**). Declining skips Telegram
credential wiring cleanly — HSCC stays fully functional without it (cluster ops,
kanban, dispatch, and the API path are all Telegram-independent). An operator's
existing Telegram config is never stripped or destroyed by answering "no" on a
later run.

## Files
| File | Role |
|------|------|
| `bootstrap.sh` | the orchestrator script |
| `doctor.py` | preflight checks |
| `detect.py` | parse `sparkrun cluster list` |
| `install_payload.py` | copy repo → runtime (backup/guard/exclude tests) |
| `enable_plugins.py` | idempotent config wiring |
| `install_soul.py` | SOUL + ops personality (sentinel blocks) |
| `serving_gen.py` | build serving.json from the detected cluster |
| `suggest_template.py` | suggest a node-count template |
| `apply_patches.py` | reapply the WS8 upstream patch set (see `../patches/`) |
| `telegram_choice.py` | resolve the optional Telegram decision (env → --yes → prompt) |
| `restart_daemon.sh` | restart the daemon via `hscc stop`/`hscc start` + verify PID turned over |

## Flags
`--yes` non-interactive · `--force` regenerate serving.json · `--no-backup` ·
`--skip-skills|--skip-roles|--skip-daemon|--skip-patches`.
Telegram: `--telegram=yes|no` or env `HSCC_TELEGRAM=yes|no` force the decision
without prompting (unattended runs); otherwise `--yes` declines (documented
default) and an interactive run asks `Add Telegram integration? [y/N]`.

## Tests
`tests/` — 258 tests incl. an end-to-end stage-sequence test and the daemon-restart helper tests. `python -m pytest tests/ -q`.
