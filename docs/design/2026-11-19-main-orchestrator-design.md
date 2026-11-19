# Main orchestrator — design

Date: 2026-11-19. Status: draft for operator approval. Builder: OpenCode cards.

## Intent

The operator manages the cluster today through an outside assistant: it watches
every project orchestrator, answers "what is happening on the cluster", steers
stuck orchestrators, reviews and merges PRs, and cuts releases. hscc should ship
that role itself. A fresh `hscc-bootstrap` creates **one main orchestrator**: a
Hermes profile that knows every project and the whole cluster, holds authority
over the cluster, wakes itself up on a schedule, talks to the other
orchestrators through the hscc daemon, and pings the operator's phone and Mac
through the hscc apps when it needs them.

This was the original plan (a default `general` board plus a `general`
orchestrator). What exists today never got used and is split in two:

- `general-orch` (from `orch-all`): a plain project orchestrator on the
  `default` board, explicitly *without* cluster authority. 0 sessions.
- `orchestrator` (role profile): holds cluster authority, no board of its own.
- The machine-wide kanban current pointer is whatever was last switched to
  (today `headless-display`), so cards created without `board=` land on a
  random project board.

## Goals

1. **Named at bootstrap.** The interview asks
   `Main orchestrator name [general]:`; `--yes` takes `general`. The answer is
   stored once in hscc state and every component reads it from there.
2. **One profile, merged.** The main orchestrator replaces both `general-orch`
   and `orchestrator`: their board duties + cluster authority in one SOUL.
3. **Its own board, and it is the default.** A board with the same name is
   created and set as the kanban current board. Cards created without a board
   land there, never on a project board. A doctor check flags drift.
4. **Knows everything.** Its SOUL/skills cover: every registry project and its
   orchestrator + board, cluster topology and serving, hscc settings, the
   release recipe, review rules, the post-bootstrap checklist. Its memory holds
   a per-project state note it keeps current.
5. **Answers live.** Asked "what is happening", it checks live state (boards,
   orchestrator panes, PRs, nodes, proxy, daemon) before answering — never from
   memory alone. Short, plain answers.
6. **Always watching.** It keeps standing watch tasks open on its board (one per
   active orchestrator/project with work in flight) and works them on each
   wake-up: stuck or failing cards, wedged panes, drift into meta/side cards,
   duplicate cards, cards completed while their PR is not on main.
7. **Wakes itself.** Bootstrap installs default wake-ups (editable):
   - cluster health — every 2 h (nodes, serving, proxy errors, daemon, autodown
     state, kanban caps)
   - orchestrator sweep — every 3 h (the watch tasks above)
   - project memory refresh — daily
   - own settings / skills review — weekly
   Wake-ups are delivered into the **same running session** the operator talks
   to (so its context and memory stay one), queued when it is busy.
8. **Talks to orchestrators via the daemon only.** Uses `hscc orch inject`
   (`--mode auto|steer|queue|plain`) and the board tools — no `~/bin` scripts.
