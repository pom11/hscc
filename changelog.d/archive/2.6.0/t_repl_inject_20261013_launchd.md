kind: Added
task: t_repl_inject_20261013

- **Opt-in boot resilience for `hscc api`** — `hscc api install-service`
  writes `~/Library/LaunchAgents/com.hscc.api.plist` (RunAtLoad + KeepAlive,
  the real `python3 -m api_server serve` entrypoint with the unchanged
  loopback/token bind rules) so a REBOOT no longer takes the iPhone client
  offline. Strictly opt-in: the command writes the file and prints the
  `launchctl bootstrap` step — it NEVER loads the unit itself (the module has
  no subprocess import at all, and a test enforces that structurally).
  `hscc api uninstall-service` removes the file. The bootstrap reports the
  current state and the opt-in command but never runs either. Honest note
  (same asymmetry as the daemon unit): `hscc api status` reads the PID file,
  so a launchd-run instance looks stopped there — trust `launchctl print`.
