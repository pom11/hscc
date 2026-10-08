kind: Fixed
task: t_be687dd9

- **Every shipped trigger rule fired a notification on EVERY 15 s trigger-loop
  cycle while the bad state persisted.** None of the five rules in
  `triggers.default.json` declared `cooldown_seconds`, and the engine only
  suppresses re-fires when `cooldown_seconds > 0` — so any state-based rule
  re-notified once per loop tick for as long as the stream stayed `ok: False`.
  The NAS lost-mount alert (`9e41728`, released in 2.5.6) made it visible: the
  mount went away at the 14:13 reboot and macOS got 178 "HSCC: NAS mount lost"
  notifications in 11 minutes, still climbing. Event-driven rules hid the same
  hole because a real event appears in the tail once. The fix has two layers:
  (1) the shipped defaults now carry a cooldown per rule — `nas-down` and
  `watchdog-blocked` 3600 s, `orch-dgx-down`, `vllm-down` and
  `engine-wedge-detected` 1800 s — so the floor lives in the default, not only
  in the engine;
  (2) a floor in `trigger.py` itself, for rules that **never declared** a
  cooldown: a match that reflects a persisting state — a
  `state.<stream>.degraded` pseudo-event, or a metric the engine answers by
  reading a stream file (`failed_dgx`, `vllm_down`, `watchdog_blocked`,
  `nas_down`, `state.*`) — is re-notified at most once per
  `STATE_NOTIFY_FLOOR_SECONDS` (30 min) per rule. A discrete event match is
  never suppressed, so the floor cannot drop a genuine one-off alert, and it
  stamps the cooldown file only for the fires the suppression actually
  consults. An explicit `cooldown_seconds: 0` stays the operator's deliberate
  "notify every cycle" and is honoured; an absent or unparseable value gets the
  floor, so a typo in hand-edited `triggers.json` cannot re-open the storm.
  For EXISTING installs, note the bootstrap semantics: it only ever ADDS
  missing rule ids, so new defaults CANNOT retro-fit `cooldown_seconds` into a
  live `~/.hscc/triggers.json` that already carries those ids — which is
  exactly why layer (2) lives in the engine: upgrading the daemon alone
  dampens a live file that still has no cooldowns. An operator who wants the
  shipped 1 h NAS cadence before then adds `"cooldown_seconds": 3600` to the
  `nas-down` rule in `~/.hscc/triggers.json`; no daemon restart is needed, the
  file is re-read every cycle.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_trigger.py`: +**5** cases — a persistent
  `state.nas.degraded` notifies once across three 15 s cycles when the rule
  declares a cooldown; the same rule with the key STRIPPED notifies once under
  the engine floor; an explicit `cooldown_seconds: 0` still fires every cycle
  (operator intent preserved); a second degraded stream on the same rule is
  reported once, not silently dropped by a first stream's notify (a real event
  match is not suppressed); and event-driven rules are unaffected by the
  floor. `hscc-bootstrap/tests/test_install_triggers.py`: +**2** cases —
  `DEFAULT_COOLDOWNS` contract (hardcoded id → seconds, every shipped rule must
  match and be > 0) and a fresh install writes those cooldowns into
  `triggers.json`.
