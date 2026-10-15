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

def _http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


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
                             "kv": m.get("kv"), "gen_tps": gen, "prompt_tps": prompt})
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
             ("Gen t/s", "right", 7)]
    if wide:
        cols.append(("Prompt t/s", "right", 10))
    t = _plain_table()
    # Node/port/numbers never shrink; model and route names get ellipsised.
    for col, just, w in cols:
        t.add_column(col, justify=just, no_wrap=True, overflow="ellipsis",
                     min_width=w, max_width=None if w else 26)

    def row(node, port, model, routes, *rest):
        cells = [node, port, model] + ([routes] if wide else []) + list(rest[:4])
        if wide:
            cells.append(rest[4])
        t.add_row(*cells)

    totals: dict[str, list] = {}
    for r in rows:
        routes = ", ".join(_alias(a) for a in r["routes"] if a != r["model"]) or "-"
        # 'org/Model-Name' -> 'Model-Name': the org prefix eats the column.
        short = r["model"].rsplit("/", 1)[-1]
        if r.get("error"):
            row(esc(r["node"]), esc(r["port"]), Text("down", style="error"), esc(routes),
                Text(esc(r["error"])[:30], style="error"), "", "", "", "")
            continue
        run, wait = r["running"], r["waiting"]
        tot = totals.setdefault(short, [0, 0, None, 0])
        tot[0] += run
        tot[1] += wait
        if r["gen_tps"] is not None:
            tot[2] = (tot[2] or 0.0) + r["gen_tps"]
        tot[3] += 1
        row(esc(r["node"]), esc(r["port"]), esc(short), esc(routes),
            Text(str(run), style=_load_style(run, wait)),
            Text(str(wait), style="warn" if wait else "dim"),
            _cell(None if r["kv"] is None else 100 * r["kv"], "{:.1f}"),
            _cell(r["gen_tps"], "{:.1f}"), _cell(r["prompt_tps"], "{:.0f}"))
    for model, (run, wait, gen, n) in sorted(totals.items()):
        row(Text(f"total ({n})", style="label"), "", esc(model), "",
            Text(str(run), style="label"), Text(str(wait), style="label"), "",
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
    parts.append(Text(f"serving — proxy :{PROXY_PORT}", style="title"))
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
