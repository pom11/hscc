"""Held-stream PROXY STALL watchdog (t_fc6926fe, incident 2026-11-02).

The proxy stalls of 2026-11-02 (12:27, 12:45) had ONE signature that nothing
else in the daemon can see: LiteLLM held worker streams open on :4000 while
EVERY serving node sat idle (faulthandler proof: the frozen worker blocked in
openai ``_streaming`` ``iter_bytes`` against :4000; direct node requests
answered in <0.5 s at the same moment). ``check_proxy`` cannot catch this —
its catalogue + backend-drift probes only ask whether the proxy ACCEPTS new
work, which a stalled proxy still does. Recovery was manual both times
(``sparkrun proxy stop`` → TERM/KILL the lingering litellm → ``proxy start``).

This stream automates exactly the operator's rule. A tick is a STALL only when
ALL THREE conditions hold:

  (a) the proxy holds >=1 live socket on :4000 (ESTABLISHED — the held stream);
  (b) sum over serving endpoints of vLLM ``num_requests_running +
      num_requests_waiting`` == 0, SUSTAINED for >= 120 s (streak carried in
      monotonic time, immune to wall-clock jumps);
  (c) a cheap 5-token chat probe THROUGH :4000 still SUCCEEDS — the proxy
      accepts new requests but does not drain the held one.

ANY read that comes back UNKNOWN (socket query failed, a node's /metrics
unreachable, probe timeout) FAILS OPEN: no restart, a named degradation in the
state file, and the stall streak is reset (the 120 s must be CONTINUOUS
confirmed-idle evidence). An intentional autodown (``intentional=autodown``
AND state ``down`` — the same ``autodown.classify`` verdict the watchdog and
check_workers consult) makes the tick inert: we never fight a deliberate stop.

Remediation on a CONFIRMED stall is the operator's recipe through the existing
proxy-control path — ``health._proxy_repair_orphan``: ``sparkrun proxy stop``
→ cmdline-vetted TERM/KILL of any lingering (untracked/tracked) litellm →
plain ``sparkrun proxy start --hosts <fleet>`` → port-owner verification, each
leg a bounded subprocess (``util.run_cmd`` timeouts), so the 30 s loop can
never wedge inside a restart. Rate limit: at most ONE restart per 10 min,
persisted in the stream state file (the cooldowns.json precedent) so a
crash-looping proxy cannot be bounce-killed repeatedly; a suppressed
remediation logs + raises an event instead.

FLEET-FACT RULE: node metrics are local HTTP GETs of each serving endpoint's
``/metrics`` via ``throughput.fetch_node_metrics`` (the same parser the G/T/E
streams and verify trust) resolved from serving.json — never ssh, never local
``docker ps``. The socket count is the ONE local-host exception the sparkrun
rule explicitly allows: the proxy is THIS host's service, so a local ``lsof``
via the subprocess seam is legitimate.

Every external touch is an injectable seam (pipeline_watchdog's
``check_dgx_fn`` pattern) — tests run on fakes only and never shell out.
"""

import os
import time

from .daemon_ops import log
from .state import now_iso

STREAM = "proxy_stall"

# Sustained all-true window required before a stall is CONFIRMED. 120 s at the
# stream's 30 s cadence = 4 consecutive qualifying ticks minimum (a single
# idle snapshot while a long request finishes is NOT a stall).
STALL_SUSTAIN_S = float(os.environ.get("HSCC_PROXY_STALL_SUSTAIN_S", "120"))
# Rate limit: one remediation restart per window. Persisted (see
# _restart_times) — a daemon restart must NOT reset it, or a crash-looping
# proxy gets bounce-killed every daemon boot.
STALL_RESTART_COOLDOWN_S = float(os.environ.get(
    "HSCC_PROXY_STALL_RESTART_COOLDOWN_S", "600"))
# Per-node /metrics GET budget. Same order as ENGINE_WEDGE_BUSY_CHECK_TIMEOUT:
# a dead node must burn at most this, not the whole tick.
NODE_METRICS_TIMEOUT = float(os.environ.get(
    "HSCC_PROXY_STALL_METRICS_TIMEOUT", "3"))
