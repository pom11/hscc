# t_e5a9469e — the (D) api-port sensor reads the operator's live port

Branch `wt/t_e5a9469e`, base `d23b0ea3`.

## 1. Mechanism — MEASURED, not assumed

The finding text `--help bound the api port 8787` is emitted by exactly one
sensor (`scripts/hscc_cli_help_audit.py:1381`), computed per probe:

    "port_flip": (not self.port_busy_at_start) and self._port_busy()   # :625

`port_busy_at_start` is read ONCE in `Prober.__init__` (`:514`), BEFORE the
defensive hold binds anything (`:540-550`), and `_port_busy()` (`:573-594`)
answers from the hold when the Prober owns the port and from a REAL
`connect_ex("127.0.0.1", API_DEFAULT_PORT)` when the hold FAILED.

So the sensor has exactly one fireable posture, and it is the non-hermetic one.
Before driving it, the socket semantics it relies on were measured on this host
(`darwin`, python 3.14.10, `scratch/probe_bound_no_listen_semantics.py`) against
the sensor's own read — `settimeout(0.25)` then `connect_ex(...) == 0`
(`hscc_cli_help_audit.py:690-692`):

```
state                       connect_ex          secs     `== 0` -> sensor verdict
nothing bound               ECONNREFUSED (61)   0.0001   False  free      (correct)
bound, never listen()ed     EAGAIN (35)         0.2521   False  free      (WRONG: held)
real listener               0                   0.0001   True   busy      (correct)
```