9. **Authority: everything except LIVE publish.** On its own it may: start /
   stop / restart models, pause/resume boards, create/steer/block/complete
   cards on any board, steer/interrupt/restart any orchestrator, review and
   merge PRs (gates green + its own review), change hscc/cluster settings, run
   node recovery, and do **releases end to end** (changelog fold, tag, GitHub
   release, tap bump, app builds, post-release upgrade + bootstrap of the
   gateway's hscc CLI with the post-bootstrap checks).
   **Only a LIVE publish (production deploys of a project, e.g. a BC LIVE
   publish) asks the operator first.**
10. **Reaches the operator by push.** iOS: Apple Push Notification service, the
    phone buzzes within seconds even with the app closed. macOS: the menu bar
    app shows a notification within seconds. Alert only — the operator answers
    in the main orchestrator's pane / chat, not from the notification.
    It notifies for: a LIVE publish waiting for approval; something broken it
    could not fix; a release it shipped (FYI, low priority). Deduplicated and
    rate-limited — the same condition does not re-alert until it changes.

## Non-goals

- Approve/deny or reply from the notification.
- Replacing the per-project orchestrators; they keep their boards. The main
  orchestrator watches and steers them, it does not do their project work.
- Telegram or any other chat channel.

## Anti-goals (fails even if it "works")

- It does project work itself instead of routing it to cards.
- Notification spam: alerts for routine card moves, repeated alerts for the same
  condition.
- An existing install loses data or breaks: profiles, memories or sessions of
  `general-orch` / `orchestrator` deleted, or something that refers to those
  names stops working.
- It breaks the standing rules: autodown re-enabled without the operator,
  built apps installed on the gateway Mac, private data (LAN addresses, tenant
  ids, secrets, device ids) in the public repo, attribution lines in commits/PRs,
  destructive git or `rm -r` without the operator, non-English dispatch to
  orchestrators.

## Constraints

- pom11/hscc is public: the APNs key, team id, device tokens and the chosen
  name's runtime state live outside the repo (keychain / `~/.hscc`), never in
  tracked files; fixtures use placeholder addresses.
- Fleet access only through sparkrun/hscc, never raw docker/ssh.
- Idempotent bootstrap: re-running keeps the existing name (asks only when none
  is stored), never clobbers memories/sessions, never reverts operator-set
  values (caps 12/3, compression thresholds, autodown).
- iOS app is ad hoc signed (bundle prefix `ro.parsec.hscc`); macOS is Developer
  ID + notarized. Gates stay compile-only; nothing is installed on the gateway.
- One-time operator steps (2FA / Apple portal) are allowed: create the APNs
  auth key (.p8) and enable Push Notifications on the app ids. Everything else
  is automated.

## Decided by the operator

- One merged main orchestrator; name asked at bootstrap, default `general`.
- Authority: everything, including reviews, merges and releases; only LIVE
  publish asks.
- Reach: push to the hscc iOS and macOS apps, within seconds; alert only.
- Built through OpenCode cards.

## My calls (operator can override)

- Profile name = the name typed (`general`), board slug = same name. Name must
  be a slug and must not collide with a role or project profile.
- Migration on an existing install: create the main profile, carry over
  `general-orch`'s memory/sessions if any, keep `general-orch` and
  `orchestrator` on disk but retired (SOUL says "retired, use <name>"), and
  route every code reference (`GENERAL_PROFILE`, `GENERAL_BOARD`, the
  cluster-authority role lookup, `/orch-restart`, API orchestrator routes) to
  one resolver that returns the stored main name. Nothing deleted.
- The `default` board keeps its history; it simply stops being the default.
- Bootstrap (or `hscc main start`) ensures the main orchestrator runs in its own
  tmux pane that `hscc orch inject` resolves.
- Wake-ups are owned by the hscc daemon (config under `~/.hscc`, listed and
  edited with an `hscc main wakeups` verb) and delivered through the existing
  inject path; the builder may use `hermes cron` instead only if its runs land
  in that same running session.
- Standing watch tasks are cards on its board parked so the dispatcher never
  hands them to a worker (e.g. blocked with a `watch` reason); the sweep
  comments on them and closes them when the watched work is merged.
- Push: one alert feed in the API (`hscc notify` / `POST /v1/alerts`, `GET
  /v1/alerts`), device registration (`POST /v1/devices`), APNs HTTP/2
  token-auth sender in the daemon. The Mac menu bar app may poll the feed every
  few seconds instead of APNs if push entitlements for a Developer ID app prove
  impractical; the phone must use APNs.

## Left to the builder

Exact module layout, state file format, SOUL wording, CLI verb names beyond the
ones above, card split. Work goes as small single-purpose OpenCode cards, each
with its own PR, gates green, no attribution, address guard clean. Suggested
order: (1) name in interview + state + resolver, (2) merged SOUL + migration,
(3) board + current pointer + doctor check, (4) pane start, (5) wake-ups,
(6) alert feed + `hscc notify`, (7) APNs sender + device registration,
(8) iOS push, (9) macOS notifications, (10) docs + bootstrap tests end to end.
