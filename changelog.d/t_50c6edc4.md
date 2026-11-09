kind: Fixed
task: t_50c6edc4

- **A deliberate `hscc cluster down` still paged the operator.** The fleet was
  torn down at 17:51:23 and daemon.log recorded
  `macOS notification sent: HSCC: orchestrator/DGX unhealthy` at 17:52:19. The
  page came from the shipped `orch-dgx-down` rule (`condition.metric ==
  failed_dgx`, `trigger_type: notify`): a teardown makes that metric — and
  `vllm_down` and the engine wedge — true BY CONSTRUCTION, and
  `trigger.fire_trigger_action` had a gate on `auto_restart`
  (`_intentional_autodown`) but none on `notify` and no knowledge of a manual
  `cluster down` at all. The notify branch now checks the serving-layer rules
  against t_ab75e987's durable record through one new seam,
  `trigger._operator_down(deliberately_down_fn=None)` (deferred `post_boot`
  import, any exception ⇒ `None`), threaded through
  `trigger_engine(..., deliberately_down_fn=None)` so the real loop passes
  nothing and reads the real predicate. Suppression is per-rule, never global,
  and covers exactly the three serving-layer signals named by
  `_OPERATOR_DOWN_SUPPRESSED_METRICS` — `failed_dgx`, `vllm_down`,
  `state.engine_wedge.degraded` — logging one INFO line
  `Trigger <rule_id>: notify suppressed — fleet deliberately down (<reason>)`
  before `send_macos_notification` can run. `nas_down` and `watchdog_blocked`
  keep firing: a lost NAS mount and a tripped watchdog are not fleet-serving
  signals. `emit_event` and `block_pipeline` are structurally untouched (the
  gate lives inside the `notify` branch only), and the `auto_restart` gate is
  byte-identical — the two gates answer different questions and are deliberately
  not merged.
  **Which writers count as a stay-down (decided, not string-matched):**
  `clear_expected_up(reason)` has two sanctioned callers —
  `hscc-cluster/hscc.py:547` (`hscc cluster down`, the operator) and
  `hscc_daemon/autodown.py:2319` (`autodown: intentional idle teardown`) — so
  the record means "someone deliberately cleared the footprint", not "an
  operator typed the verb", and BOTH are honoured: the idle teardown has no
  other notify gate (health.py's checks get there first via
  `_intentional_window()`, the trigger engine does not). `reason` is copied into
  the log line and nowhere else — no substring of it is ever tested, so an
  operator-chosen wording cannot become a safety property (pinned verbatim for
  both producers, plus a third arbitrary wording, in
  `hscc_daemon/tests/test_trigger.py`). Fail-safe direction is toward PAGING: a
  raising predicate, a non-dict, or a record missing `set_at`/`reason` reads as
  no evidence and the page goes out as it did before the record existed.
  One spec detail worth flagging: the shipped `engine-wedge-detected` rule keys
  on `metric == "event_type"` and carries `state.engine_wedge.degraded` in
  `condition.value`, so matching `condition.metric` alone would have left that
  rule paging straight through the gate — `_serving_layer_notify` matches the
  value for `event_type`-keyed rules and nothing else.
