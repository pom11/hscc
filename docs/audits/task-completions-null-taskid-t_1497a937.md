# t_1497a937 — task_completions.jsonl logs task_id=null: findings

Status: IN PROGRESS. Root cause established from evidence; fix staged below.

## Ledger evidence (read-only, `~/.hscc/task_completions.jsonl`, 851 rows)

```
completions: 851 rows, 799 null-task_id
blocked    : 894 rows, 799 null-task_id
null-task_id rows that carry ANY other field: 0
null completions paired to a null blocked row within 500 ms: 799/799
real completions: 52   (distinct task ids: 52)
```

Three facts pin the cause:

1. **Every single null row has all four fields null** (task_id, profile_name,
   summary/reason, board). A kwarg-shape drift where hermes renamed or nested
   one field would drop *that* field, not all four while leaving a real
   `completed_at`.
2. **799 of 799 null completion rows have a matching null blocked row within
   500 ms** (25–60 ms apart, blocked first). Real task traffic does not block
   and complete the same nothing 799 times in a row 30 ms apart; two functions
   called back-to-back in one pytest session do.
3. **Null-row volume tracks suite runs, not completions**: 2026-11-01 = 73 null
   rows vs 1 real; 2026-10-31 = 41 vs 0.

The two writers are the noop tests in `hscc-cluster/tests/test_lifecycle_hooks.py`:

```
:69  def test_blocked_handler_noop_without_context(self):
:71          result = workflow.on_kanban_task_blocked()      # <- no args, NO path patch
:100 def test_completed_handler_noop_without_context(self):
:102         result = workflow.on_kanban_task_completed()   # <- no args, NO path patch
```

Every sibling test in that file patches `workflow._COMPLETION_LOG` /
`workflow._BLOCKED_LOG` to `tmp_path`. These two do not, so each run of
`scripts/run_tests.sh` appends two all-null rows to the operator's LIVE
ledgers. That is the 797-799-row null population, and it is why the fleet
check reads garbage: `hscc_daemon/stats.py:61` counts any row with a parseable
`completed_at` — over the last 7 days that is **200 junk rows against 2 real
ones, a 99% inflation of `completions.total`**.

## The card's premise does not reproduce against the installed hermes

Installed: `Hermes Agent v0.21.5 (2026.9.24) · local 88dcabb9`.

Fire site `hermes_cli/kanban_db.py`:

```
:157  invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
:164  _fire_kanban_lifecycle_hook(event, task_id, board=get_current_board(),
          assignee=..., run_id=run_id, **fields)
:2838 _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
```

Payload keys today: `task_id, profile_name, board, assignee, run_id, summary,
telemetry_schema_version`. Handler `hscc-cluster/workflow.py:381` declares
`on_kanban_task_completed(task_id=None, profile_name=None, summary=None,
board=None, **kwargs)`. Because it carries `**kwargs`,
`plugins_dispatch._hook_callback_kwargs` (:190) hands it the **whole** payload,
so `task_id` binds. The shape the handler expects is the shape hermes sends.

The 52 real rows confirm it from the other side: all 52 carry a real task_id,
a board and a profile — `t_dc2cf015` as recently as 2026-11-01T13:54:52.
**When the hook actually fires, task_id lands.**

## Open question carried forward

Real cards completed today (t_51931733, t_04c35b37, t_c697278f) produced **no
ledger row at all** — neither real nor null. That is a *missing* write, not a
null write, and is a different defect (hook not firing in the completing
process) from the kwarg-shape drift the card assumes. Timestamp cross-check in
`forensic_recent.py`; board-side `completed_at` comparison pending.

## Fix shipped in this card

1. `hscc-cluster/tests/conftest.py` — autouse `_pin_lifecycle_logs_to_tmp`
   pins both module log paths to `tmp_path` for every test in the suite, same
   doctrine as the existing `_pin_serving_json_to_tmp` /
   `_pin_expected_up_marker_to_tmp` fences. The leak becomes impossible by
   construction, not by each test remembering.
2. `hscc-cluster/workflow.py` — both handlers now normalise identity fields
   before writing: a genuinely absent task_id logs `"unknown"`, never a silent
   null, and an entry with no task identity is **not appended at all** (that is
   what made 799 rows unattributable *and* un-deletable-by-join).
