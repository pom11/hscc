# The kanban read surfaces read the operator's project registry (t_c11f0d88)

**Status: fixed on `wt/t_c11f0d88` → PR #553 (`6796f450`..`4f15c2dd`, base `e3d60c21`).**
Product rule unchanged. `leg hscc_daemon` is green on the runner at that head.

## The claim, and the number behind it

`t_4447d736` §11 took the alone-vs-in-leg sweep on the CI posture and the agreement
property held — but the leg underneath it was red on the runner, silently, because
`HSCC_TESTS_ENFORCE` is unset and `.github/scripts/leg_verdict.sh` turns a test failure
into a warning annotation. `pytest-legs.yml` run 44884690833 (head `2b044516`):
`1 failed, 4031 passed, 28 skipped in 204.91s`.

The one failure is `hscc_daemon/tests/test_kanban_cli.py::TestCmdKanban::test_listing_json_valid`,
`IndexError: list index out of range` at the `data["tasks"][0]["id"]` assert.

Re-measured here (canonical interpreter `~/.hermes/hermes-agent/venv/bin/python`, tree
`fbb67acf`, before any edit) by pointing the seam's own override at a file that does not
exist — the CI posture is a machine that has never registered a project:

| run | result |
|---|---|
| `pytest -q hscc_daemon/tests/test_kanban_cli.py` | 26 passed |
| `FLIGHTDECK_REGISTRY=<missing> pytest -q hscc_daemon/tests/test_kanban_cli.py` | **1 failed**, 25 passed — the same `IndexError`, `test_kanban_cli.py:326` |
| `FLIGHTDECK_REGISTRY=<missing> pytest -q hscc_daemon` (whole leg) | **1 failed, 4098 passed, 1 skipped in 157.91s** |

The leg-wide poison is the number that matters: across 4100 tests in the daemon leg,
**exactly one** reads project-registry host state, and it is this one. So this was never
a flake or an environment quirk — one unpinned seam, reproducible in 0.16 s.

**And on the runner itself, which is the posture this whole card is about.** `leg
hscc_daemon` on PR #553 (`pytest-legs.yml` run `44897698273`, job `136826821284`, head
`4f15c2dd`): **`4078 passed, 28 skipped in 228.57s`** — zero failures, and no
`leg_verdict.sh` warning annotation (the only annotations on the job are the Node-20
deprecation notice and a runner-capacity notice). The same job was
`1 failed, 4031 passed, 28 skipped` at head `2b044516`. The runner has no flightdeck
registry and no hermes, so that green is the hermeticity proof in the environment that
matters, not a local simulation of it.

## Why the test passed at all

`hscc kanban stale` narrows its board list to *registered project boards*
(`kanban_cli.py:253` → `autodown._enum_board_names(..., registered_only=True)` →
`autodown.py:408-412`). `registered_board_names()` (`autodown.py:335-373`) reads the
flightdeck project registry — `~/.flightdeck/registry.yaml`, or `$FLIGHTDECK_REGISTRY` —
and an absent file is `(set(), None)`: **not an error**, "no project boards".

The test patched the kanban-lib seam (`_load_kanban_db_or_default`) and nothing else. The
fake `hscc` board therefore cleared the filter **only because the operator has an `hscc`
project registered**. Green on the laptop, red on the runner, advisory-green in CI.

The rest of the file family already knew this: `test_kanban_blocked.py:279,344` and
`test_kanban_live.py:166,177` pin `registered_board_names` for exactly this reason. This
file simply never learned.

## The fix, and what it deliberately is not

`registered_only` narrowing an absent registry down to nothing is the **product rule**
(`autodown.py:341-343`): a read surface must never show an unregistered sqlite file as a
project board. The bug is the unpinned test. So:

* **per-test pin** in `test_listing_json_valid` (and `test_listing_reuses_load_kanban_db`,
  which was silently host-dependent too, though it only asserted `rc == 0`) — the shape
  the sibling files use;
* **an autouse fence** in the same file: an *unpinned* reliance on the seam now answers
  with an error rather than with the host's registry, so a future test that forgets the
  pin fails instead of going green-on-my-machine. Same backstop role the `gh` probe fence
  in `hscc_daemon/tests/conftest.py` plays;
* **the product rule is asserted, not worked around**: `test_listing_json_valid` now also
  pins `(set(), None)` and asserts the board drops out of the listing. An empty registry
  must not mean "no narrowing" — that half is now measured inside the case;
* **an out-of-process guard**, `hscc_daemon/tests/test_kanban_registry_hermetic.py`: it
  re-runs `test_kanban_cli.py` + `test_kanban_blocked.py` + `test_kanban_live.py` in a
  child pytest with `FLIGHTDECK_REGISTRY` pointing at a missing path (the `t_c51c87d7`
  pattern — poison holds for the whole child run), asserts green, and asserts the
  **pass/fail/skip counts are identical** between the poisoned and the plain posture, so
  a case that skips itself out of one posture is caught too. ~4 s for the guard.

## Teeth, probed rather than claimed

Mutations applied to `test_kanban_cli.py`, guard run after each (`PYTHONDONTWRITEBYTECODE=1`
so CPython cannot serve a stale `.pyc` for a same-length mutant):

| mutation | the file's own run | the guard |
|---|---|---|
| **M2** — fence removed **and** the pin removed (i.e. the pre-fix state) | **26 passed** ← the false green that hid the CI red | **3 failed** |
| **M3** — pin removed, fence kept | 1 failed (`IndexError`) | **3 failed** |
| restored | 30 passed (26 + 4) | 4 passed |

M2 is the whole argument for the guard: on a workstation that has an `hscc` project
registered, the un-pinned test still looks green, and only the poisoned child run moves.
M3 initially slipped past the guard's structural anti-vacuity — the case keeps a second
`(set(), None)` pin for the product-rule measurement, and a checker looking only for the
symbol stayed green. `0dc4e24b` pins the *shape* instead: the case must carry a pin whose
returned board set is non-empty.

## Boundary / what this does not change

`HSCC_TESTS_ENFORCE` is still unset in `pytest-legs.yml`, so the leg stays advisory: a
failure annotates instead of failing the job. That is a CI-posture decision (the parent
`t_4447d736` withheld promotion deliberately), not this card's, and nothing here weakens
it — the guard's value is that when promotion does happen, this class is already green on
a registry-less machine.
