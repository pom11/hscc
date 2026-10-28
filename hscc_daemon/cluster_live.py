"""`hscc cluster monitor --live`: a refreshing fleet view.

Two sources, polled independently so a slow one never stalls the other:

* host metrics — ONE long-running ``sparkrun cluster monitor --json`` child.
  It already streams a frame per interval over its own SSH sessions, so we
  keep the newest frame instead of re-spawning sparkrun (and re-opening SSH)
  every tick.
* serving load — the proxy's ``/v1/model/info`` says which backend
  (node:port) serves which alias; each backend's vLLM ``/metrics`` gives the
  requests running/waiting, KV-cache use and token counters. Token rates are
  the counter delta between two ticks.

The host table mirrors sparkrun's monitor columns minus the per-container
detail (the Jobs column is enough). Everything that touches the network or a
subprocess is injectable so the renderers and parsers are testable offline.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hscc_daemon.cli_theme import esc

PROXY_PORT = int(os.environ.get("HSCC_PROXY_PORT", "4000"))
DEFAULT_INTERVAL = 2
BACKENDS_REFRESH_S = 30  # the router changes rarely; don't ask it every tick

_METRICS = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:kv_cache_usage_perc": "kv",
    "vllm:gpu_cache_usage_perc": "kv",  # older vLLM name for the same gauge
    "vllm:generation_tokens_total": "gen_total",
    "vllm:prompt_tokens_total": "prompt_total",
}
_PROM_LINE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)')
_MODEL_LABEL = re.compile(r'model_name="([^"]*)"')


# ── parsing ────────────────────────────────────────────────────────────────

def parse_vllm_metrics(text: str) -> dict:
    """Prometheus text -> {model_name: {running, waiting, kv, gen_total, prompt_total}}.

    Values are summed across engines (data-parallel replicas expose one series
    per ``engine`` label) except ``kv``, which is averaged.
    """
    out: dict[str, dict] = {}
    kv_n: dict[str, int] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _PROM_LINE.match(line)
        if not m:
            continue
        key = _METRICS.get(m.group(1))
        if key is None:
            continue
        try:
            val = float(m.group(3))
        except ValueError:
            continue
        mm = _MODEL_LABEL.search(m.group(2) or "")
        model = mm.group(1) if mm else "?"
        row = out.setdefault(model, {})
        row[key] = row.get(key, 0.0) + val
        if key == "kv":
            kv_n[model] = kv_n.get(model, 0) + 1
    for model, n in kv_n.items():
        if n > 1:
            out[model]["kv"] /= n
    return out


def hostport(api_base: str) -> str:
    """'http://10.0.0.2:8001/v1' -> '10.0.0.2:8001'; '' when unparseable."""
    try:
        p = urllib.parse.urlparse(api_base or "")
    except ValueError:
        return ""
    if not p.hostname:
        return ""
    return f"{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}"


def parse_model_info(data: dict) -> dict:
    """Proxy /v1/model/info -> {"node:port": sorted [aliases routed there]}."""
    routes: dict[str, set] = {}
    for m in (data or {}).get("data") or []:
        hp = hostport((m.get("litellm_params") or {}).get("api_base") or "")
        if hp:
            routes.setdefault(hp, set()).add(m.get("model_name") or "?")
    return {hp: sorted(names) for hp, names in routes.items()}


def fmt_tokens(n) -> str:
    """Compact token count: 999, 202k, 18.0M, 1.23G; '-' when unknown."""
    if n is None:
        return "-"
    n = float(n)
    if n < 1e3:
        return f"{n:.0f}"
    if n < 1e6:
        return f"{n / 1e3:.0f}k"
    if n < 1e9:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1e9:.2f}G"


def rates(prev: dict | None, cur: dict, dt: float) -> tuple:
    """(gen tok/s, prompt tok/s) from two counter snapshots; None when unknown.

    A counter that went DOWN means the engine restarted — report None rather
    than a huge negative rate.
    """
    if not prev or dt <= 0:
        return None, None
    out = []
    for key in ("gen_total", "prompt_total"):
        a, b = prev.get(key), cur.get(key)
        out.append(None if a is None or b is None or b < a else (b - a) / dt)
    return tuple(out)


# ── data sources ───────────────────────────────────────────────────────────

# ── BEGIN mirrored block: lan_curl_fallback (t_391b084e) ──────────────────
# macOS binds Privacy > Local Network to the *signing identity* of the
# executable, so an adhoc-signed interpreter — a Homebrew keg's Python.app, a
# python.org Framework build — is denied on EVERY private-range connect, no
# matter how often the Python entries are re-toggled. urllib then reports
# `OSError(65, 'No route to host')` (EHOSTUNREACH) for a host that is up and
# answering, while Apple-signed /usr/bin/curl — which keeps its own TCC grant
# — reaches the same URL fine. That is what made `hscc cluster monitor --live`
# render every backend `down` on a healthy fleet (host stats survived only
# because they ride ssh). Do NOT try to fix this with TCC/system changes: it is
# an OS identity rule, not our bug. The workaround is to retry exactly that one
# failure through curl.
#
# THREE PLUGINS CARRY THIS BLOCK VERBATIM (hscc_daemon/cluster_live.py,
# hscc-bootstrap/doctor.py, hscc-cluster/cluster_template.py). It is NOT a
# cross-plugin import on purpose: each plugin deploys standalone into
# ~/.hermes/plugins and the hyphenated dirs are not importable package names,
# so none of them can import another. The block is pinned byte-identical by
# hscc-cluster/tests/test_lan_curl_fallback.py::test_mirrored_block_is_identical
# — edit all three copies in the same commit, or that test fails.
#
# Deliberately narrow. The fallback fires ONLY when urllib reports errno 65
# SPECIFICALLY on a PRIVATE host:
#   * Linux EHOSTUNREACH is 113 and means there genuinely is no route, so it
#     must NOT be retried by anything — a "helpful" fallback there would hide a
#     dead node behind a second probe.
#   * A public host is not the Local Network story at all (TCC gates only local
#     traffic), so falling back there would paper over a real firewall or DNS
#     problem.
#   * Loopback never falls back: TCC does not gate localhost, so errno 65
#     there is not this bug.
# HTTPError, timeouts and every other errno propagate UNCHANGED, so callers
# keep their existing 'probe error' vs 'unreachable' distinction.
import ipaddress
import queue
import urllib.error
import urllib.parse

LAN_CURL_FALLBACK_NOTICE = "LAN via curl fallback (Local Network permission)"

_CURL_BIN = "/usr/bin/curl"
_CURL_UNUSABLE_HINT = (" — /usr/bin/curl fallback unavailable: restore it, or grant "
                       "macOS Local Network to this interpreter")

# One-shot ticket: filled at import, consumed by the first successful fallback.
# Consume-once is the whole point — `cluster monitor --live` re-renders every
# tick and the daemon loop ticks forever, so the notice must not repeat. A
# maxsize-1 Queue rather than a bool because the monitor scrapes backends from
# a thread pool, where a read-then-write flag would let two workers announce.
_CURL_FALLBACK_TICKET: "queue.Queue" = queue.Queue(maxsize=1)
_CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)

# 100.64.0.0/10 is CGNAT — where Tailscale hands out addresses — and Python
# does NOT classify it private, so it is named explicitly. fe80::/10 and
# fc00::/7 cover IPv6 link-local and ULA.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_ULA_NET = ipaddress.ip_network("fc00::/7")


def _lan_private_host(host) -> bool:
    """True for a private/LAN host the Local Network gate can deny.

    RFC1918, 100.64.0.0/10 (tailnet CGNAT), 169.254.0.0/16 + fe80::/10
    link-local, fc00::/7 ULA, and ``.local`` mDNS names. Loopback — numeric or
    ``localhost`` — is False: TCC does not gate it, so errno 65 there is not
    this bug. A name that is not an IP is False unless it is ``.local``, so a
    public hostname can never reach the fallback.
    """
    h = (host or "").strip().rstrip(".").lower()
    if not h or h == "localhost":
        return False
    if h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    # Family guard: `IPv4Address in <IPv6Network>` raises TypeError, so a
    # membership test must only run against the network of the same family.
    if ip.version == 4:
        return bool(ip.is_private or ip.is_link_local or ip in _CGNAT_NET)
    return bool(ip.is_private or ip.is_link_local or ip in _ULA_NET)


def _lan_errno(exc):
    """The errno behind a urllib failure, or None when it carries none.

    ``URLError.reason`` is the OSError raised by the connect. ``HTTPError`` is
    a URLError subclass whose reason is a *status string*, so it lands here as
    None — a status is never a connectivity denial, and never a fallback.
    """
    return getattr(getattr(exc, "reason", None), "errno", None)


def _lan_curl_notice_once() -> None:
    """Print the fallback notice the first time only, then stay quiet.

    stderr, not stdout: the two probe mirrors print structured output on stdout
    (doctor's checklist, template's ``--json``) and must not be polluted, and
    the daemon log picks stderr up — one line, then silence.
    """
    import sys
    try:
        _CURL_FALLBACK_TICKET.get_nowait()
    except queue.Empty:
        return
    print(LAN_CURL_FALLBACK_NOTICE, file=sys.stderr, flush=True)


def _lan_curl_reset_notice() -> None:
    """Refill the one-shot notice ticket.

    Test seam only — no runtime path calls it. A test that asserts 'the notice
    appears exactly once across N fallbacks' has to start from a known state,
    and the suite raises far more than one fallback per process, so without
    this the assertion would silently depend on test order.
    """
    while not _CURL_FALLBACK_TICKET.empty():
        _CURL_FALLBACK_TICKET.get_nowait()
    _CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)


def _lan_curl_run(argv, timeout):
    """Run one curl argv LIST (never a shell) -> (returncode, stdout, stderr).

    Injectable seam: the suite replaces this module attribute with a stub that
    inspects argv — which is also how 'the api_key never reaches argv' is
    proven, since every test asserts on the argv the stub was handed.
    """
    import subprocess
    p = subprocess.run(argv, capture_output=True, timeout=timeout + 10)
    return p.returncode, p.stdout, p.stderr


def _lan_curl_unusable(original, cause) -> Exception:
    """What the caller sees when curl could not be run at all.

    Same ``URLError`` family the caller already handles, and its text still
    carries the ORIGINAL errno-65 message — the hint is appended, never
    substituted, so a reader still learns the endpoint was denied.
    """
    return urllib.error.URLError(f"{original}{_CURL_UNUSABLE_HINT} [{cause}]")


def _lan_curl_argv(url, timeout, header_file=None) -> list:
    """The curl argv.

    ``-sS`` quiet but loud on error, ``--fail`` so a 4xx/5xx becomes exit 22
    (urllib's HTTPError equivalent), ``-L`` because urllib follows redirects
    too, ``--max-time`` so a hung node cannot hang the monitor. The URL is
    always the LAST token, and no token ever holds a secret.
    """
    argv = [_CURL_BIN, "-sS", "--fail", "-L", "--max-time", str(timeout)]
    if header_file:
        argv += ["-H", f"@{header_file}"]
    argv.append(url)
    return argv


def _lan_curl_write_header(api_key) -> str:
    """Write the Bearer header to a 0600 temp file and return its path.

    The api_key must NEVER be an argv token: ``ps`` shows every argument of a
    child process for the life of the request, so ``-H "Authorization: Bearer
    <key>"`` would leak the key to anyone on the box. curl reads the header
    from the file instead (``-H @path``), and the caller unlinks it.
    """
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix="hscc-lan-hdr-")
    try:
        os.fchmod(fd, 0o600)  # mkstemp is 0600 already; pin it explicitly
        with os.fdopen(fd, "w") as fh:
            fh.write(f"Authorization: Bearer {api_key}\n")
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _lan_curl_http_error(url, stderr) -> Exception:
    """Map curl's exit-22 back onto urllib's HTTPError family.

    Parity matters more than the number: callers branch on HTTPError to say
    'endpoint reachable, probe/auth wrong' as opposed to 'unreachable', so the
    fallback raises the same family urllib would, carrying the status curl
    reported. ``code`` is 0 when curl did not name one — the exception TYPE is
    what callers branch on.
    """
    import re
    m = re.search(r"returned error: (\d{3})",
                  (stderr or b"").decode("utf-8", "replace"))
    code = int(m.group(1)) if m else 0
    reason = f"curl fallback: HTTP {code}" if code else "curl fallback: HTTP error"
    return urllib.error.HTTPError(url, code, reason, None, None)  # type: ignore[arg-type]


def _lan_curl_fallback(url, timeout, exc, api_key=None):
    """Retry one denied GET through /usr/bin/curl -> the body bytes, or None.

    Returns the response body when the retry succeeded; None means 'not
    eligible, or curl could not reach it either' and the caller MUST re-raise
    the original error unchanged. ``api_key`` is only ever sent as a header
    from a 0600 temp file — never in argv, never in a message.
    """
    import os
    if _lan_errno(exc) != 65:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not _lan_private_host(parsed.hostname):
        return None
    header_file = None
    try:
        if api_key:
            header_file = _lan_curl_write_header(api_key)
        rc, out, err = _lan_curl_run(_lan_curl_argv(url, timeout, header_file),
                                     timeout)
    except Exception as cause:      # curl missing / not executable / timed out
        raise _lan_curl_unusable(exc, cause) from exc
    finally:
        if header_file:
            try:
                os.unlink(header_file)
            except OSError:
                pass
    if rc == 22:
        raise _lan_curl_http_error(url, err) from exc
    if rc != 0:
        # curl could not reach it either — it really is down. Let the original
        # errno-65 error stand with the caller's own wording.
        return None
    _lan_curl_notice_once()
    return out
# ── END mirrored block: lan_curl_fallback ──────────────────────────────────

def _http_get(url: str, timeout: float) -> bytes:
    """GET ``url`` -> body bytes.

    A macOS Local-Network denial (errno 65 on a private host) retries once via
    ``_lan_curl_fallback``; everything else — other errnos, HTTPError, timeouts
    — propagates unchanged so 'down' still means down.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        body = _lan_curl_fallback(url, timeout, e)
        if body is None:
            raise
        return body


class HostStream:
    """Keeps the newest frame from a long-running `sparkrun cluster monitor --json`."""

    def __init__(self, interval: int, cluster: str | None = None, popen=subprocess.Popen):
        args = ["sparkrun", "cluster", "monitor", "--json", "--interval", str(interval)]
        if cluster:
            args += ["--cluster", cluster]
        self.frame: dict | None = None
        self.error: str | None = None
        try:
            self._proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, bufsize=1)
        except (OSError, ValueError) as e:
            self._proc = None
            self.error = f"sparkrun monitor did not start: {e}"
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self._proc.stdout:
            try:
                self.frame = json.loads(line)
                self.error = None
            except json.JSONDecodeError:
                continue
        self.error = "sparkrun monitor exited"

    def close(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class ServingPoller:
    """Per-backend vLLM load, keyed by node:port, plus the proxy's route map."""

    def __init__(self, get=_http_get, clock=time.monotonic,
                 proxy_url: str | None = None):
        self._get = get
        self._clock = clock
        self._proxy_url = proxy_url or f"http://localhost:{PROXY_PORT}/v1/model/info"
        self.routes: dict = {}
        self.proxy_error: str | None = None
        self._routes_at = None
        self._prev: dict = {}      # hp -> (t, {model: metrics})
        self._pool = ThreadPoolExecutor(max_workers=8)

    def _refresh_routes(self, now):
        if self._routes_at is not None and now - self._routes_at < BACKENDS_REFRESH_S:
            return
        try:
            self.routes = parse_model_info(json.loads(self._get(self._proxy_url, 5)))
            self.proxy_error = None
        except Exception as e:  # keep the last known routes on a blip
            self.proxy_error = str(e) or type(e).__name__
        self._routes_at = now

    def _scrape(self, hp):
        try:
            return hp, parse_vllm_metrics(self._get(f"http://{hp}/metrics", 3).decode("utf-8", "replace")), None
        except Exception as e:
            return hp, None, str(e) or type(e).__name__

    def poll(self) -> list:
        """One sample: a row per (backend, model) with load + token rates."""
        now = self._clock()
        self._refresh_routes(now)
        rows = []
        for hp, metrics, err in self._pool.map(self._scrape, sorted(self.routes)):
            node, _, port = hp.rpartition(":")
            base = {"node": node, "port": port, "routes": self.routes.get(hp, [])}
            if metrics is None:
                rows.append({**base, "model": "-", "error": err})
                self._prev.pop(hp, None)
                continue
            prev_t, prev_m = self._prev.get(hp, (None, {}))
            for model, m in sorted(metrics.items()):
                gen, prompt = rates(prev_m.get(model), m,
                                    now - prev_t if prev_t is not None else 0)
                rows.append({**base, "model": model,
                             "running": int(m.get("running", 0)),
                             "waiting": int(m.get("waiting", 0)),
                             "kv": m.get("kv"), "gen_tps": gen, "prompt_tps": prompt,
                             # cumulative since the engine started (vLLM counters)
                             "prompt_total": m.get("prompt_total"),
                             "gen_total": m.get("gen_total")})
            self._prev[hp] = (now, metrics)
        return rows

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


# ── rendering ──────────────────────────────────────────────────────────────

def _cell(v, fmt="{}", suffix=""):
    if v in (None, ""):
        return "-"
    try:
        return fmt.format(float(v)) + suffix
    except (TypeError, ValueError):
        return esc(v)


def _swap_pct(s: dict) -> str:
    try:
        total = float(s.get("swap_total_mb") or 0)
        return f"{100 * float(s.get('swap_used_mb') or 0) / total:.0f}%" if total else "-"
    except (TypeError, ValueError):
        return "-"


def _plain_table(**kw) -> Table:
    # sparkrun's monitor is a borderless grid; match it, keep the hscc palette.
    return Table(box=None, pad_edge=False, header_style="accent", padding=(0, 2, 0, 0), **kw)


def host_table(frame: dict | None) -> Table:
    t = _plain_table()
    for col in ("Host", "Jobs", "CPU%", "RAM%", "Swap%", "GPU%",
                "CPU Temp", "GPU Temp", "GPU Power"):
        t.add_column(col, justify="left" if col == "Host" else "right")
    for h in (frame or {}).get("hosts") or []:
        s = h.get("sample") or {}
        if h.get("error") and not s:
            t.add_row(esc(h.get("host")), "-", Text(esc(h["error"]), style="error"),
                      *["-"] * 6)
            continue
        t.add_row(esc(h.get("host")), _cell(s.get("sparkrun_jobs"), "{:.0f}"),
                  _cell(s.get("cpu_usage_pct"), "{:.1f}"),
                  _cell(s.get("mem_used_pct"), "{:.1f}"), _swap_pct(s),
                  _cell(s.get("gpu_util_pct"), "{:.0f}"),
                  _cell(s.get("cpu_temp_c"), "{:.1f}", " C"),
                  _cell(s.get("gpu_temp_c"), "{:.0f}", " C"),
                  _cell(s.get("gpu_power_w"), "{:.2f}", " W"))
    return t


def _load_style(running: int, waiting: int) -> str:
    if waiting:
        return "warn"
    return "ok" if running else "dim"


WIDE_MIN = 110  # below this the route and prompt-rate columns are dropped


def _alias(name: str) -> str:
    # 'orchestrator-model' -> 'orchestrator': every proxy alias ends in -model.
    return name[:-len("-model")] if name.endswith("-model") else name


def serving_table(rows: list, width: int | None = None) -> Table:
    wide = width is None or width >= WIDE_MIN
    cols = [("Node", "left", 15), ("Port", "right", 4), ("Model", "left", None)]
    if wide:
        cols.append(("Routes", "left", None))
    cols += [("Run", "right", 3), ("Wait", "right", 4), ("KV%", "right", 5),
             ("In", "right", 6), ("Out", "right", 6), ("Gen t/s", "right", 7)]
    if wide:
        cols.append(("Prompt t/s", "right", 10))
    t = _plain_table()
    # Node/port/numbers never shrink; model and route names get ellipsised.
    for col, just, w in cols:
        t.add_column(col, justify=just, no_wrap=True, overflow="ellipsis",
                     min_width=w, max_width=None if w else 26)

    def row(node, port, model, routes, *rest):
        cells = [node, port, model] + ([routes] if wide else []) + list(rest[:6])
        if wide:
            cells.append(rest[6])
        t.add_row(*cells)

    totals: dict[str, list] = {}
    for r in rows:
        routes = ", ".join(_alias(a) for a in r["routes"] if a != r["model"]) or "-"
        # 'org/Model-Name' -> 'Model-Name': the org prefix eats the column.
        short = r["model"].rsplit("/", 1)[-1]
        if r.get("error"):
            row(esc(r["node"]), esc(r["port"]), Text("down", style="error"), esc(routes),
                Text(esc(r["error"])[:30], style="error"), "", "", "", "", "", "")
            continue
        run, wait = r["running"], r["waiting"]
        tot = totals.setdefault(short, [0, 0, None, 0, None, None])
        tot[0] += run
        tot[1] += wait
        if r["gen_tps"] is not None:
            tot[2] = (tot[2] or 0.0) + r["gen_tps"]
        tot[3] += 1
        for i, key in ((4, "prompt_total"), (5, "gen_total")):
            if r.get(key) is not None:
                tot[i] = (tot[i] or 0.0) + r[key]
        row(esc(r["node"]), esc(r["port"]), esc(short), esc(routes),
            Text(str(run), style=_load_style(run, wait)),
            Text(str(wait), style="warn" if wait else "dim"),
            _cell(None if r["kv"] is None else 100 * r["kv"], "{:.1f}"),
            Text(fmt_tokens(r.get("prompt_total")), style="dim"),
            Text(fmt_tokens(r.get("gen_total")), style="dim"),
            _cell(r["gen_tps"], "{:.1f}"), _cell(r["prompt_tps"], "{:.0f}"))
    for model, (run, wait, gen, n, tin, tout) in sorted(totals.items()):
        row(Text(f"total ({n})", style="label"), "", esc(model), "",
            Text(str(run), style="label"), Text(str(wait), style="label"), "",
            Text(fmt_tokens(tin), style="label"), Text(fmt_tokens(tout), style="label"),
            Text("-" if gen is None else f"{gen:.1f}", style="label"), "")
    return t


def render(frame, rows, interval, proxy_error=None, host_error=None,
           snapshot=False, width=None) -> Group:
    n = len((frame or {}).get("hosts") or [])
    when = "snapshot (use --live to refresh)" if snapshot else f"every {interval}s"
    parts = [Text(f"hscc cluster monitor — {n} host(s) — {when}", style="title"),
             host_table(frame)]
    if host_error:
        parts.append(Text(esc(host_error), style="error"))
    parts.append(Text(""))
    head = Text(f"serving — proxy :{PROXY_PORT}", style="title")
    head.append("   In/Out = tokens since each engine started", style="dim")
    parts.append(head)
    if proxy_error:
        parts.append(Text(f"proxy /v1/model/info unreachable: {esc(proxy_error)}"
                          + (" (showing last known routes)" if rows else ""), style="warn"))
    parts.append(serving_table(rows, width) if rows else Text("no backends behind the proxy", style="dim"))
    if not snapshot:
        parts.append(Text("Ctrl-C to quit", style="dim"))
    return Group(*parts)


# ── entry ──────────────────────────────────────────────────────────────────

def run_live(console, interval: int = DEFAULT_INTERVAL, cluster: str | None = None) -> int:
    from rich.live import Live

    hosts = HostStream(interval, cluster)
    serving = ServingPoller()
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                rows = serving.poll()
                live.update(render(hosts.frame, rows, interval, serving.proxy_error,
                                   hosts.error, width=console.width), refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        hosts.close()
        serving.close()
