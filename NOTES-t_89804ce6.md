# t_89804ce6 — Rich CLIPS (not folds) in the machine-path rows

Base: `wt/t_89804ce6` on `origin/main` @ `bf6acda2` (merged into this branch).
Harness: real pytest per case at a chosen tmp-root length, with a plugin
(`richtee`) that tees the **verbatim** Rich render to a log.

## THE HARNESS IS THE FINDING THAT REFRAMES THIS CARD

The parent's clip verdicts were read out of pytest `E` lines. pytest
middle-elides a long repr inside an `E` line, so an ellipsis there is
**pytest's**, not Rich's. Every one of this card's "clipped mid-token with `...`"
descriptions is that artifact. Reading the render itself (tee) instead:

| case (real node id) | what the render actually does | verdict |
|---|---|---|
| `hscc-project/tests/test_project.py::test_list_shows_columns_and_health` | **CLIP** — `…` in the REPO cell, four columns collapsed to width 0 | **view bug** |
| `hscc-project/tests/test_project.py::TestNoAnsiProject::test_list_human` | same render, same cause | **view bug** |
| `hscc-project/tests/test_markup_escape.py::test_project_list_rows_survive_markup` | same render, same cause | **view bug** |
| `hscc-project/tests/test_markup_escape.py::test_roadmap_add_path_and_heading_survive` | **FOLD** — `repo[` ⏎ `/bold]/ROADMAP.md` | not a clip → `_says` |
| `hscc-project/tests/test_markup_escape.py::test_roadmap_adopt_backup_path_survives` | **FOLD** — `…/repoa` ⏎ `[bold]b[/bold]c/ROADMAP.md.bak` | not a clip → `_says` |
| `hscc-project/tests/test_map_sessions.py::test_cli_run_reports_real_totals` | **FOLD** — `out/m` ⏎ `ap-sessions-…md` | not a clip → `_says` |
| `hscc_daemon/tests/test_api_launchd.py::test_cli_install_service_writes_unit_in_private_home` | **FOLD** — `…/Library/L` ⏎ `aunchAgents/com.hscc.api.plist` | not a clip → `_says` |
| `hscc_daemon/tests/test_autodown_cli.py::TestStatus::test_status_surfaces_unevaluable_kanban_interlock` | **FOLD** — `…/miss` ⏎ `ing'` | not a clip → `_says` |
| `hscc-cluster/tests/test_fleet.py::test_daemon_mod_does_not_pollute_sys_path_{on_success,on_import_failure}` | not a render at all — **sweep harness pollution** | not layout |
| `hscc_daemon/tests/test_node_pause_stop_intent_t_8ad06a53.py` (6 cases) | not a render — the sweep tripped the leg's **sparkrun fence** | not layout |

## FOUR OF THE CARD'S NINE CASES DO NOT EXIST

The card body is "MEASURED, DO NOT RE-DERIVE", but four of its node ids are not
in `main` and never were in the parent's own sweep data:

* `hscc-cluster/tests/test_topology_cli.py` — no such file (`ls` + `find`), and
  `test_topology_cli` appears **0** times in the parent's `runs.jsonl` (18,837
  recorded runs). Its two cases `test_doctor_shows_both` /
  `test_up_down_and_status_render` appear nowhere in the repo
  (`grep -rn "def test_doctor_shows_both\|def test_up_down_and_status"` → no match).
* `hscc_daemon/tests/test_api_dashboard.py` — no such file;
  `test_status_serves_html_with_badges` exists nowhere.
* `hscc-cluster/tests/test_fleet.py::test_cli_status_renders_table` /
  `test_cli_status_marks_stale_host` — not in that file. What the sweep really
  confirmed there is `test_daemon_mod_does_not_pollute_sys_path_{on_success,
  on_import_failure}` (parent's own `confirmed_all.json`).
* `hscc_daemon/tests/test_api_launchd.py::test_cli_install_service_writes_unit_in_home`
  — the real id ends `..._in_private_home`.

So the case list below is the parent's own `confirmed_all.json` (20 confirmed,
9 of them `hscc_daemon`/`hscc-cluster`), not the card body.

## ASK 1 — per-case verdict, render quoted

### The three CLIPs: one view, one cause — `project list`

`hscc-project/flightdeck/commands/project.py::cmd_list` builds
`NAME / REPO(no_wrap=True) / BOARD / TOPIC / HEALTH`. `no_wrap=True` makes the
REPO column's *minimum* width equal its full content length, so on a deep tmp
root Rich cannot shrink REPO and shrinks the four flexible columns to their
floor instead — then clips REPO anyway. Verbatim render, tmp root 190
(`tee.log`, `renders/hscc-project__test_list_shows_columns_and_health__190.txt`):

```
                         projects
┏┳━━━…━━━┳┳┳┓
┃┃ REPO  ┃┃┃┃
┡╇━━━…━━━╇╇╇┩
││ /private/tmp/s898/rd/ccc…ccc/test_list_shows_columns_and_he0/… ││││
└┴━━━…━━━┴┴┴┘
```

Four of five columns render as zero width and the path itself ends in Rich's
`…`. NAME (`sigma`), HEALTH (`ok`) and the header `HEALTH` were never printed —
so no normaliser may recover them and the parent was right not to convert them.
This is a **bug in the view**: `flightdeck project list` becomes unreadable
(except for one clipped path) at a deep path, while the row's own promise is
that health is shown.

### The five FOLDs — not clips, no view change

`roadmap add`, `roadmap adopt`, `map-sessions`, `api install-service`,
`autodown status` all print the path inside a Panel/console body that Rich
**folds** mid-token: every character is still on the page, split across two
lines, e.g. launchd at tmp root 190:

```
launchd unit written:
/private/tmp/s898/rd/ccc…ccc/test_cli_install_service_write0/Library/L
aunchAgents/com.hscc.api.plist
```

and autodown at 146:

```
│   kanban interlock: UNEVALUABLE — Hermes agent source not found at
│ '/private/tmp/…/test_status_surfaces_unevaluab0/miss
│ ing' — cannot import hermes_cli.kanban_db. …
```

A fold is what a fixed-width human view must do with a path longer than the
width: nothing is lost, and the card's own decision (`_says` for a mid-token
FOLD) recovers it. These stay raw-marker-anchored, `_says`-checked.

### The two non-layout classes

* `test_fleet.py::test_daemon_mod_does_not_pollute_sys_path_*` — red at EVERY
  length 53..212 because the parent's sweep ran pytest from
  `…/workspaces/t_f2382c78/hscc` and the assert is
  `assert <that dir> not in sys.path`. The sweep's own workspace is the value it
  asserts absent. Attribute: harness, not layout. Verified below under the
  canonical leg posture.
* `test_node_pause_stop_intent_t_8ad06a53.py` (6 cases, every length) — the
  parent's E lines are `SPARKRUN FENCE GUARD: a test/fixture tried to exec the
  operator's live sparkrun CLI`. That is the leg's autouse fence firing on the
  sweep's posture, exactly the hazard the card's DECISIONS block warns about.
  Attribute: sweep posture, not layout.

## ASK 2 — the view fix

`REPO` loses `no_wrap=True` and gains `overflow="fold"`: the path folds instead
of eating the table, the label columns keep their floor width and their content.
No console width is touched anywhere (`make_console` still pins 200 for a
non-tty), so this is not the parent's prohibited "widen the console to pass".

## ASK 3 / ASK 4 — status

* 0 asserts deleted, skipped, xfailed or broadened.
* …  (fill in below as the arms land)