# Budget for the held-stream probe THROUGH the proxy (5 tokens) and for the
# single post-restart re-probe.
PROBE_TIMEOUT = float(os.environ.get("HSCC_PROXY_STALL_PROBE_TIMEOUT", "10"))
PROBE_MAX_TOKENS = 5
# Budget for the local lsof socket query.
SOCKET_QUERY_TIMEOUT = 5

# In-process stall streak. Monotonic seconds (never wall clock — a clock jump
# must not manufacture or erase the 120 s window). Lives for the daemon's
# lifetime; a restart resets the streak, which is correct (the same doctrine
# as the engine-wedge per-unit streaks: a still-stalled proxy re-qualifies
# within 4 ticks, a healed one never re-trips). Persisted copy goes into the
# stream state for observability only.
_stall_state = {"since": None}


# ── default seams (lazy: tests inject fakes and import nothing heavy) ──────

def _default_socket_count():
    """ESTABLISHED socket count on the proxy port — THIS host's network state.

    ``lsof -iTCP:<port>`` matches either local OR remote endpoint, so it counts
    worker sockets held open against :4000 AND any upstream the proxy itself
    holds on that port — the (a) of the rule. lsof exits 1 with an EMPTY body
    when nothing matches: that is a confirmed ZERO, not an unknown. Any other
    failure (lsof missing, timeout) is UNKNOWN → the tick fails open.
    """
    from .health import PROXY_PORT
    from .util import run_cmd
    res = run_cmd(["lsof", "-nP", "-iTCP:%d" % PROXY_PORT, "-sTCP:ESTABLISHED"],
                  timeout=SOCKET_QUERY_TIMEOUT)
    if not res.get("ok"):
        # lsof's "no match" shape: nonzero rc AND no output lines.
        if not (res.get("output") or "").strip():
            return 0
        return None
    lines = [ln for ln in (res.get("output") or "").splitlines()[1:]
             if ln.strip()]
    return len(lines)


def _default_node_metrics():
    """Sum of running+waiting across every serving endpoint, via serving.json.

    Returns ``{"sum": int}`` — every endpoint answered and the fleet total —
    or ``{"unknown": "<named>"}`` when serving.json is unusable, no endpoint
    is declared, or ANY endpoint's /metrics is unreachable. An unreachable
    node is never counted as idle: UNKNOWN is its own verdict.
    """
    from . import serving, throughput
    try:
        svc = serving.load_serving()
    except Exception as exc:
        return {"unknown": f"serving.json unreadable: {type(exc).__name__}"}
    units = [u for u in ((svc or {}).get("units") or []) if isinstance(u, dict)]
    endpoints = []
    default_port = None
    try:
        default_port = serving.serving_port(svc)
    except Exception:
        default_port = None
    for u in units:
        try:
            port = int(u.get("port") or default_port)
        except (TypeError, ValueError):
            port = None
        if not port:
            continue
        for node in (u.get("nodes") or []):
            endpoints.append((node, port))
    if not endpoints:
        return {"unknown": "no serving endpoints in serving.json"}
    total = 0
    for node, port in endpoints:
        m = throughput.fetch_node_metrics(
            "http://%s:%s/metrics" % (node, port), timeout=NODE_METRICS_TIMEOUT)
        if m is None:
            return {"unknown": f"metrics unreachable: {node}:{port}"}
        total += int(m.get("running") or 0) + int(m.get("waiting") or 0)
    return {"sum": total}


def _default_probe():
    """5-token chat-completions probe THROUGH the proxy (:4000).

    True — the proxy accepted the request AND streamed text back (it is still
    serving new work); False — it answered but generated nothing inside the
    bounded window, or returned an error status. None — transport failure:
    the proxy may be fully down (check_proxy owns that; we fail open).
    """
    from .health import PROXY_PORT, _probe_unit_generation
    res = _probe_unit_generation("localhost", PROXY_PORT, timeout=PROBE_TIMEOUT,
                                 max_tokens=PROBE_MAX_TOKENS)
    err = res.get("error") or ""
    if res.get("ok"):
        return True
    if err in ("wedged", "closed_early") or res.get("status"):
        # Answered (HTTP) without generating, or answered with an HTTP error:
        # a KNOWN negative — the proxy is not draining/generating for us.
        return False
    return None


