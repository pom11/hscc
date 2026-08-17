# Bot Mode (v2026.8.16.2) — Relevance to HSCC's Headless Deployment

**Investigation report — task t_ba307d02**
**Date:** 2026-08-18 (against runtime `hermes-agent v2026.7.30-2-gb1f96d250e`, HSCC v1.8.3)

This is a skeptical assessment, not a feature pitch. The conclusion up front: the
Bot Mode **UI plugin does not apply to this deployment**, the underlying
mechanisms are already present in HSCC, and the genuinely valuable part of this
release is NOT Bot Mode at all — it's the upstream **kanban worktree, cron
self-heal, and session-handoff data-loss fixes**, which map directly onto real
incidents from this past week. Those belong in the PR #19 cluster verification.

---

## Ranked verdict (what, if anything, is worth doing)

| # | Item | Verdict | Worth doing? |
|---|------|---------|--------------|
| 1 | Bot Mode **UI plugin** (desktop app) | **Not applicable.** No Hermes Desktop app exists or is used anywhere in this deployment. The plugin loads from `~/.hermes/desktop-plugins/` **on the machine running the desktop app** — we have no such machine. | **No.** Don't install, don't plan for it. |
| 2 | Group-chat coordination **protocol** (headless) | **Same problem, already solved differently — kanban is strictly better for HSCC.** No real gap. | **No follow-up card.** Keep kanban dispatch. |
| 3 | "Make flightdeck manage profiles" | **Mostly already solved** by HSCC's own role framework (`hscc-roles create/generate` + native profile API). A real, small gap: profile creation isn't surfaced in the unified `hscc` CLI, and the descriptor→profile data model differs. | **Optional, low-priority card** — only if we want profile ops under one command. See below. |
| 4 | Non-Bot-Mode fixes in v2026.8.16.2 (kanban worktree, cron self-heal, session handoff) | **Directly relevant to real incidents this week.** These are the part of the release to prioritize in the PR #19 cluster verification. | **Yes — prioritize in PR #19 verification.** Flag below. |

---

## Item 1 — Does a Hermes desktop app exist anywhere? → NO

Verified by direct inspection (not assumption):

- **Processes:** all hermes processes are headless — `hermes -p architect --cli ... chat`
  (kanban worker), `hermes_cli.main gateway run --replace` (the gateway), MCP stdio
  watchdogs, the `~/.hermes-tg/mcp_server.py` Telegram bridge. No Electron/desktop
  process anywhere.
- **Plugin layout:** `~/.hermes/plugins/` contains only the `hscc-*` headless plugins.
  There is **no `desktop-plugins/` directory at all** — which is the exact directory
  Bot Mode must be installed into (`~/.hermes/desktop-plugins/` on the desktop-app
  machine). Since it doesn't exist, nothing was ever installed there.
- **Connected platforms:** `~/.hermes/channel_directory.json` lists **only `telegram`**
  as a platform. No desktop connection is registered against the gateway.
- **Apps:** no Hermes Desktop app in `/Applications` or `~/Applications`.

