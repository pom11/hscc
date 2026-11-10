# MON-ENV-PIN's fork half — the wedge gate's CLI probes (t_8616f2a5)

Follow-up to `board-pin-t_b014c2b8.md` §5c, which named this residual and left it
deliberately: it is the only board read in this repo that
`hscc_daemon/board_pin.py` **cannot** guard, because the read is performed by
another process.

## 1. Why the in-process guard stops at the fork

| site | what it did |
|---|---|
| `dispatcher_wedge._default_gate_cli_runner` | `subprocess.run(argv, capture_output=True, text=True, timeout=20)` — **inherited** environment, no `env=` |
| `dispatcher_wedge._gate_cli_json` | parsed the child's stdout as fact |
| `_board_ready_facts` → `hermes kanban list --status ready --json` | the ready list, per spawnable board |
| `_card_hold_from_cli` → `hermes kanban show <id> --json` | the hold-window evidence, per card |

`board_pin.open_board` removes `HERMES_KANBAN_DB` from `os.environ` for the
duration of *our own* `connect_closing`, then restores it. That is invisible to a
child two ways: a forked process carries a **copy** of the environment taken
before the restore, and `subprocess.run` only forwards a child environment that
is passed explicitly. So the guard was correct, and irrelevant, at the same time.

The precedence being defeated is the one measured in the parent audit
(`hermes_cli/kanban_db.py:540-555` `_board_path()` +
`_explicit_board_intent_pinned()` → `agent/delegation_context.py:76-93`): with
`HERMES_KANBAN_DB` set **and** either fence marker present, the pin wins over the
explicit `--board`. A dispatched worker (`HERMES_KANBAN_TASK`) or a delegated
child (`HERMES_DELEGATED_CHILD_CONTEXT`) running the daemon path is exactly such
a process. `scripts/run_tests.sh` used to unset `HERMES_DELEGATED_CHILD_CONTEXT` but **not**
`HERMES_KANBAN_DB`, so no canonical leg ever ran the gate in the shape — the
harness gap `t_7e343e04` closes (PR #272, landed while this card was running: the
runner now unsets the board-location pins too, and deliberately leaves the
worker *identity* vars alone). That is precisely why the cases here arm or clear
the worker state explicitly instead of trusting the ambient environment: they pass
inside a worker shell and outside one, and the harness scrubbing the pin cannot
quiet them.

## 2. What the gate got wrong — this is not cosmetic

The gate's verdict decides whether `recover_dispatcher_wedge` restarts the
gateway. Both probes resolve through the child's environment, so under a
conflicting pin they answered **consistently for the pinned board P** while the
verdict sentence named the board we asked about, B:

* `"board 'B': all N ready cards explainably held [t_x: respawn_guarded:active_pr]"`
  — built entirely from P's queue. B's plain, unheld ready card is never seen, so
  a **real wedge is explained away** and the restart that would have cleared it is
  suppressed. That is the exact silent no-dispatch failure this watchdog exists to
  surface.
* The card id in that sentence lives on P. Task ids are per-board (parent audit
  §2c), so the hold evidence is attributed to a card that does not exist on B.
* The mismatch direction is not stable: when P's ready queue is empty the gate
  says `"probe saw spawnable work, CLI listed none"` and the restart proceeds —
  because the SQL probe (guarded by t_b014c2b8/t_147201fa) saw B while the CLI
  saw P. Two halves of one gate comparing two different files for one board.

## 3. Chosen shape — card option (a), with option (b) where (a) is impossible

Detection and wording are `board_pin`'s, so one seam owns the precedence question;
only the *action* differs, because the action has to cross a process boundary.

| state of the world | what the probe is handed | what the gate says |
|---|---|---|
| no pin / no fence / pin already **is** B / lib cannot be interrogated | nothing — `cli_fn(argv)`, child inherits (byte-identical to before) | unchanged |
| pin would outrank `--board`, runner accepts `env` (the real `_default_gate_cli_runner`, and any runner written against the grown contract) | a copy of `os.environ` **minus `HERMES_KANBAN_DB`** | warns once per (pin, board) in `board_pin`'s `read_warning()` wording, then explains normally |
| pin would outrank `--board`, runner is the legacy `runner(argv)` shape and cannot be scrubbed for | **never called** | `board '<B>': board state unverifiable …` — the existing fail-closed path, so the restart proceeds, with the pin named in the log line |

Three properties worth keeping:

* **Nothing is refused when the pin is the honest answer.** A worker whose pin
  *is* the board being asked about, and every probe that expressed no board
  intent, keep the inherited environment and say nothing — the same posture as
  `board_pin`'s reads, and pinned by companion tests.
* **Refusing is not a third behaviour invented here.** `blocked` returns through
  `_board_ready_facts`' existing `(False, why)` fail-closed channel, so no caller
  or test sees a new shape; the gate's documented "anything we cannot verify
  leaves the restart in place" holds.
