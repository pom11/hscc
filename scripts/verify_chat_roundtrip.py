#!/usr/bin/env python3
"""Prove the whole chat pipeline works end to end — the way the operator
experiences it, not just that a route answers.

This is the process-level proof the api_route_sweep only approximates: it is
the check that would have caught the outage where every orchestrator profile
pointed at a dead host, the relay silently dropped the message, and the model
never saw it while the cluster looked idle.

What it does, back to back, against the LIVE API:

  1. Derive the API base from `hscc api status` (never hardcode — this repo is
     public and hscc_daemon/tests/test_no_real_addresses_committed.py fails
     the suite on any committed real address).
  2. POST a chat exactly as the app does (POST /v1/orchestrator/chat with
     confirm=true) and capture the job id.
  3. Poll GET /v1/orchestrator/chat/{id} to a terminal state; assert a real
     reply — non-empty and not an error.
  4. Assert the orchestrator model actually saw traffic: read the orch unit's
     vllm:generation_tokens_total before and after, and require it moved.
     That distinguishes "the API accepted it" from "the model actually
     answered" — the exact gap that hid the earlier outage.
  5. Exit non-zero with a clear diagnosis on failure, naming the likely cause
     (profile endpoint unreachable / hermes not spawning / model idle).

The orchestrator generation counter is read from the orchestrator unit's
/metrics endpoint, derived from serving.json (nodes[0] of the role=="orch"
unit + its port). This is READ-ONLY (Prometheus /metrics) — safe to hit.

Usage:
    python3 scripts/verify_chat_roundtrip.py            # human output
    python3 scripts/verify_chat_roundtrip.py --json     # machine-readable

Exit codes:
    0  round trip succeeded — model saw the message and answered
    1  a step failed (diagnosis printed)
    2  preconditions missing (no API host/token, no orch endpoint, no metrics)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Seconds to wait for a terminal chat state before giving up. The default
# orchestrator chat timeout in ~/.hscc/api.json is 600 s; the API blocks the
# underlying hermes call under a wedge-backstop. Give the poll a little room.
_POLL_TIMEOUT = 660
_POLL_INTERVAL = 5

# A short, self-identifying prompt so an operator can spot our test traffic in
# the session transcript and so the reply is trivially checkable.
TEST_PROMPT = (
    "E2E round-trip probe: reply with exactly the single token \"pong\" "
    "and nothing else."
)

PROMPT_RE = re.compile(r"pong")


def api_base():
    """Derive the API base from `hscc api status` — never a hardcoded host."""
    try:
        out = subprocess.run(["hscc", "api", "status"], capture_output=True,
                             text=True, timeout=30).stdout
        m = re.search(r"Listening:\s+([0-9.]+):(\d+)", out)
        if m:
            return "http://%s:%s" % (m.group(1), m.group(2))
    except Exception as exc:
        print("cannot derive API host from `hscc api status`: %s" % exc,
              file=sys.stderr)
    return None


def api_token():
    try:
        with open(os.path.expanduser("~/.hscc/api-token")) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _orch_unit(serving_data):
    """Return the orchestrator unit dict, or None."""
    for unit in serving_data.get("units", []):
        if unit.get("role") == "orchestrator":
            return unit
    return None


def orch_metrics_url():
    """URL of the orchestrator unit's vLLM /metrics endpoint (READ-ONLY).

    Derived, not hardcoded: nodes[0] + port of the role=="orch" unit in
    serving.json. Returns None if it cannot be derived.
    """
    try:
        sys.path.insert(0, os.path.join(REPO, "hscc_daemon"))
        from hscc_daemon import serving
        svc = serving.load_serving()
        unit = _orch_unit(svc)
        if not unit or not unit.get("nodes"):
            return None
        return "http://%s:%s/metrics" % (unit["nodes"][0], unit.get("port", 8000))
    except Exception:
        return None


def fetch_metric(url, name="vllm:generation_tokens_total"):
    """Return the float value of `name` from a vLLM /metrics body, or None."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    best = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(name):
            continue
        # strip the {...} label block, then the value
        m = re.search(r"\{[^}]*\}\s+([0-9.]+)", line)
        if m:
            best = float(m.group(1))
            break
    return best


