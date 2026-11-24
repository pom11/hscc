#!/usr/bin/env python3
"""Measure the payload key set of a read route by calling its handler directly.

Hermetic, the way the sibling suite is hermetic:
  * HOME / HSCC_HOME / HERMES_HOME point into a throwaway dir, so no real
    ~/.hscc and no real ~/.hermes/profiles is read;
  * a tripwire replaces socket.socket and subprocess.Popen so any attempt to
    reach the fleet or spawn a process is RECORDED and refused rather than
    performed. Anything the tripwire catches is reported: a key set measured
    behind a suppressed network read is not trustworthy and must be labelled.

Two measurements are reported per route, because they answer different
questions:
  real  -- handler called with its real module code and the sealed HOME; this is
           the shape a live client actually sees on a degraded/idle host.
  stub  -- handler called with its `_backing_*` seam replaced by a probe dict;
           for splat-style handlers (`return 200, {**data, ...}`) this measures
           the PROBE plus the handler's own added keys, which is the handler's
           *contribution*, not the route's shape. Both are printed deliberately.
"""
import inspect
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile

_home = tempfile.mkdtemp(prefix="t_b9808146-live-")
os.environ.update(HOME=_home, HSCC_HOME=_home + "/.hscc", HERMES_HOME=_home + "/.hermes")
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

REPO = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.join(REPO, "hscc-api"))

TRIPPED = []
_real_socket = socket.socket
_real_popen = subprocess.Popen


class _BlockedSocket(_real_socket):
    def __init__(self, *a, **k):
        TRIPPED.append("socket %s" % (a[:2],))
        raise OSError("nolive: socket blocked")

    def connect(self, *a, **k):
        TRIPPED.append("connect %s" % (a[:1],))
        raise OSError("nolive: connect blocked")


def _blocked_popen(*a, **k):
    TRIPPED.append("popen %s" % (str(a[:1])[:120],))
    raise OSError("nolive: subprocess blocked")


socket.socket = _BlockedSocket
subprocess.Popen = _blocked_popen

import api_server  # noqa: E402

TARGETS = json.load(open(sys.argv[2]))
PROBE = {"probe_alpha": 1, "probe_beta": [2], "probe_gamma": {"d": 3}}


class Ctx:
    """Minimal stand-in for the server's request context."""
    def __init__(self):
        self.headers = {}
        self.body = b""
        self.path = "/"
        self.method = "GET"


def top_keys(payload):
    if isinstance(payload, dict):
        return sorted(payload.keys())
    return ["<%s>" % type(payload).__name__]


routes = {}
for m, pat, h in api_server.ROUTES:
    routes[(m, pat.pattern)] = h

out = []
for t in TARGETS:
    mod = __import__(t["module"])
    hname = t["handler"]
    h = getattr(mod, hname)
    rec = {"module": t["module"], "handler": hname, "pattern": t["pattern"],
           "docstring_first_line": (inspect.getdoc(h) or "").split("\n")[0].strip()}

    for mode in ("real", "stub"):
        TRIPPED.clear()
        note = None
        if mode == "stub":
            seams = [n for n in dir(mod) if n.startswith("_backing_")]
            saved = {}
            # Stub only the seams this handler actually references.
            body = inspect.getsource(h)
            used = [s for s in seams if re.search(r"\b%s\b" % s, body)]
            for s in used:
                saved[s] = getattr(mod, s)
                setattr(mod, s, lambda *a, _s=s, **k: dict(PROBE))
            note = used
        try:
            status, payload = h(None, Ctx(), {}, b"")
            keys, st = top_keys(payload), status
        except TypeError as e:
            keys, st = None, "signature-mismatch: %s" % e
        except Exception as e:
            keys, st = None, "%s: %s" % (type(e).__name__, str(e)[:100])
        out_rec = dict(rec)
        out_rec.update({"mode": mode, "status": st, "keys": keys,
                        "tripwire": list(TRIPPED), "seams_stubbed": note})
        out.append(out_rec)
        if mode == "stub":
            for s, v in saved.items():
                setattr(mod, s, v)

print(json.dumps(out, indent=1))
