# t_4c9feb44 — scripts/tests leg "stalls" at ~test 233: `test_cli_help_surface.py`

Result: **root-caused, fixed, red-before-green proven.** The leg was not hung —
the module-scoped `report` fixture was paying a 0.25 s black-holed socket
timeout on every one of its 961 CLI probes.

Host for every number below: this Mac Studio, `~/.hermes/hermes-agent/venv/bin/python`
3.14.10, `HERMES_DISABLE_LAZY_INSTALLS=1 HSCC_SKIP_COMPACT_PROBE=1`, clean
detached worktree of `main` @ `a65695a6`. Load recorded per run (1.8–2.6, other
cards' legs sharing the box).

## 1. The symptom, restated precisely

`test_surface_is_enumerated` alone for 75 s, tree-watched every 5 s
(`probe/capture.sh`): pytest was *not* blocked on one child. CLI probe children
appeared briefly and sparsely while pytest itself accumulated ~0.1 s CPU per
5 s sample:

```
20:09:53  35718  python -m hscc_daemon.hscc bootstrap --help   (R, cpu 0:00.06)
20:10:09  35913  python -m hscc_daemon.hscc start --help        (R, cpu 0:00.05)
```

So: a crawl at ~one probe per couple of seconds, which for 961 probes is minutes
— indistinguishable from a hang from the outside, and exactly why `-p
faulthandler --faulthandler-timeout=25` printed nothing (no single long wait).

## 2. The fingerprint

`lsof` on the stalled pytest pid (`probe/sample_probe.sh`, cap2/lsof.txt):

```
python 37614 11u IPv4 TCP localhost:msgsrvr (CLOSED)      <- the audit's own tripwire bind
python 37614 12u IPv4 TCP localhost:8788   (CLOSED)      <-   (8787 / 8788)
python 37614 13u IPv4 TCP localhost:57903->localhost:msgsrvr (SYN_SENT)
```

`msgsrvr` = 8787 = `hscc-api/api_server.py DEFAULT_PORT`, which
`scripts/hscc_cli_help_audit.py` holds as a defensive tripwire.

## 3. The lie in the code, measured

`Prober.__init__` bound the api ports **without** `listen()` and its comment
asserted: *"No listen() means connect_ex still reports 'refused', so the
port_flip sensor below keeps its exact semantics"*. Measured
(`probe/binding_blackhole.py`):

| port state | `connect_ex("127.0.0.1", 8787)` | secs |
|---|---|---|
| nothing bound | `ECONNREFUSED` (61) | 0.000 |
| bound + `listen()` | accepted (0) / refused (54) | 0.000 |
| **bound, no `listen()`** | **`EAGAIN` (35), SYN_SENT** | **0.252 / 2.001 = the whole timeout** |

A bound-but-not-listening port never answers the SYN, so the caller waits out
its own `settimeout`. This is the skill's "unreachable is not refused" trap,
manufactured by the harness against itself. (macOS returns EAGAIN=35 where other
BSDs return EINPROGRESS=36 — both mean "still waiting".)

A/B inside the real code path, same `Prober`, sandbox, env and widths, only the
hold closed (`probe/ab_tripwire.py`): **0.30 s vs 0.05 s per probe — 0.28 s per
probe of pure black hole.**

## 4. The census (what the leg's wall actually is)

`probe/audit_census.py` wraps `Prober.run`, `_coverage` and
`_systemic_import_probe` with timers and runs `audit()` once — a census, not a
sample:

| run | audit wall | Prober.run calls | sum(Prober.run) | CLI subprocess | sensor+bookkeeping | residue |
|---|---|---|---|---|---|---|
| shipped | **307.64 s** | 961 | 307.27 s (99.9%) | 84.22 s | **223.05 s = 0.232 s/call** | 0.29 s |
| shipped, hold closed | **60.00 s** | 961 | 59.82 s | 62.51 s | −2.69 s | 0.09 s |
| **fixed (this card)** | **59.69 s** | 961 | 59.52 s | 62.22 s | −2.70 s | 0.08 s |

All three report identically: `surface_paths=169 probes=331 findings=20`. So
**73 % of the leg's audit wall was the audit's own port sensor**, and removing
it changes nothing the gate asserts. Load was 1.7–2.1 for these runs; the
slowest single probe is 0.38 s (shipped) / 0.10 s (fixed), and no probe exceeds
2 s in any run.

## 5. The fix

`scripts/hscc_cli_help_audit.py` — `Prober`:

* the hold now records what it holds: `self._held_ports: dict[int, socket]`
  (`_tripwire_socks` stays as a list view);
* `_port_busy(port=None)` answers **`False` immediately for a port this Prober
  holds** and only opens a socket for a port it does not. The port is resolved
  at call time so a test can move the sensor off the real 8787.

Why that is verdict-preserving: while the hold is up, a stray server cannot
bind the port (it dies on `EADDRINUSE` — that *is* the hold), so `port_flip`
was already unfireable and the probe could only ever answer "not busy". When the
bind **fails** (operator's live api, or a concurrent audit) the port is not held
and the sensor probes for real, exactly as before. `port_busy_at_start` still
runs before the hold, so it still comes from the kernel.

## 6. Red-before-green (`scripts/tests/test_cli_help_surface.py`)

Old product code + new tests, verbatim:

```
.F.                                                                      [100%]
FAILED scripts/tests/test_cli_help_surface.py::test_prober_never_asks_a_port_it_holds
1 failed, 2 passed, 26 deselected in 1.51s
```

Restored (proven by `diff` against the saved copy) + new tests:

```
...                                                                      [100%]
3 passed, 26 deselected in 1.44s
```

The three tests, and why each is shaped that way:

* `test_bound_without_listen_does_not_report_refused` — pins the **kernel
  behaviour** the deleted comment assumed away (unbound → `ECONNREFUSED` <0.5 s;
  bound-no-listen → `EAGAIN`/`EINPROGRESS` after ≥0.5 s of a 1 s timeout).
  macOS-gated with the reason stated: Linux answers with an RST, so the split
  cannot exist there. Fails before *and* after by design — it is the trap pin,
  and it fires if an OS release ever makes the question free again.
* `test_prober_never_asks_a_port_it_holds` — the red half. Asserts "no syscall"
  via a sentinel socket factory, not "fast", so load cannot flake it. Runs on a
  throwaway loopback port, so it does not contend with the `report` fixture's
  own hold for 8787 (a test that needed the real port would silently skip in a
  full-file run and un-gate the fix).
* `test_prober_still_probes_a_port_it_does_not_hold` — the guard that the fix
  did not blind the sensor: blocker bound **without** `listen()` (a `listen()`
  here would make a *concurrent* audit on this host read the port as busy and
  manufacture a false (D) finding in another card's leg), fake socket answers
  "accepted", `_port_busy()` must still say busy.

Also fixed: the `report` fixture docstring claimed "full CLI walk ~35 s". It now
states the measured 59.69 s, what it was under the bug (307.64 s) and why.

## 7. Not this card / kept out of scope

* `test_readme_lint.py::test_repo_readmes_pass_the_lint` is red on `main`
  (`scripts/README.md` `ruby -c` CMD-BINARY) — baseline noise, owned by
  t_d3448025. Not touched here.
* The audit still launches **961 CLI subprocesses** for 331 surfaces (widths
  80/120/500). At ~60 s that is now the honest floor, not a bug — but if the leg
  ever needs to be cheaper, the next lever is the width fan-out, and the census
  script above is how to prove any such cut is table-preserving (t_28b0e521 did
  exactly this for the (A)-only pass).
* `audit()` never closes its `Prober`'s hold sockets, so the module fixture
  leaks 8787/8788 for the rest of the pytest process. Harmless today (a later
  hold degrades to "bind failed") but worth a `close()`; deliberately not done
  here to keep this diff to the cost that was measured.

## 8. Reproduction

```
bash probe/capture.sh <worktree> <pytest-target> <outdir> [secs] [every]   # tree watch + kill by pid
bash probe/sample_probe.sh <worktree> <pytest-target> <outdir> [warm] [secs]
~/.hermes/hermes-agent/venv/bin/python probe/binding_blackhole.py          # the kernel split
~/.hermes/hermes-agent/venv/bin/python probe/ab_tripwire.py <worktree>     # A/B the hold
~/.hermes/hermes-agent/venv/bin/python probe/audit_census.py <worktree>    # the census
```
(probe/ lives in the card workspace, not the repo.)