3. `hscc-cluster/tests/test_lifecycle_hooks.py` — fake-payload tests pinning
   (a) the shape hermes ships today, incl. the extra `assignee`/`run_id`/
   `telemetry_schema_version` kwargs and a narrow signature, (b) the
   field-absent fallback → `"unknown"`, and (c) a live-leak guard.
4. Ledger reader audit (below) — readers are null-tolerant; no format change,
   so no reader migration is needed.

## Second, distinct defect found while verifying (NOT fixed here — see LEFT)

The card assumes real completions DO write a row, just with a null id. They do
not write a row at all. Cards completed today by this profile — `t_51931733`
(02:02:32Z), `t_04c35b37` (02:00:09Z), `t_c697278f` (01:10:02Z) — have **no**
entry in `task_completions.jsonl`; the only rows in that window are the null
pairs at 01:25:57 / 01:48:09 / 01:55:00 written by suite runs.

Evidence that the hook is not firing for worker profiles at all:

| signal | value |
| --- | --- |
| `hermes plugins list` (active profile = `backend-engineer`) | `hscc-cluster … not enabled` |
| `~/.hermes/profiles/backend-engineer/config.yaml` | no `plugins:` key at all |
| `~/.hermes/config.yaml:561-567` | `plugins.enabled: [hscc-cluster, hscc-commands, sparkrun-hermes]` |
| `tool_events.jsonl` last LIVE row (written by the same plugin's `on_pre_tool_call`) | `2026-10-06T11:08:33`, `profile_name: "unknown"` |
| `task_completions.jsonl` last real row | `t_dc2cf015`, `profile_name: "default"`, 2026-11-01 |
| profile names ever recorded on real rows | `default` ×50, `reviewer` ×1, `backend-engineer` ×1 |

So `hscc-cluster` is enabled only where the config resolves to the global
`plugins.enabled` list; the worker profiles do not enable it, and every hook it
registers — `kanban_task_completed` included — is therefore never subscribed in
a worker process. The last row the plugin itself wrote from a live session is
2026-10-06; everything since in its ledgers is test traffic. That is why the
fleet check has "no attributable completions": not a null task_id, but no write.

**Deliberately not fixed in this card.** Enabling a plugin per profile is an
operator/deployment decision with blast radius beyond observability (it also
registers the guarded cluster toolsets, `pre_tool_call` guards, and slash
commands — `hscc-cluster` is documented as "Orchestrator-only toolset"). Flipping
it fleet-wide from a worker card would be an unrequested cluster-config change.
Filed as a follow-up; the fix belongs in `hscc-bootstrap`'s deploy step or the
operator's profile configs, not here.

## Third, smaller defect spotted in the same pass (NOT fixed here)

`on_pre_tool_call` (workflow.py:464) reads `kwargs.get("profile_name",
"unknown")`, but hermes' `pre_tool_call` fire site
(`hermes_cli/plugins.py:1871`) passes
`tool_name, args, task_id, session_id, tool_call_id, turn_id, api_request_id,
middleware_trace` — **no `profile_name`**. Every live row in
`tool_events.jsonl` therefore carries `"profile_name": "unknown"`, and
`stats.py`'s `tool_calls_by_profile` has exactly one bucket. One-line fix: fall
back to `hermes_cli.profiles.get_active_profile_name()` the way
`kanban_db._hook_profile_name()` does, or read it off the session key.

## Reader audit (done before changing anything)

- `hscc_daemon/stats.py:61` — `record.get("profile_name")`, skips None. Null-safe.
- `hscc-cluster/workflow.py:436` (auto-unblock scan) — `blocked_entry.get("task_id")`
  then `if blocked_tid and ...`: nulls are skipped, which is why the 797 null
  rows silently disabled auto-unblock for every real completion.
- `hscc-api/routes_kanban.py` / `hscc_daemon/kanban_blocked.py` — read the
  kanban DB, not these ledgers. No dependency on the ledger format.

No reader requires `task_id: null`; all read via `.get()`. Recording
`"unknown"` instead of null, and dropping identity-less rows, is safe.

## Deliberate non-actions

- **Did not delete or rewrite `~/.hscc/task_completions.jsonl`** — runtime data,
  read-only per the card. The 797 junk rows stay; anything downstream can now
  filter them (`task_id` is null or missing in exactly the rows that carry no
  identity). Removing them is a one-liner for the operator if wanted:
  `python3 scripts/ledger_scrub.py`-style filter kept in this repo, not run here.