The source tree `apps/desktop/` exists under `~/.hermes/hermes-agent/` (and `node_modules`
is installed there from the repo's own dev setup), but `git describe` reports the installed
runtime as `v2026.7.30-2-gb1f96d250e` — i.e. it predates the v2026.8.16.2 release that
bundles Bot Mode, and even so the presence of a source tree is not a running desktop app.
There is **no built (`dist/`) app and no running instance.**

**Conclusion:** The Bot Mode UI plugin is categorically inapplicable here. This matches
the task's own expectation ("this doesn't apply" is the likely and correct answer). We do
**not** manufacture a use case.

---

## Item 2 — Is the group-chat coordination protocol worth adopting headless? → No.

Bot Mode's "group chat" is a coordination **protocol** (not a new backend primitive):
2–6 bots, @mention rounds, serial turns, hard caps (10 msg/turn, 3 rounds), a "needs you"
@user escalation, and per-bot persistent `Group: <name>` sessions. It's implemented in
the desktop plugin's own JS, on top of ordinary per-profile CLI chat invocations
(`hermes -p <bot> chat --in ~ -c "Bot Chat" -Q -q "Message from 🤖 <sender> (@<sender>): ..."`).

HSCC already solved the same coordination problem with **kanban cards + dispatch**:

- **Durable, reviewable, replayable record.** Every handoff has a task row, a parent/child
  edge, comment thread, git worktree, commit trail, and review gate. Bot-mode group chat is
  an **ephemeral live back-and-forth** — nothing survives the round except each bot's own
  session. HSCC's kanban model is strictly better for anything that must be auditable,
  resumable across a crash, or hand-waved to a human reviewer.
- **Dependency-ordered, not turn-serial.** Kanban expresses arbitrary DAG dependencies
  (fan-out, fan-in, child-waits-for-parents) with automatic promotion. Bot-mode rounds are
  a flat serial loop with hard caps — weaker.

The only thing group chat plausibly adds is **faster turnaround for genuinely interactive
back-and-forth** where nobody needs a durable card — the example raised is the "live
thermal-investigation collaboration" (t_f50f61ec, the vLLM spin-wait investigation). But
note how that collaboration actually ran: as **separate kanban cards per investigation**,
each fully reviewable, trading findings through commits/comments on the board. That is the
correct shape even for "interactive" multi-agent work in HSCC, because the writes are
durable and a human can audit them. The workers on those cards already have the full
terminal+file+board toolset to iterate live among themselves *within* a card; they do not
need a separate chat protocol.

**Verdict:** kanban dispatch already gives HSCC everything group-chat-style coordination
would add, plus durability and review the chat form lacks. **No gap worth closing.** Do not
build a headless emulation of the Bot Mode group-chat protocol.

*(The underlying "a bot IS a profile + CLI handoff" mechanism is, as the task notes, not new —
it has always been scriptable. HSCC's own `hscc-roles` and fork `fix/kanban-worker-session-end`
already rely on the same primitives.)*

---

## Item 3 — Should flightdeck (or hscc) manage profile creation? → Mostly already solved; one small real gap.

The user's idea: "put flightdeck to setup profiles and stuff aka bots." Evaluated against
what both tools actually do today:

**Flightdeck** (`~/dev/flightdeck`, v0.6.0) is a **read-only project-control / mission-
control tool** — standup digests, kanban board summaries, project registry, Telegram
topics, QA queues. It talks to boards/git/Telegram. It has **no profile-management
command** and does not belong in the profile-creation path: it is a *consumer* of the
fleet's state, not a *producer* of agent identities. Routing profile creation through
flightdeck would be the wrong layer.

**HSCC already has a mature, native, headless profile-creation mechanism** — the role
framework (`hscc-roles/`):

- `hscc-roles create <name> "<desc>"` — author a role spec (`author.py`,
  writes `hscc-roles/roles/<name>.yaml` from a name + description, auto-generates identity
  + routing_description).
- `hscc-roles generate` — materialize/build every spec into a Hermes profile via the
  **native profile API** (`hermes_cli.profiles.create_profile` / `write_profile_meta`),
  composing a layered SOUL + HSCC cluster config (model tier, compaction routing, toolset
  boundary). This is precisely the backend that Bot Mode's "New Agent" rides via the
  `profiles.*` RPCs — HSCC does it headless, and it predates Bot Mode.
- `hscc-roles list` / `validate` — inventory and spec checks.
- Shipped: 22+ role profiles already under `~/.hermes/profiles/`; README documents the
  flow and explicitly says new roles are "minted (by a human or the orchestrator) with
  `create`" (README.md:163). Roadmap marks it shipped since v1.0.0 (ROADMAP.md:54).

So the *profile-creation primitive* the user wants flightdeck to handle is **not missing** —
HSCC has it. The genuine (small) gaps, and the only real candidates for follow-up work:

1. **Command surface confusion.** `hscc profiles` (the unified CLI) only shows
   *profile-status* (running kanban task counts). Real role/profile creation lives in the
   sibling **`hscc-roles`** plugin (`create`/`generate`/`list`), which is a different entry
   point and easy to miss. A low-effort, honest improvement would be to **surface role
   authoring/generation under the unified `hscc` CLI** (e.g. `hscc role create` / `hscc role
   list` / `hscc role generate`) rather than teaching flightdeck (a state *consumer*) to
   create profiles. This is ergonomics, not new capability.
2. **No evidence of real friction today.** I searched this week's sessions; profile/role
   creation did **not** come up as a manual, awkward step. The incubation/managing of roles
   is handled by the `hscc-roles` CLI and the orchestrator. So there is no *compelling*
   friction driving this — it would be a convenience, not a fix.

The `profiles.*` gateway RPCs in v2026.8.16.2 are a desktop-facing surface for these exact
primitives; adopting them headless would just **re-implement** what `hscc-roles` already
does with better cluster awareness (model tier, compaction, toolset caps). No reason to.

**Verdict on item 3:** Not a real gap worth a dedicated profile-management feature in
flightdeck. At most, an optional low-priority ergonomics card to unify role authoring under
the `hscc` CLI. I would **not** start a separate `flightdeck profile` subsystem.

---

## Item 4 — The actually-valuable parts of v2026.8.16.2 → prioritize these in PR #19.

The release window (`v2026.8.16 → v2026.8.16.2`, ~125 PRs) contains fixes that map
**directly** onto real incidents from this past week. These are more valuable to HSCC than
Bot Mode by a wide margin, and belong in the PR #19 cluster-verification checklist.

**A. kanban worktree / dispatch fix — directly fixes this week's dead-task/worktree incident.**

- `fix(kanban): reap worktree workspaces at task completion and archive` — Kanban worktree
  workspaces were **never removed by anything**: `_cleanup_workspace` didn't handle worktrees
  and `hermes kanban gc` only swept scratch, so every worktree task leaked its worktree. The
  fix adds `_cleanup_worktree_workspace` (with dirty/unpushed-commit predicates), a deferred
  worktree-parent reaper, and a `gc` backstop. This is the same class of "worktree/dispatch
  anchoring" problem that produced the **6 consecutive dead-task runs** this week (worker
  pinned to `t_389041ed`, whose record was lost in an incident and respawned as `t_1bd666ab`,
  with no terminal board call possible — my memory notes the root cause as the dispatcher
  never retiring the dead task record).
