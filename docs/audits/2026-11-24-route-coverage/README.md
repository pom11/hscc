# t_b9808146 — route-coverage allowlist: findings before any row was written

Status: **blocked on a missing input** (§4). Everything below is measured, not
inferred. Nothing here changed a route, a payload or a handler.

Measured at base `f77e42b5ccaf77ebefc6f6aa2cbc60f99da668b4` (`origin/main`).
Hermetic: `HOME`/`HSCC_HOME`/`HERMES_HOME` redirected into a throwaway dir before
`import api_server`; `socket.socket` + `subprocess.Popen` replaced by a tripwire that
**records and refuses**. No fleet contact, no real `~/.hscc`, no port bound.

## 1. The gap, reproduced independently

The card says the gate measured 98 routes / 26 documented. My own parser, written from
scratch against the same base, gives:

| quantity | this card | `t_dcf684c9`'s number |
|---|---|---|
| registered HTTP routes | **97** | 97 |
| WS upgrade routes | **1** | 1 |
| `routes_*` modules | **24** | 24 |
| Endpoint-reference rows | **22** | 26 |
| registered routes named nowhere | **75** | 72 |
| of those, called by the iOS client | **29** | 29 |

The two counts that set this card's scope — **98 registered** and **29 iOS-facing** —
match the sibling exactly. The documented-rows count differs (22 vs 26) because the
sibling measured on a rebased branch carrying PR #714's Profiles rows plus #708/#709's
sections; `origin/main` has none of those.

Identity is decided the way the server decides it: Swift call sites *and* doc Path
cells are both run through `api_server.ROUTES` and reduced to the resolved
`(method, pattern, handler)`. That makes the parent card's two traps inoperative — a
documented `/v1/foo` cannot cover `/v1/foo/list`, and
`^/v1/profiles/(?P<name>[^/]+)$` cannot credit the wrong row, because credit goes to
the pattern the router actually picks.

## 2. Priority-1 list: the 20 iOS-facing **read** routes

`ios-app/Sources/HSCC/HSCCClient.swift` calls these; the Endpoint reference does not
name them. This is the F2 class. "style" is measured empirically (§3), not read off
the AST.

**enumerated — key set is the handler's own, measurable now (10):**

| pattern | handler | measured key set |
|---|---|---|
| `^/v1/activity/feed$` | `routes_activity.handle_activity_feed` | `{count, entries, profiles, running_count, speak}` |
| `^/v1/autodown/status$` | `routes_autodown.handle_autodown_status` | 24 keys — **degraded path, see §3.3** |
| `^/v1/commands$` | `routes_commands.handle_commands` | `{commands, speak}` |
| `^/v1/cron/list$` | `routes_cron.handle_cron_list` | `{speak}` — degraded, see §3.3 |
| `^/v1/daemon/history$` | `routes_history.handle_history` | `{count, events, exhausted, window_bytes}` |
| `^/v1/escalate$` | `routes_ops.handle_escalate` | `{count, escalations, speak}` |
| `^/v1/kanban/blocked$` | `routes_kanban.handle_kanban_blocked` | `{boards, count, errors, speak, tasks}` |
| `^/v1/kanban/stale$` | `routes_kanban.handle_kanban_stale` | `{boards, count, errors, older_than, speak, tasks}` |
| `^/v1/profiles/list$` | `routes_profiles.handle_list` | `{count, profiles, speak}` |
| `^/v1/projects$` | `routes_project.handle_projects` | `{count, projects, speak}` |

**splat — the key set lives behind the `_backing_*` seam (5):** `routes_ops.handle_daemon_status`,
`routes_ops.handle_profiles`, `routes_template.handle_template_list`,
`routes_template.handle_template_status`, `routes_ops.handle_triggers` — all of the
form `return 200, {**data, "speak": ...}`.

**needs a required parameter before it answers at all (5):** `routes_logs.handle_logs`
(`?source=daemon|api|worker`), `routes_sessions.handle_sessions_list` (`?profile=`),
`routes_project.handle_project_detail` (path name),
`routes_profile_editor.handle_profile_editor_get` (path profile),
`routes_orchestrator.handle_orchestrator_chat_job` (a live job id).

