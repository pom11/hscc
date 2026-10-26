# t_9a9aeab1 — brew venv lacks hermes runtime deps: fix design + evidence

Status: IN PROGRESS (append findings as they land)

## Root cause — CONFIRMED (re-verified on this mac's real keg, 2026-10-26)

Measured with a probe script under the shipped keg interpreter
(`/opt/homebrew/Cellar/hscc/<ver>/libexec/venv/bin/python`, version-stable path —
no operator-identifying host in this doc):

    exe: <keg>/libexec/venv/bin/python
    import hermes_cli:   ModuleNotFoundError: No module named 'hermes_cli'
    import hermes_state: ModuleNotFoundError: No module named 'hermes_state'
    import httpx:        ModuleNotFoundError: No module named 'httpx'

The brew formula (`Formula/hscc.rb`) creates `venv =
virtualenv_create(libexec/"venv", python3, system_site_packages: false)` and
installs ONLY PyYAML/rich/psutil/markdown-it-py/mdurl/pygments. So
`flightdeck.core.project_lifecycle._open_profile_session_db` (the
`from hermes_cli import ...` / `from hermes_state import SessionDB` chain)
dies with ModuleNotFoundError (re-raised HermesRuntimeUnavailable) or, when
hermes_cli *is* importable but its deps aren't, dies inside the import chain
(auth_codex -> httpx) with a raw ModuleNotFoundError that is NOT caught by
`except ImportError` ... (verify: ModuleNotFoundError IS a subclass of
ImportError — see findings; the httpx chain is caught by the same except).

## Call sites that touch the hermes runtime from the CLI process

(imports of hermes_cli / hermes_state / state.db under the brew venv)

- hscc-project/flightdeck/core/project_lifecycle.py:364 `_open_profile_session_db`
  — the single opener every CLI-side session read/write funnels through:
  - :408 ensure_session (project new / repair step 5b)  [WRITE]
  - :461 seed_session_with_digest (chat seed)            [WRITE]
- hscc-project/flightdeck/core/session_discovery.py:73 (read-only wrapper, used
  by `_runtime_error` probe + `_list_telegram_sessions` + orchestrator row)  [READ]
- hscc-project/flightdeck/core/digest.py:97 `_probe_runtime`                [PROBE]
- hscc-api/routes_orchestrator.py has its own copies — NOT in scope: the API runs
  under the hermes venv by design (api_launchd installs that unit).

## Design decision (Option A — delegate, do NOT vendor)

Chosen: subprocess delegation to the hermes venv interpreter. Justification:
- vendoring hermes_cli/httpx into the brew venv duplicates a moving runtime's
  package set -> version drift renders an environment fault as a data fact
  (the exact trap this error class exists to prevent) + widens tap supply surface;
- precedent already in-repo for resolving the hermes venv interpreter:
  hscc_daemon/install.py:103 (`~/.hermes/hermes-agent/venv/bin/python`),
  hscc_daemon/api_launchd.py:63 (`$HERMES_HOME/hermes-agent/venv/bin/python3`),
  hscc_daemon/lifecycle.py:63, hscc-bootstrap/install_cli.py;
- no suitable `hermes` CLI verb exists for the ensure-session step:
  `hermes sessions` has list/rename/recover/repair — NO "create session titled X
  if absent" verb, and `hermes profile create` has no session seed. Reuse is
  therefore impossible for ensure_session; delegation keeps Hermes' OWN
  SessionDB machinery authoritative (same ops as routes_orchestrator).

## Findings (append)

- [x] Root cause re-verified mechanically (probe output above).
- [ ] rollback: `hermes profile delete -y` cleanup semantics (alias wrapper?) — check hermes_cli/profiles.py
- [ ] existing test seams: _session_db / _default_db injected by tests (never proxy) vs production default opener
- [ ]
