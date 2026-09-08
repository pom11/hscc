# Report — t_ab336532: surface installed-payload drift automatically

Date: 2026-09-08
Branch: wt/payload-drift-guard
Commits:
  * 786bb67 — feat(api): warn on installed-payload drift at `hscc api start`
  * b3632a7 — fix(bootstrap): enable_plugins no longer clobbers an operator's
             max_concurrent_children (operator finding folded into same scope)

Two changes were delivered, both the same bug class the card names: bootstrap
must not silently leave the runtime wrong.

────────────────────────────────────────────────────────────────────────────
1. Payload drift: verify at the moment it matters — `hscc api start`
────────────────────────────────────────────────────────────────────────────

The check already existed and worked (verify.py:959 check_plugin_payload,
wired into run_all:1092) but was never scheduled or hooked to anything the
operator actually runs. `hscc api start` is the moment it matters: that verb
explicitly serves the INSTALLED payload, so a stale installed payload is
exactly what it would boot.

Change: hscc_daemon/api_cli.py adds _warn_payload_drift() and calls it from
_handle_start() before forking into the background serve loop. It reuses
verify.check_plugin_payload() (the SAME function run_all runs — one source of
truth, no re-implemented diff).

Behavior by result:
  * ok=None (no repo checkout on a real user install) → SILENT, start normally.
  * ok=True  (installed payload matches repo)          → SILENT.
  * ok=False (drift) → LOUD warning naming the drifted plugin + the exact
    remedy (`python3 hscc-bootstrap/install_payload.py`), then CONTINUES.

It never blocks startup: a stale payload that still boots is strictly better
than an API that refuses to start. The check is wrapped in try/except so even
a crash in the check prints a soft warning and proceeds.

Startup cost (measured): ~80 ms on a healthy install (the check sha-sums the
409 payload files). Negligible vs the fork+serve work; imperceptible to the
user. The number: 80 ms.

Proof (evidence, not assertion):
  * hscc_daemon/tests/test_api_cli.py drives the REAL check against temp
    repo/plugins dirs:
      - test_drift_produces_loud_warning — genuine differing content ->
        warning names 'hscc_daemon' + 'install_payload.py'. PASSED.
      - test_ok_None_is_silent — ok=None -> empty stdout. PASSED.
      - test_ok_true_is_silent — matching payload -> empty stdout. PASSED.
      - test_check_exception_does_not_block_startup — crash -> warns, no raise. PASSED.
  * 4/4 pass in 0.04s.
  * Bonus real-run proof (not kept in the suite): running _warn_payload_drift()
    against the live repo+plugins correctly flagged that my own edited
    api_cli.py had not yet been deployed — proof it catches the exact outage
    class ("the installed payload differs from repo: api_cli.py").

Judgement call — why STARTUP and not (only) a cron:
The card's "preferred" answer is startup, and it is the right one, for the
reason the card itself states: "verify at the moment it matters." A stale
payload only causes harm once something RUNS it. `hscc api start` is that
moment — the check runs exactly when the stale code would go live, so the
warning is delivered in-band with the failure it prevents. A cron guard
would also help (drift could sit unnoticed until the next `api start` if the
API is long-lived), but the operator's hscc_daemon is currently NOT running
and only a daemon-stream answer would guard nothing today. Startup is the
only hook guaranteed to run on the path that matters. I picked startup, and
did NOT add a cron: adding one more recurring job on top of the existing
watchers, when the operator's daemon isn't even running and the cron list
already has watchers nobody guarantees, would be a guard of uncertain
liveness. Startup is self-certifying — it fires when the thing it guards
fires.

(I did not add a cron. Rationale above. If the operator later brings the
daemon/cron back up, run_all already includes check_plugin_payload, so a
scheduled `hscc verify` would immediately cover the long-lived case with zero
extra code.)

────────────────────────────────────────────────────────────────────────────
2. Operator finding (mid-run): enable_plugins clobbers max_concurrent_children
────────────────────────────────────────────────────────────────────────────

Same bug class: bootstrap silently leaving the runtime wrong. _ensure_delegation
raised delegation.max_concurrent_children toward the default (9) whenever the
operator set a LOWER cap. The operator's cap is 2 — a runaway-queue safety
brake — and it was silently flipped back to 9 on every enable()/doctor()
reconcile (recurring: verified 2 at 13:54, and .bak files already contained 9).

Change: hscc-bootstrap/enable_plugins.py lines 270-274 now mirror the
kanban-concurrency pattern in _ensure_kanban_routing (lines 222-232). A lower
operator value is deliberate (STRICTER — throttles fan-out under load), so it
is preserved via the established _is_int_like() helper. The cap is only FILLED
when absent / not int-like. doctor.py:699's existing comment "(idempotent,
preserves operator caps)" is now TRUE — previously false for this key, no
comment edit needed.

Proof:
  * test_delegation_low_cap_survives_reconcile: fully-wired config with
    max_concurrent_children=2 -> enable() -> stays 2 (not 9), and not reported
    as changed. PASSED.
  * test_delegation_string_cap_preserved: a string-typed "2" also survives. PASSED.
  * Naming pytest selection: 10 related tests pass (routing/caps/delegation).
  * No edits to the operator's live ~/.hermes/config.yaml — temp config paths
    only (enable() takes config_path).

Caps that now hold (untouched, already correct for these): max_concurrent_children
(now preserved), max_in_progress, max_in_progress_per_profile (kanban already
preserved lower values).

────────────────────────────────────────────────────────────────────────────
What I deliberately did NOT do
────────────────────────────────────────────────────────────────────────────
  * Did NOT add a cron job for the drift check (judgement call, argued above).
  * Did NOT add a strict/fail-closed mode to api start — the card forbids
    blocking startup by default; a stale payload that boots is better than a
    dead API. (Opt-in strict mode is possible later, but the card's judgment
    and the graceful-degrade requirement point to warn-not-block.)
  * Did NOT re-implement the payload diff — reuse check_plugin_payload only.
  * Did NOT touch ~/.hermes/config.yaml, profiles, state.db, or live runtime.
  * Did NOT edit the operator's live config in any test.
  * Did NOT use Telegram anywhere.