A bound-but-not-`listen()`ed socket answers no SYN at all on macOS/BSD — the read
waits out the whole window and returns `EAGAIN`, which `== 0` calls free. That
kernel behaviour is already pinned on main
(`test_cli_help_surface.py:1199`, `test_bound_without_listen_does_not_report_refused`)
and the code comment at `hscc_cli_help_audit.py:524-529` says the same; the sensor
just did not act on it. It matters because the audit's defensive hold IS such a
socket, and so is a *concurrent* audit's: the baseline can record "free" while
another process owns the port. (While these numbers were taken, host 8787 answered
`EAGAIN` after 0.25 s — another walk's hold was live.)

Measured on this host with `Prober` driven directly by a scratch-only diagnostic
(`repro_mechanism.py`, not committed — its output is transcribed below, and the
socket table above comes from the same kind of probe), with the venv python, one
probe = `hscc project init -h` — the real probe path, on a throwaway sensor port
so nothing of the operator's is touched.

| posture (state of the sensor port)                 | hold taken | busy_at_start | port_flip |
|----------------------------------------------------|-----------|---------------|-----------|
| S1 nothing bound (clean)                           | yes       | False         | **False** |
| S2 foreign bind WITHOUT listen, stays that way     | no        | False         | False     |
| S3 foreign bind without listen → real listener     | no        | False         | **True**  |
| S4 listener already up when the audit starts       | no        | True          | **False** |

* S1: while the hold is up nothing else can bind the port, so the sensor is
  **unfireable** — the clean walk cannot produce this finding at all.
* S3: the audit failed to acquire the port, and a listener appeared during the
  walk — every probe in that window is charged `--help bound the api port`,
  and the probes did nothing. Re-running S3 over the five surfaces of the
  incident reproduces the incident verbatim:

      port_flip=True  hscc project init -h
      port_flip=True  hscc project legacy-cards --help
      port_flip=True  hscc project legacy-cards -h
      port_flip=True  hscc project lint-cards --help
      port_flip=True  hscc project lint-cards -h

  (five, because that is how many probes the walk ran while the foreign
  listener lived — the finding is de-duplicated per surface+kind+detail).
* S4: the SAME host state, started a few seconds earlier, is permanently
  silent (`port_busy_at_start` True short-circuits the AND). That asymmetry is
  why the base-control re-run and the file-alone run were both green on the
  same machine with the same host: the finding depends on WHEN the audit
  started relative to the foreign process, not on the tree.

`test_cli_help_surface.py:1251` (`test_prober_never_asks_a_port_it_holds`)
already documents the first half in words — "port_flip cannot fire while this
Prober holds the port" — i.e. the check only ever measures a port it does not
own. It is a host-state reader with a probe's name.

## 2. What the host state was (and was not)

Not the operator's api: `~/.hscc/api.json` is `{"port": 8788, "bind": "tailscale"}`
and the live unit (pid 14493, up 23h46m) runs `-m api_server serve --bind
10.0.0.x --tailscale` (the operator's LAN address, redacted) — it listens on
8788 on the LAN/tailnet address,
never on `127.0.0.1:8787`. `~/.hscc/daemon.log` has no api/port/launchd event
in the incident window (its last line is 08:34:51Z, the incident was ~08:50Z),
and `lsof -iTCP:8787` is empty now. `scripts/api_smoke.py` is a WS *client*
(default port 8788) and `hscc-api/tests` + `hscc_daemon/tests` bind ephemeral
loopback ports / `no_fork` fixtures.

So the trigger class is "ANY process that binds 127.0.0.1:8787 while the walk
runs (60–300 s, many workers share this host) — including another audit run's
own defensive hold, which is itself a bound-without-listen 8787". I could not
attribute the single 10:50 event to a named process from host logs; the fix
below deliberately does not depend on knowing who it was.

## 3. Seal (implemented, `8d06074d` on `wt/t_e5a9469e-r1`)

Design decision: **the walk must never read a port it does not own, and a
genuine leak must land on a port the walk DOES own.**

* `Prober.sensor_port` — reserved from the kernel (`bind(port 0)`, released),
  verified free (`connect_ex` must answer `ECONNREFUSED`; a `0`/`EAGAIN` answer
  means a stranger and retries), deliberately **not held** (a held port cannot
  be bound by the stray server the check looks for — that is precisely why the
  old sensor was dead), and re-verified as the walk's baseline: a busy answer
  raises instead of silently re-baselining.
* `Prober._write_sealed_api_config()` seeds the sandbox `~/.hscc/api.json`
  with `{"port": sensor_port, "bind": "loopback"}`. Every probe runs with
  `HOME=<sandbox>` and `hscc-api/api_server.py` resolves
  `defaults <- api.json <- explicit flags`, so a probe child resolves the
  sealed port: a real leak binds the port the walk measures, and no probe ever
  resolves or binds 8787.
* `Prober._port_busy()` defaults to `sensor_port`. The 8787/8788 defensive hold
  stays (defence: a stray grandchild cannot squat the operator's machine) but
  the walk may no longer open a socket to either — pinned by a `connect_ex`
  spy over a whole walk.
* Findings carry `<SEAL_PORT>` (the number is kernel-assigned, so it must not
  reach the bipartite expectation table as a literal) and the real number goes
  to `report["sealed_port"]` / `report["defended_ports"]`.

Measured after the change (`posture_run.py`, `~/.hermes/.../venv/bin/python`,
this host, other agents' legs running concurrently):

| posture | what the host does | result |
|---|---|---|
| control | nothing on 8787/8788 | **33 passed, 1 skipped**, rc 0 |
| poisoned | a foreign process owns both defended ports for the whole walk: bound-without-listen while the walk starts (the audit's hold FAILS — the incident's precondition), then a real listener for the rest of the run | **33 passed, 1 skipped**, rc 0 (see below) |
| leak | `hscc api start --help` mutated to fork a detached stray listener on the port the *child* resolves | **1 failed**, `NEW [D] hscc api start --help :: --help bound the sealed api port <SEAL_PORT>` (+ every later probe) |

The control's 33 passed is also the proof that the seal does not buy its
hermeticity by moving the goalposts: `test_a_counts_match_committed_table`,
`test_findings_match_expected` and the width/style/coverage gates all still pass
against the committed baselines, so the seeded `api.json` changed no render.

Mutation probes of the seal itself (`PYTHONDONTWRITEBYTECODE=1`, byte-exact
restore asserted, `hscc-flake-triage` step 5):

| mutant | result |
|---|---|
| `_port_busy` default back to `API_DEFAULT_PORT` | **3 failed** — incident replay, positive control, `connect_ex` spy |
| sandbox `api.json` seed dropped | **1 failed** — `test_probe_child_resolves_the_sealed_api_port` |
| `port_flip` pinned to `False` | **1 failed** — the positive control (no blind-green) |

Two things the first leak mutation taught, both worth keeping:

1. A stray listener that **inherits the probe's stdout/stderr pipes** makes
   `subprocess.run(capture_output=True)` block until it exits, so the probe
   "completes" after the stray is gone and the flip is invisible — the first
   mutant forked with `os.fork()` and reported clean while taking 40 s. The
   mutant now detaches (`start_new_session`, stdio to `DEVNULL`) the way a real
   server does. `hscc api start` double-forks + `setsid()` but does **not**
   redirect fds (`hscc_daemon/api_cli.py:774-796`), so a genuine regression on
   that path would surface as the `hung` (D) finding at 60 s *and* a flip on
   every probe after the grandchild is abandoned — the two sensors are
   complementary, not redundant.
2. A "reverted" mutation that only renames the sensor's *symbol* cannot be
   caught by text-matching the guard; the guard's premise assertions
   (hold genuinely failed, baseline genuinely free, foreign listener genuinely
   answering) are what make the failure real.

## 4. Delivery and what a reviewer should re-check

* Branch `wt/t_e5a9469e` (pre-rebase, base `d23b0ea3`) and **`wt/t_e5a9469e-r1`**
  (rebased onto `origin/main` `bb5d2b83`, head `5813970f`) are both pushed;
  PR **https://github.com/pom11/hscc/pull/598** is from `-r1`. The force-push that
  would have updated the first branch is refused in single-query mode, so the
  rebased line went to a new ref instead — merge `-r1`, not `wt/t_e5a9469e`.
* `scripts` leg on the exact tested bytes: **591 passed, 5 skipped** at
  `d23b0ea3`, **593 passed, 5 skipped** at `bb5d2b83` (the delta in `scripts/` was
  `gen_site_commands.py` + `test_site_landing.py`, i.e. NOT byte-for-byte equal,
  so the leg was re-run rather than assumed).
* The one thing a reviewer should re-check above all: that sealing the port does
  not weaken the check. It cannot —
  `test_port_flip_fires_when_a_probe_binds_the_sealed_port` is the positive
  control, and the leak posture above is the full-audit version of the same proof.

## 5. Deliberately not done

* The `--help` walk is unchanged; no baseline entry was added to
  `scripts/tests/cli_help_expected.json` — a baseline entry would have absorbed
  a real leak, which is the failure mode the card named.
* `site/` untouched. The `8787` literal inside legitimate help text
  (`hscc_daemon/api_cli.py:70` "Override the configured port (default 8787)")
  stays: it is documentation of the default, not an execution echo, and no
  rule in this gate reads text for ports (rule D is an *execution* sensor).
* `hscc-daemon/tests` and `hscc-api/tests` unaffected: the change is confined
  to `scripts/hscc_cli_help_audit.py` + its pin file. The (A)-only sibling pass
  (`a_table`) builds its own `Prober`, so it inherits the seal for free.

