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
Measured on this host with `Prober` driven directly
(`scratch/repro_mechanism.py`, venv python, one probe = `hscc project init -h`,
the real probe path — throwaway sensor port so nothing of the operator's is
touched):

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

## 3. Seal (in progress)

Design decision: **the walk must never read a port it does not own, and a
genuine leak must land on a port the walk DOES own.**

* `Prober` picks a kernel-assigned free loopback port (`sensor_port`) and does
  NOT hold it, so a stray server forked by a `--help` really can bind it.
* The sandbox seed gets `~/.hscc/api.json = {"port": sensor_port, "bind":
  "loopback"}`: the probe child resolves its api port through the sealed
  sandbox config, never through the host's or through 8787.
* `port_busy_at_start` becomes a *premise* (the sealed port is free at start,
  else pick another), never a baseline for a stranger's state.
* The 8787/8788 defensive hold stays — but strictly as defence (a stray
  grandchild cannot squat the operator's machine). The sensor never connects
  those ports again.