- `fix(kanban): kill tmux worker before removing its worktree cwd` and
  `fix(kanban): drop --force from worktree remove so git re-verifies dirtiness (TOCTOU)` —
  worktree teardown correctness fixes in the same family.

**B. session-handoff data-loss fix — directly maps to this week's data-loss/session context issue.**

- `fix: prevent handoff leg data loss + surface state.db corruption to users` — fixes two
  bugs: the `/handoff` CLI→gateway race (#88234, where a completed handoff leg vanished from
  session history because the CLI was still writing to the session DB when the handoff
  completed) and surfacing state.db corruption. This is exactly the "data-loss incident from
  a related kanban_db bug" and the "worker session/context issues" from this week. HSCC's own
  fork branch `fix/kanban-worker-session-end` (in `~/dev/hermes-prfix`) is a *sibling* effort
  of the same class — deterministic memory session-end on hard `os._exit` paths for `-Q`
  kanban workers. Relevant to PR #19 verification: check whether upstream's built-in handoff
  fix and HSCC's fork session-end fix compose cleanly after the bump.

**C. cron scheduler self-heal — relevant to dispatcher/cron reliability.**

- `fix(cron): persisted-state recovery re-arms recurring job stuck in stale last_status=error`
  and `fix: re-arm wedged cron jobs to next legal occurrence, cache cadence` — a stale-error
  recurring job now re-arms to its next *legal* occurrence (respecting the CRON expression),
  and a job stuck wedged is force-re-armed. This is the "cron scheduler self-heal
  (stale-claim reconciliation, wedged-job re-arm)" in the release notes.
- `fix(cron): stop retry storms when the gateway is deliberately stopped` — prevents the
  dashboard-fire/cron retry storm on a deliberately-stopped gateway.

**D. MCP 2.x SDK migration + stateless protocol, subprocess Python runtime ownership
hardening (PYTHONHOME/PYTHONPATH isolation)** — relevant to the worker context issues and the
general reliability of the upgraded runtime. Worth exercising in the cluster verification.

**Not relevant to HSCC specifically:** the `profiles.*`/`image.generate` RPCs (desktop-facing),
the Bot Chat protocol injection, Cua Driver 0.20 contracts, /worktree + /rollback hand-edit
preservation (single-user CLI conveniences), Gemini tool-call ID preservation.

### Concrete recommendation for PR #19 verification (do not duplicate the checklist — this is just what to focus on)

When the PR #19 (hermes-agent v2026.7.30 → v2026.8.16.2, sparkrun v0.3.1 → v0.3.4) card is
run, weight the verification toward:
1. **Worktree lifecycle** — after this bump, confirm a completed/archived task actually reaps
   its worktree (no leftover `.worktrees/`), which is the exact failure this week's dead-task
   incident rode on.
2. **Session handoff / memory-session-end** — confirm `/handoff` and the `-Q` kanban-worker
   memory flush (HSCC's `fix/kanban-worker-session-end` in `~/dev/hermes-prfix`) still work
   after the bump; the upstream handoff data-loss fix and the fork session-end fix touch
   adjacent code.
3. **Cron self-heal** — a deliberately-stuck recurring job now re-arms and a stopped-gateway
   retry storm no longer fires.
4. **MCP 2.x migration** — confirm the telegram MCP bridge and any MCP tools still load.
5. **sparkrun 0.3.4** — this is a Beta→Stable graduation (PRs #253/#254), low risk, but the
   bump should be exercised via the normal cluster-verification steps.

---

## Summary

- **Bot Mode UI plugin: do nothing.** Not applicable to a headless deployment; no desktop app
  exists or is used anywhere in the fleet (verified via processes, plugin dir, channel
  directory, and /Applications). Installing it would be a no-op or pointless.
- **Group-chat protocol: do nothing.** Kanban dispatch already covers coordination with a
  strictly better durable/reviewable model.
- **Profile creation: mostly a solved problem** (HSCC role framework). At most an optional,
  low-priority ergonomics card to unify role authoring under `hscc` — not a new flightdeck
  subsystem.
- **The real value of v2026.8.16.2 is the kanban worktree / cron self-heal / session-handoff
  data-loss fixes.** These map to real incidents from this week. Prioritize them in the
  PR #19 cluster verification (which already exists as a separate card — do not duplicate).