def _default_intentional():
    """True only for intentional=autodown AND state down (expected_down).

    The NARROW verdict (health._intentional_autodown_down), same semantics the
    check_workers resurrection guard uses: during a WAKE the proxy may be
    legitimately half-up, but a stall signature while waking is still a stall
    we may need to heal; only a deliberate DOWN is untouchable.
    """
    from .health import _intentional_autodown_down
    return bool(_intentional_autodown_down())


def _default_restart():
    """The operator's recipe via the existing proxy-control path.

    ``health._proxy_repair_orphan`` = ``sparkrun proxy stop`` → cmdline-vetted
    TERM/KILL of any lingering litellm (tracked-hollow or orphan listener) →
    plain ``sparkrun proxy start --hosts <fleet>`` → port-owner verification.
    Each leg is a bounded ``util.run_cmd`` subprocess, so the 30 s stream can
    never wedge inside a restart; sparkrun owns the lifecycle, we own the
    order. Heal lease first (F2 doctrine): one repair at a time — a held
    lease defers this tick instead of racing another repair.
    """
    from . import health
    from . import heal_lease
    ok, holder = heal_lease.acquire("proxy-stall-restart",
                                    f"worker proxy on :{health.PROXY_PORT}")
    if not ok:
        return {"ok": False, "skipped": "heal-lease",
                "detail": heal_lease.describe(holder)}
    try:
        hosts = health._proxy_relaunch_hosts(health.serving.load_serving())
        return health._proxy_repair_orphan(hosts)
    finally:
        heal_lease.release("proxy-stall-restart")


# ── cooldown persistence (cooldowns.json precedent, via the stream state) ──

def _restart_times(state, wall_now):
    """Trimmed list of restart timestamps (EPOCH) inside the cooldown window.

    Wall clock by necessity: these stamps persist in the stream state and the
    cooldown must survive a daemon restart — a monotonic value read back after
    a restart is meaningless (a garbage/negative age must never re-arm nor
    silently disarm the throttle, so out-of-window AND future stamps are
    dropped here and only a stamp inside [0, cooldown] counts).
    """
    times = []
    for t in (state or {}).get("restart_times") or []:
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        if 0 <= wall_now - t <= STALL_RESTART_COOLDOWN_S:
            times.append(t)
    return times


# ── the three condition guards + the cooldown gate (module-level so the
#    mutation proofs in the test file can replace exactly one of them and
#    show it guards exactly its own scenario) ───────────────────────────────

def _cond_a(sockets):
    """(a) The proxy holds at least one live socket — the held stream."""
    return sockets >= 1


def _cond_b(metrics):
    """(b) Sum of running+waiting across ALL serving endpoints is zero."""
    return metrics.get("sum", 1) == 0


def _cond_c(probe):
    """(c) The 5-token probe through the proxy SUCCEEDED.

    Strict ``is True``: False (answered, generated nothing / error status) is
    a known-negative condition failure — a fully-sick proxy is check_proxy's
    domain, not the held-stream shape; None never reaches here (fail-open
    unknown handled earlier in the tick).
    """
    return probe is True


def _cond_cooldown(in_window, wall_now):
    """Restart is forbidden while a restart stamp sits inside the window."""
    return bool(in_window) and \
        (wall_now - max(in_window)) <= STALL_RESTART_COOLDOWN_S


# ── the tick ────────────────────────────────────────────────────────────────

