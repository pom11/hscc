kind: Verified
task: t_8ad06a53

* `hscc_daemon/tests/test_node_pause_expiry.py` anchored its whole fixture
  clock to a literal instant (`_T0 = 2026-11-22 12:00 UTC`), so `at(3600)` was
  "an hour ahead" only until the wall clock reached that hour. Past it, every
  neighbour pause in the file read past-due, `resume_host`'s span guard — which
  deliberately ignores expired pauses — stepped aside, and the two span-refusal
  tests (`test_the_verb_refuses_to_resume_half_a_span`,
  `test_the_rule_is_in_the_policy_not_only_in_the_tick`) asserted a refusal that
  the code no longer owed them. The anchor is now import time, which is what the
  file's own comment already promised ("facts about the fixture, not about when
  pytest ran"). Observed as a false gate: `leg hscc_daemon` green on main at
  12:45 UTC, red at 14:14 UTC on an unrelated PR with
  `{'action': 'resumed', 'ok': True}` where the refusal was asserted.
