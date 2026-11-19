kind: Added
task: t_6af94eae

- **The main orchestrator now has ONE merged SOUL: board duties plus cluster
  authority.** `hscc-roles` generates the profile named at bootstrap (default
  `general`) with the old general catch-all's board work and the `orchestrator`
  role's cluster authority in a single identity. Its SOUL says it knows every
  registry project, orchestrator profile and board; answers "what is happening"
  from live state first (boards and cards, orchestrator panes via
  `hscc orch inject --dry-run` and pane tails, PRs and checks, `hscc status` /
  `hscc doctor`, the proxy) and never from memory alone, in short plain answers;
  keeps standing watch cards parked `blocked` with a `watch` reason so the
  dispatcher never hands them to a worker, and works them on wake-ups (stuck or
  failing cards, wedged panes, drift into meta/side cards, duplicate cards,
  cards completed while their PR is not on main); and talks to other
  orchestrators only through `hscc orch inject` (steer when busy, plain when
  idle) and the board tools, in English. Its authority is everything — models,
  boards, cards, other orchestrators, reviews, merges, settings, node recovery,
  and releases end to end including the post-release CLI upgrade + bootstrap
  with the post-bootstrap checks (profiles YAML-compare, kanban caps, autodown
  status) — except a LIVE publish, which raises an alert with `hscc notify` and
  waits for the operator's OK. The standing rules are written into the same
  SOUL: autodown exactly as the operator set it, no built apps installed on the
  gateway host, public-repo rules, no attribution lines, no destructive git or
  `rm -r` without the operator, and fleet access only through sparkrun/hscc.

- **Existing installs migrate without losing anything, and re-running is a
  no-op.** `orch-all` creates the main profile when it is missing, copies the
  legacy catch-all's `memories/` and `sessions/` into it per file —
  destination wins, the source is never moved or deleted — and marks both the
  legacy catch-all and the legacy cluster-wide role retired in their SOULs
  ("retired — use `<main>`"), leaving both profiles on disk. `generate` also
  stamps the legacy cluster-wide role retired on every run, so a regeneration
  cannot re-mint a competing authority. The migration never rewrites a config
  file; the generator's existing YAML merge keeps every operator key.

---
kind: Verified
task: t_6af94eae

- **The fresh, upgrade and idempotence paths are pinned in a temp
  HERMES_HOME/HSCC_DIR.** `hscc-roles/tests/test_main_orch_migration.py` covers
  a fresh install (main profile created, no legacy profile invented, full SOUL
  contract including the roster and every standing rule), an upgrade with an
  existing catch-all and cluster role (missing memories/sessions copied,
  existing main memory/sessions untouched, both sources still present, both
  SOULs retired with their old content preserved), a byte-identical second run
  (no file rewritten, no duplicate retirement marker, no `migration` block),
  destination-directory creation, config keys surviving main-profile
  generation (`__config_version`, `browser`, operator memory/compression
  values) and legacy configs never being rewritten. A real-CLI smoke run of
  `hscc.py orch-all --hermes-home` on a seeded throwaway home reproduced the
  upgrade (copied + retired on run 1, no-op on run 2), and `hscc.py generate`
  marked the legacy role retired with its config preserved.