def check_proxy_stall(socket_count_fn=None, node_metrics_fn=None, probe_fn=None,
                      restart_fn=None, intentional_fn=None, write_state_fn=None,
                      read_state_fn=None, emit_fn=None, detector=None, now=None,
                      wall_now=None):
    """One proxy-stall watchdog tick (the ``proxy_stall`` CHECK STREAM).

    Returns True unless a stall is currently DECLARED (confirmed and not yet
    healed) — matching how the other health checks return their ``ok``.

    Injectable seams (tests use fakes ONLY; no live sparkrun/lsof/HTTP):
      * ``socket_count_fn()`` → int | None  (condition a; None = UNKNOWN)
      * ``node_metrics_fn()`` → {"sum": int} | {"unknown": str} (condition b)
      * ``probe_fn()``        → True | False | None             (condition c)
      * ``restart_fn()``      → {"ok": bool, ...}               (remediation)
      * ``intentional_fn()``  → bool  (autodown gate)
      * ``write_state_fn`` / ``read_state_fn`` / ``emit_fn`` / ``detector`` —
        state, events, stall-streak memory.
      * ``now`` — monotonic clock for the 120 s streak (a wall-clock jump must
        never manufacture or erase the window); ``wall_now`` — epoch clock for
        the PERSISTED cooldown timestamps (monotonic values are meaningless
        across processes; the cooldown must survive a daemon restart).

    A raising seam must never kill the stream thread and must never restart
    anything: the wrapper turns any unexpected exception into a fail-open OK
    tick (a bug in this watchdog must not flip the fleet red).
    """
    try:
        return _tick(socket_count_fn, node_metrics_fn, probe_fn, restart_fn,
                     intentional_fn, write_state_fn, read_state_fn, emit_fn,
                     detector, now, wall_now)
    except Exception as exc:  # pragma: no cover - defensive, stream never dies
        log(f"Proxy-stall check: unexpected {type(exc).__name__}: {exc} — "
            "fail open (no restart)", "ERROR")
        return True