* **The scrub is a copy, never a mutation.** `os.environ` keeps the pin, so the
  worker fence hermes intends is not weakened in *our* process — only the child's
  view of one variable is narrowed, for a read-only probe.

`_cli_runner_takes_env` decides the second/third row by signature, with a
`cli_env_capable` escape hatch so the `_GateCliCaller` wrapper can state what it
actually forwards instead of its own signature implying a capability it withheld.
That matters because the alternative — forwarding blindly and letting
`_gate_cli_json`'s blanket `except` swallow the `TypeError` — is the same
fail-closed outcome but with no line naming the pin, i.e. a wedge explained by an
unreadable board and nobody able to say why.

## 4. What this does NOT fix (deliberate, and where it is owned)

* **`--board` still does not reach the child for `default`.** `list_argv` /
  `show_argv` keep `["--board", board] if board and board != "default"` — owned by
  `t_dad0da32`, whose direction is settled by §5b of the parent audit (`default`
  is a real board). Consequence, stated precisely so nobody re-derives it: with
  this card landed and that one not, a scrubbed `default` probe in a worker shell
  resolves hermes' *current* board (`HERMES_KANBAN_BOARD`) instead of the pinned
  file. Those coincide in every dispatcher-spawned shell (the dispatcher sets both
  for one board), and the daemon has neither variable, which is why the shape went
  unseen. It is still not the flat legacy `default` DB the guarded SQL probes read;
  `t_dad0da32` closes that.
* **`hscc_daemon/kanban_pause.py`** has the same fork hazard at
  `_run_hermes` (`:147-149`): `list_cards` reads `--board <b> list --json` and
  `_hold` **writes** `reclaim` / `block` under it (`:173-187`). Its read is the
  same misdirection; its write is the class `board_pin` §2c refuses outright —
  scrubbing that environment would be the wrong fix. Carded as **t_70c97264**.
* Hermes' precedence is untouched, as in every card of this series: no resolver
  patch, nothing under `~/.hermes/**`. The upstream ask stays the (unfiled) draft
  in the parent audit §4.

## 5. Evidence

`hscc_daemon/tests/test_board_pin_gate_cli.py` — 10 tests, each conflict case
paired with a non-vacuity assertion, all assertions on observables (the
environment the CLI was handed, the argv, the verdict text, whether the probe ran
at all) rather than internal flags:

* non-vacuity first: the pre-change call (`_gate_cli_json(cli, argv)`, same armed
  env) answers `t_alpha_1` for a question about `beta`, and `show` for `beta`'s
  card finds no hold — i.e. the armed fixture really does pin the child;
* the acceptance case driven through `_board_ready_facts` **and** the whole gate:
  every child env has no `HERMES_KANBAN_DB`, keeps every other key (a marker
  variable proves the scrub is one key, not an empty world), argv byte-identical,
  evidence names `t_beta_1`, the warning names pin + board, `os.environ` keeps the
  pin;
* option (b) observable: a legacy runner records **zero** calls and the verdict
  says unverifiable — paired with the same runner explaining fine when nothing is
  pinned, so the refusal cannot be caused by the fake;
* companions: no pin → `cli_fn(argv)` with no `env` at all and the exact argv
  bytes; pin == asked board → nothing handed over, nothing said; no `board=` →
  left to the pin;
* the real runner, with `subprocess.run` stubbed and its **kwargs** inspected —
  the injected-fake cases prove the gate decides the env, this one proves the
  production runner passes it, which is the half that actually reaches hermes.

Mutation probes (8 decision points, control run unmutated and green, provenance
asserted to the copy in every run): **8/8 caught** — never-scrub (5 tests),
scrub-but-keep-the-pin (4), real runner drops `env=` (1), gate ignores
`unverifiable` (1), `show` inherits instead of scrubbing (4), capability always
"cannot" (3), capability always "can" (1), wrapper never forwards (3).

Suite measurements (both at a green tree, `env -u HERMES_DELEGATED_CHILD_CONTEXT
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -rs <leg>/tests -p
no:cacheprovider`):

* pre-merge, at `c1c628a1` — `hscc_daemon/tests`: 3591 passed, 2 skipped in 80 s.
  The 2 skips were the then-documented `t_2be3b04e` `hermes_cli`-coherence skips in
  `test_board_pin.py`, present on `main` too;
* post-merge (origin/main `a2e23c9d`, which brought #271, #272 and the t_2be3b04e
  per-test `hermes_cli` isolation), at `9f179571` — `hscc_daemon/tests`: **3610
  passed, 0 skipped** in 108 s; `hscc-api/tests`: **994 passed, 1 skipped** in
  252 s;
* the four files this seam shares (new file + `test_board_pin.py` + both wedge
  suites) went 146 → 156 pre-merge, and 225 with the parent's wedge probe family
  and `test_kill_switch.py` in the base.

`scripts/changelog_fragments.py check` OK; `scripts/readme_lint.py` 0 findings,
docs-index 202/202.