The other 9 iOS-facing undocumented routes are **mutating** (POST `/v1/cluster/up`,
`/v1/cluster/down`, `/v1/escalate`, `/v1/triggers/run`, `/v1/orchestrator/chat`,
`/v1/profile/editor/{profile}`, `/v1/kanban/blocked/{id}/recover`,
`/v1/sessions/{id}/compact`, `/v1/sessions/{id}/retire`) and are out of scope by this
card's own "read-only routes only" rule.

## 3. Finding: the card's measurement rule is unsound for 5 of the 20

The card says to carry a key set "measured by calling the handler with its `_backing_*`
seam stubbed". For **enumerated** handlers that is exactly right. For **splat**
handlers it measures the wrong thing. `routes_ops.handle_daemon_status`:

```python
return 200, {**data, "speak": _speak_daemon_status(data)}
```

Stubbing `_backing_daemon_status` to a probe and reading the result's keys returns the
**probe's** keys plus `speak`. My run proves it — all 5 splat routes came back
identically as `{probe_alpha, probe_beta, probe_gamma, speak}`, which is an artefact of
my stub, not a payload. Followed literally, the card's rule would have written five
rows of invented key names into the canonical reference and then pinned them in a
shape-checked table, so every future run re-measures the fiction.

**The rule needs a split, and the gate must record which side a route is on:**
- enumerated (10/20): measure at the handler, as the card says.
- splat (5/20): measure at the **`_backing_*` function** and say so in the row.
- parameterised (5/20): the row must carry the required parameter as part of the
  promise; a bare GET does not answer, so a bare key set is unmeasurable.

### 3.3 two of the "enumerated" sets were measured on a degraded path

`handle_cron/list` returned only `{speak}` — its backing raised and the handler fell
back to the speak-only shape. `handle_autodown_status` returned 24 keys but its
backing **tried to spawn** `gh pr list --repo pom11/hscc --state open --limit 1
--json number`; the tripwire **refused and recorded it** (nothing ran). Both key sets
are therefore measured on a degraded path and must be labelled as such, not presented
as the live shape. A doc row that published either as the route's promise would be
under-claiming `cron/list` and publishing an artefact for `autodown/status`.

## 4. Blocker — why no row could be written

Every mechanic in the card presupposes an artifact that does not exist:

> "delete its `UNDOCUMENTED_ROUTES` entry in the same commit … add the route to **that
> file's** shape-checked table"

`hscc-api/tests/test_api_reference_route_coverage.py` exists in **no ref** — not on
`origin/main`, no remote branch, no local branch or worktree:

```
$ git ls-remote origin 'refs/heads/*dcf684c9*'                     # no output
$ git log --all --diff-filter=A -- hscc-api/tests/test_api_reference_route_coverage.py
                                                                    # no output
```

`hscc-api/tests/test_profiles_routes_documented.py` — whose `_rows()` parser and
shape-checked table this card is told to extend — is likewise **not on `origin/main`**;
it exists only in open PR #714.

Its author `t_dcf684c9` is **still running** (run 2164, heartbeats every 60 s, last one
minutes before this was written) and has pushed no branch; its own comment says the
gate design "follow[s] in the next commit". So the allowlist I am ordered to delete
from, the table I am ordered to extend, and the section order I am told to respect are
all unwritten.

Writing the 20 rows now would mean inventing a second, parallel gate + allowlist +
shape table alongside the one the sibling is about to create, and editing the
`docs/API.md` Endpoint reference that PRs #708, #709, #714 and the sibling are all
editing at once — the exact collision this card's own "Hotspot: docs/API.md" warning
names, and it would leave two allowlists claiming one route set.

**Needed to unblock:** `t_dcf684c9`'s PR — the `UNDOCUMENTED_ROUTES` allowlist keyed by
`(method, resolved pattern)`, the shape-checked table, and the Endpoint-reference
section order. This card had **no board parent** despite being "spun out of" that task,
so nothing ordered it after the gate; a dependency edge was added by this run.

With the gate in hand, §2 + §3 are a complete spec for the 20 rows: the list, the
handler, the measured key set, and which rows must be measured at the backing or carry
a parameter so the table does not pin fiction.
