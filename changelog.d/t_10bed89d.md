kind: Docs
task: t_10bed89d

- **`docs/UPSTREAM-kanban-wake-once-gate.md` §9.4 no longer asks the question — it
  records the answer.** The doc parked one open question: which `deliver_wake` branch
  the `hscc-orch` kanban sub (`platform=local`, `mode=notify+wake`) takes, since the
  identity-threading half (§3.2) is worth nothing to us if the answer is self-post.
  `t_12350485` settled it: **the push branch** — `LocalSessionAdapter` inherits
  `supports_async_delivery = True`, so the wake goes
  `gateway/wake.py:135 → admit_route_wake(..., "kanban-wake", ...) → session_admissions`,
  and `_self_post_chat_completion` sits behind `adapter_supports_push` being False and
  is structurally unreachable for this sub. Proven at runtime, not read off the source:
  9 `kanban-wake`-shaped `request_id` rows joined 9/9 to the 9
  `kanban notifier: woke agent … profile=hscc-orch` log lines, each carrying the
  `uuid.uuid4().hex` nonce minted at `gateway/session_automation.py:837` — the exact
  token §3.2 replaces, on the branch we take. **Verdict: §3.2 is enough for hscc**, so
  the upstream ask does *not* extend to `_self_post_chat_completion` and no comment was
  added to [NousResearch/hermes-agent#152206](https://github.com/NousResearch/hermes-agent/issues/152206)
  (that follow-up was conditioned on the self-post branch; the issue stays untouched).
  Two caveats now bind §3.2 and §5's acceptance test, both measured in the same store:
  the `admit_internal_event` fallback (`gateway/wake.py:149-151`) delivers a wake with
  **no** admission row (0 of 9 observed), and session retirement moves rows into
  `state_meta` out of `find_session_admission`'s reach — so a fence row is evidence, not
  a receipt, the dedupe token must be re-derivable from event identity, and deleting
  `gateway_wake_cursors` / `gateway_already_delivered` still requires §3.1's board-DB
  claim. Every anchor re-checked against the live runtime tree at
  `045881137d6a177cac690b1d373316ce8c271f43`; orchestrator session id redacted.