def _tick(socket_count_fn, node_metrics_fn, probe_fn, restart_fn,
          intentional_fn, write_state_fn, read_state_fn, emit_fn, detector,
          now, wall_now):
    from .state import write_state, read_state
    socket_count_fn = socket_count_fn or _default_socket_count
    node_metrics_fn = node_metrics_fn or _default_node_metrics
    probe_fn = probe_fn or _default_probe
    restart_fn = restart_fn or _default_restart
    intentional_fn = intentional_fn or _default_intentional
    write_state_fn = write_state_fn or write_state
    read_state_fn = read_state_fn or read_state
    if emit_fn is None:
        from .desktop import emit_event
        emit_fn = emit_event
    if detector is None:
        detector = _stall_state
    if now is None:
        now = time.monotonic()
    if wall_now is None:
        wall_now = time.time()

    # The persisted cooldown read ONCE per tick (local state-file read) and
    # carried forward into EVERY stamp below: a healthy/suspect/unknown stamp
    # that wrote an empty restart_times would silently wipe the 10-min window
    # the next stall needs. The FLEET/PROXY seam reads stay behind the gate.
    prior = read_state_fn(STREAM) or {}
    carried = _restart_times(prior, wall_now)

    def _stamp(status, ok, message, degradation=None, restart_times=None,
               last_restart_ts=None):
        write_state_fn(STREAM, {
            "ok": ok,
            "status": status,
            "stall_since": detector.get("since"),
            "stall_sustain_s": STALL_SUSTAIN_S,
            "restarts_last_10m": len(
                carried if restart_times is None else restart_times),
            "restart_times": (carried if restart_times is None
                              else restart_times),
            "last_restart_ts": (prior.get("last_restart_ts")
                                if last_restart_ts is None else last_restart_ts),
            "degradation": degradation,
            "last_check": now_iso(),
            "message": message,
        })

    # Gate FIRST: never fight a deliberate stop. Same classify() table as the
    # watchdog/check_workers guards — no parallel rule invented here.
    if intentional_fn():
        _stamp("inert", True, "intentional autodown in effect — proxy stall "
                              "detection inert (deliberate stop)")
        return True

    sockets = socket_count_fn()
    metrics = node_metrics_fn() or {"unknown": "metrics fn returned nothing"}
    probe = probe_fn()

    # ANY unknown → fail OPEN with a NAMED degradation. The streak resets: the
    # 120 s window must be continuous CONFIRMED-idle evidence, and a gap in
    # observation is not confirmation.
    if sockets is None:
        detector["since"] = None
        _stamp("unknown", True, "socket query failed — fail open (no restart)",
               degradation="socket_query_failed")
        return True
    if "unknown" in metrics:
        detector["since"] = None
        _stamp("unknown", True,
               f"node metrics unknown — fail open: {metrics['unknown']}",
               degradation=metrics["unknown"])
        return True
    if probe is None:
        detector["since"] = None
        _stamp("unknown", True, "proxy probe transport failure — fail open "
                                "(no restart; check_proxy owns proxy-down)",
               degradation="probe_unreachable")
        return True

    # The three conditions, evaluated fresh EVERY tick through the module
    # guards (condition c is part of the stall definition, not just its
    # confirmation: a stall is the proxy that serves new work while holding
    # old streams).
    if _cond_a(sockets) and _cond_b(metrics) and _cond_c(probe):
        if detector.get("since") is None:
            detector["since"] = now
            log("Proxy-stall check: SUSPECT — proxy holds socket(s) on "
                f":proxy while all serving nodes idle (streak started, need "
                f">={int(STALL_SUSTAIN_S)}s sustained)")
            _stamp("suspect", True, "suspect: held stream + idle fleet, "
                                    "awaiting 120 s confirmation")
            return True
        age = now - detector["since"]
        if age < STALL_SUSTAIN_S:
            _stamp("suspect", True,
                   f"suspect {age:.0f}s (need {int(STALL_SUSTAIN_S)}s)")
            return True

        # ── CONFIRMED STALL (all three, sustained) ──────────────────────────
        log(f"Proxy-stall check: STALL CONFIRMED — proxy holds stream(s) on "
            f"::4000, fleet idle {age:.0f}s, probe still succeeds — "
            "triggering remediation")
        emit_fn("proxy.stall.confirmed", {
            "stall_age_s": round(age), "sockets": sockets,
            "fleet_idle_sum": metrics.get("sum"),
        }, severity="error")

        in_window = carried
        if _cond_cooldown(in_window, wall_now):
            since_last = wall_now - max(in_window)
            wait = STALL_RESTART_COOLDOWN_S - since_last
            log(f"Proxy-stall check: restart SUPPRESSED — last restart "
                f"{since_last:.0f}s ago (< {int(STALL_RESTART_COOLDOWN_S)}s "
                f"cooldown); retry in {wait:.0f}s", "WARN")
            emit_fn("proxy.stall.suppressed", {
                "stall_age_s": round(age), "retry_in_s": round(wait),
                "restarts_last_10m": len(in_window),
            }, severity="warning")
            _stamp("suppressed", False,
                   f"stall confirmed; restart suppressed by "
                   f"{int(STALL_RESTART_COOLDOWN_S)}s cooldown (retry in "
                   f"{wait:.0f}s)",
                   degradation="restart_suppressed_cooldown",
                   restart_times=in_window, last_restart_ts=max(in_window))
            return False

        # Remediation: operator's recipe via the existing proxy-control path.
        # The cooldown is stamped BEFORE judging the outcome: even a restart
        # that fails to verify must spend the 10-min budget, or a proxy that
        # answers the recipe wrongly gets re-killed every 30 s tick.
        res = restart_fn() or {}
        restart_times = in_window + [wall_now]
        reprobe = probe_fn()
        healed = bool(res.get("ok")) and reprobe is True
        if healed:
            detector["since"] = None
            log("Proxy-stall check: HEALED — proxy restart completed and the "
                "5-token probe through the proxy succeeds again")
            emit_fn("proxy.stall.healed", {
                "stall_age_s": round(age), "restart": str(res)[:200],
            }, severity="warning")
            _stamp("healthy", True, "stall healed via proxy restart (verified "
                                    "by post-restart probe)",
                   restart_times=restart_times, last_restart_ts=wall_now)
            return True
        # Not healed: the stall is still real — keep the streak and the red
        # stream; the cooldown stamp spent above gates the next attempt.
        log(f"Proxy-stall check: remediation did NOT restore service "
            f"(restart: {str(res)[:200]}; re-probe: {reprobe!r}) — stream "
            "stays red; next attempt respects the cooldown", "ERROR")
        _stamp("stall", False,
               "confirmed stall; restart attempted but service NOT verified",
               degradation="restart_unverified",
               restart_times=restart_times, last_restart_ts=wall_now)
        return False

    # Any single condition false (no held socket / fleet busy / probe not OK)
    # clears the streak — the AND was broken, the window restarts from zero.
    if detector.get("since") is not None:
        log("Proxy-stall check: stall streak RESET — condition cleared "
            f"(sockets={sockets}, fleet_sum={metrics.get('sum')}, probe={probe!r})")
    detector["since"] = None
    _stamp("healthy", True,
           f"healthy (sockets={sockets}, fleet_sum={metrics.get('sum')}, "
           f"probe={probe!r})")
    return True
