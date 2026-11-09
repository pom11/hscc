kind: Fixed
task: t_3a0bdca9

- **`/cluster-down` cleared nothing, so the daemon resurrected the fleet it had
  just stopped.** This is the same bug as `hscc cluster down`, on the *second*
  surface and worse: the slash verb ran `sparkrun stop --all --hosts <node>`
  per node and never called `clear_expected_up` at all, so there was no
  stay-down intent on disk anywhere for `check_workers` / `check_proxy` to read.
  It now claims the record through the SAME entry point as the CLI verb —
  `post_boot.clear_expected_up("/cluster-down")` (D1/D5), reached by a fail-soft
  `_post_boot_module()` in the tolerance style of `hscc-cluster/hscc.py:501-519`
  (a `post_boot` import failure or a raising clear must never break the verb —
  it prints `⚠️ stay-down marker could NOT be written (…)` instead). **Written
  even when a node's stop failed**, the same fail-safe direction the CLI verb
  documents at `hscc-cluster/hscc.py:530-535`: "down, no relaunch" is always the
  safe reading of a deliberate down. The unconfirmed/`--dry-run` preview path
  touches nothing — no stop, no marker. The confirmed output gains one line
  naming the record and the escape hatch together, because **there is no
  `/cluster-up` slash verb**: `fleet marked deliberately down — the daemon will
  not relaunch workers, the proxy or the notifier until `hscc cluster up``.
  **Output truthfulness (D6):** a node whose stop output says there was nothing
  to stop is marked `○ <node>: nothing running`, not `❌ … stopped — <err>` —
  sparkrun exits 1 on `Error: No running workload matches intent …` for a node
  that was already down, which used to read as a failure the operator had to
  chase. The mark is applied on the exit-0 `No sparkrun containers running.`
  spelling too, because `✅ … stopped` there claims a stop that never happened.
  Deliberately *not* matched: `Error: could not query <host>: …` / `No …
  containers found on the hosts that could be queried.` — a host that could not
  be queried may still be running containers, so calling it "nothing running"
  would be a new lie replacing an old one. A real error keeps `❌` with its
  error tail.

---
kind: Verified
order: 1
task: t_3a0bdca9

- `hscc-commands/tests/test_cluster_down_stay_down.py`: **9** new cases on this
  dir's existing `cmdlib._run` fake style — confirmed down with every node
  "nothing running" (no `❌` anywhere, `○` per node, `clear_expected_up` called
  **once** with the verbatim `/cluster-down` reason, summary line present); the
  exit-0 already-down spelling is not a `✅` either; a real error keeps its
  `❌` + tail and the `could not query` shape is never softened to `○`; the
  marker is still stamped when a node's stop failed; the summary line names
  `hscc cluster up` and the absence of a `/cluster-up` verb in one breath; an
  unconfirmed invocation calls neither `cmdlib._run` nor `clear_expected_up`;
  and the two fail-soft shapes — `hscc_daemon` poisoned in `sys.modules` so the
  **real** import tolerance is what returns `None` (not a patched seam), and a
  raising `clear_expected_up` — both warn with `⚠️` and still complete the verb.
  Focused run: `cd hscc-commands && pytest -q tests/` -> **78 passed** (69
  pre-existing + 9 new). All five mutation probes of the new code caught (marker
  call dropped, reason reworded, already-down rule disabled, already-down rule
  widened to `could not query`, the ⚠️ branch replaced by the success line).
  The autouse sparkrun fence stayed armed and the operator's real
  `~/.hscc/expected-up.json` was not written by any test — unit tests only, no
  live `cluster down`, no ssh, no sparkrun call.
  Full suite `bash scripts/run_tests.sh` from a clean worktree at this commit:
  **11/12 legs green** — including `hscc-commands` (78 passed, the 9 new ones
  here), `hscc_daemon` (3360) and `hscc-bootstrap` (1164). The `scripts` leg is
  red and is **not** claimed as green: `1 failed, 336 passed, 2 skipped` on
  `test_a_table_identical_under_sibling_interpreters`, reproduced **identically**
  at clean `origin/main@82e098c3` (`1 failed, 336 passed, 2 skipped`, same
  `subprocess.TimeoutExpired` on the audit's 300 s cap) measured on this same
  host right after. The cap raises inside `_sibling_a_table` *before* either
  assert, so no content of this diff can cause or avoid it — attributed
  pre-existing, and the ALL GREEN stamp is withheld rather than asserted.