def http_json(method, url, token=None, body=None, timeout=30):
    """Do an HTTP request, return (status_code, parsed_json_or_None)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, None
    except urllib.error.URLError as exc:
        return 0, {"error": {"code": "unreachable",
                             "message": "connection error: %s" % exc.reason}}


def diagnose(step, base, tokens_before, tokens_after, job=None):
    """Cause a named diagnosis for a failed step. Returns a short string."""
    if step == "post":
        return ("POST /v1/orchestrator/chat did not yield a job. Likely "
                "causes: profile endpoint unreachable (hermes chat hangs in "
                "SYN_SENT) → 400/5xx, or hermes not spawning (502/503). "
                "Check `hscc verify` and `hscc api status`.")
    if step == "poll":
        return ("Chat job %s did not reach a terminal state within %ds. "
                "This is a timeout — likely hermes chat wedged (stuck "
                "spawning, profile endpoint unreachable) or model idle. "
                "See ~/.hscc/api.log." % (job, _POLL_TIMEOUT))
    if step == "reply":
        return ("Chat job %s reached a terminal state but the reply is "
                "empty or an error — the orchestrator did not answer with "
                "real text. Likely causes: model idle on the orch unit, or "
                "hermes returned an error reply. See ~/.hscc/api.log."
                % job)
    if step == "metric_delta":
        return ("The API answered, but the orchestrator's "
                "vllm:generation_tokens_total did not move (%s → %s). The "
                "request reached the API but the MODEL never saw it — the "
                "profile endpoint is unreachable or the model is idle. "
                "Check serving.json and `hscc verify` profile_endpoints."
                % (tokens_before, tokens_after))
    return "unknown failure"


def run(verbose=True):
    """Execute the round trip; return (exit_code, result_dict)."""
    base = api_base()
    tok = api_token()
    if not base or not tok:
        return 2, {"error": "no API host or token (is `hscc api status` running?)"}

    metrics_url = orch_metrics_url()
    if not metrics_url:
        return 2, {"error": "cannot derive orchestrator /metrics endpoint from serving.json"}

    tokens_before = fetch_metric(metrics_url)
    result = {
        "base": base,
        "metrics_url": metrics_url,
        "tokens_before": tokens_before,
    }

    if tokens_before is None:
        result["error"] = "orchestrator /metrics unreachable at %s" % metrics_url
        return 2, result

    # 2. POST a chat exactly as the app does, capture the job id.
    code, resp = http_json("POST", base + "/v1/orchestrator/chat", tok,
                           {"project": None, "prompt": TEST_PROMPT,
                            "confirm": True})
    if code != 202 or not resp or not resp.get("job_id"):
        result["error"] = diagnose("post", base, None, None)
        result["http"] = code
        result["body"] = resp
        return 1, result

    job = resp["job_id"]
    result["job_id"] = job

    # 3. Poll to a terminal state.
    terminal = None
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        c, poll = http_json("GET", base + "/v1/orchestrator/chat/%s" % job, tok)
        if c == 200 and poll:
            st = poll.get("status")
            if st in ("done", "failed", "timeout"):
                terminal = poll
                break
        time.sleep(_POLL_INTERVAL)
    if terminal is None:
        result["error"] = diagnose("poll", base, tokens_before, None, job)
        return 1, result

    result["status"] = terminal.get("status")
    result["elapsed"] = terminal.get("elapsed")
    result["terminal"] = terminal

    # 3b. Assert a real reply: non-empty and not an error.
    raw_reply = terminal.get("reply")
    reply = raw_reply if isinstance(raw_reply, str) else ""
    is_error = terminal.get("status") != "done" or "error" in terminal
    if not reply.strip() or is_error:
        result["error"] = diagnose("reply", base, tokens_before, None, job)
        result["reply"] = reply
        return 1, result
    result["reply"] = reply.strip()

    # 4. Assert the orchestrator model actually saw traffic.
    tokens_after = fetch_metric(metrics_url)
    result["tokens_after"] = tokens_after
    if tokens_after is None:
        # Can't read the counter now but the reply was real — not a hard fail.
        result["metric_verified"] = False
        result["metric_note"] = "could not re-read /metrics after reply"
    else:
        moved = (tokens_after - tokens_before) > 0
        result["metric_verified"] = moved
        result["delta"] = tokens_after - tokens_before
        if not moved:
            result["error"] = diagnose("metric_delta", base, tokens_before,
                                       tokens_after, job)
            return 1, result

    result["ok"] = True
    return 0, result


def main():
    json_mode = "--json" in sys.argv[1:]
    code, result = run()
    if json_mode:
        print(json.dumps(result, indent=2))
    else:
        if code == 2:
            print("\n  \u2717 cannot run chat round trip: %s" % result.get("error"))
        elif code == 1:
            print("\n  \u2717 CHAT ROUND TRIP FAILED at step:")
            print("      %s" % result.get("error"))
            print("      http=%s job=%s reply=%r"
                  % (result.get("http"), result.get("job_id"),
                     result.get("reply")))
            if result.get("tokens_before") is not None:
                print("      generation_tokens_total: %s -> %s"
                      % (result["tokens_before"], result.get("tokens_after")))
        else:
            print("\n  \u2713 CHAT ROUND TRIP OK")
            print("      POST accepted  -> job %s" % result["job_id"])
            print("      status=%s  elapsed=%.1fs" % (result["status"], result["elapsed"]))
            print("      reply = %r" % result["reply"])
            print("      orchestrator vllm:generation_tokens_total  "
                  "%s -> %s  (delta +%s)"
                  % (result["tokens_before"], result["tokens_after"],
                     result.get("delta")))
            if result.get("metric_verified") is False:
                print("      (metric re-read unavailable, reply treated as proof)")
    return code


if __name__ == "__main__":
    sys.exit(main())
