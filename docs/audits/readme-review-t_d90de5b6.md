# README review: root + hscc-cli + install (t_d90de5b6)

Card: PART 2 README REVIEW (1/5) — root README + hscc-cli + install.
Verify TRUE after telegram removal, main-only cutover, Rich CLI theming.
VERIFY EVERY DOCUMENTED COMMAND BY RUNNING IT. Fix or delete stale READMEs.
PUBLIC repo — scrub LAN/tailnet addresses to 100.64.0.1 in any commit.

SCOPE (per card title): root README + hscc-cli README + install README.
(hscc-skills README is handled by a separate card — NOT in scope.)

## Root README (README.md) — VERIFIED, 2 fixes needed

Commands RUN (all exist + correct per Rich CLI):
- hscc status         -> OK (daemon STOPPED warn, streams table, Rich box)
- hscc verify         -> rans; exit 0, "✗ Some checks failed" (env: models down)
- hscc cluster status -> OK (workloads + 4 idle hosts)
- hscc stats          -> OK (last 7 days digest)
- hscc throughput     -> OK (fleet throughput)
- hscc autoscale      -> OK (NONE, healthy band)
- hscc template list  -> OK (Rich table of templates incl. 4node-coding)
- hscc template validate 4node-coding --structural-only -> OK, exit 0
- hscc template preview 4node-coding -> fails LIVE (TemplateIntentError: family
  'coding': no available worker nodes) — command EXISTS + correct; failure is
  environmental (autodown has cluster down, pool empty), NOT a stale doc.
- hscc template apply 4node-coding --confirm  (command exists per `template`
  help — NOT run: side effect)
- hscc project --help -> OK (flightdeck usage list: standup, review, etc.)
- hscc api status     -> OK (running)
- hscc api start      (exists per `hscc --help` — NOT run: starts server)
- hscc autodown status -> OK (ENABLED idle_minutes=120)
- hscc autodown enable/wake/cancel/disable  (exist per `autodown` help — NOT run: mutating)
- hscc escalate       -> OK (no escalations pending)
- roles CLI: ~/.hermes/plugins/hscc-roles/hscc.py --help -> OK; generate/create/
  list/autonomy all present (line 224-228, 240-243 accurate)
- referenced docs all exist: docs/DESIGN-template-explicit-placement.md,
  docs/design/idle-autodown.md, docs/PROJECT-COMMANDS.md,
  hscc-project/docs/COMMANDS.md, docs/API.md, patches/MANIFEST.md, docs/README.md,
  scripts/README.md, hscc-cluster/templates/README.md, hscc-skills/README.md,
  hscc-bootstrap/README.md, memori_byodb/README.md, assets/hscc.png
- roles: 25 specs in hscc-roles/roles/ -> "22+" true.

### Root README FIXES
F1 (line 11): version badge 1.17.1 -> 2.1.1
   (VERSION=2.1.1, `hscc --help` prints v2.1.1, CHANGELOG [2.1.1] 2026-09-24,
    git tag v2.1.1). Badge text + URL-label + shield alt: version-2.1.1.
F2 (line 250): escalation notifies via DESKTOP, not Telegram.
   scripts/escalate_watcher_run.py: "--no-agent --deliver desktop" (stdout
   delivered verbatim via desktop notification). Telegram removed (commit
   f91924c). Line 198 already updated. -> change "to your Telegram group" to
   desktop wording.

No other root README fixes: everything else verified current/accurate.

## hscc-cli README (hscc-cli/README.md) — STALE, rewrite

Predates main-only cutover + interpreter fix (Jun 20). Issues:
- `pip install ./hscc-cli` as install instruction is misleading: the CLI must
  run under the HERMES venv interpreter (imports hermes_cli/hermes_state; needs
  the venv's macOS Local Network TCC grant). A bare pip install into another
  interpreter bakes a wrong shebang that silently breaks `project chat/sessions/
  ask` + `hscc check`. install_cli.py (hscc-bootstrap, stage 3b) installs it
  INTO the Hermes venv by construction.
- "How it works" probe-path description is a loose paraphrase; the 4th-candidate
  parent arithmetic is fragile. -> simplify to truthful description.

REWRITE (new content drafted below in WORKSPACE file hscc-cli/README.md).

## install README (install/README.md) — VERIFIED ACCURATE, no change

- install/hscc-skills/ is the vendored source-of-truth (hscc.py
  _find_skills_source walks up to <repo>/install/hscc-skills; BUNDLED_SKILLS
  lists hscc, hscc-cluster, hscc-model-onboard, sdlc-review, devops + generics —
  all present in dir).
- `hscc-skills/hscc.py install-skills` copies to ~/.hermes/skills/ (verified
  cmd_install_skills; SKILLS_DEST = ~/.hermes/skills).
- Table list accurate (representative). Plugins-not-via-install note accurate.
- install/skill list in README all present on disk. -> NO CHANGES.

## Not fixed / noted
- `hscc template preview 4node-coding` fails on this host because autodown has
  the cluster down (no available worker nodes). Command is correct; this is an
  environmental/live-cluster limitation. Not a README change.
- hscc-skills README out of scope (separate card).
