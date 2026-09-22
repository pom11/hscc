# Card t_7a771891 — project digest <name> (bounded archive digest)

STATUS: in progress

## Plan
- [x] Read tree (bindings, session_discovery, map_sessions bounded_sample, archive, project.py commands)
- [x] Write flightdeck/core/digest.py (new module): bounded digest synthesis over archive md files
- [ ] Wire cmd_digest in commands/project.py (argparse + runtime-error rendering + exit codes)
- [ ] Tests in tests/test_digest.py (temp archive dir + mapping.json)
- [ ] Full suite green on hscc-project (1229 baseline) via hermes venv
- [ ] Merge onto dev, push, report merge sha

## Design decisions
- Primary source: archive md files under <archive_root>/<project>/ (`~/.hermes/archive/telegram/<project>/`)
- Per thread: title, date range (started..ended from header), message count (header), decision (bounded head+tail user/assistant extract mirroring map_sessions.bounded_sample), resume line (`hermes -p default --resume <id>`) — telegram sessions live on the DEFAULT profile.
- Runtime probe: reuse project_lifecycle.HermesRuntimeUnavailable via _probe_runtime(); surfaces as runtime_error, never as empty digest. Injectable _runtime_error_fn seam for tests.
- New command, plain text output (--json optional, clean); never physically merge sessions.
