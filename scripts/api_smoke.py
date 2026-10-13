#!/usr/bin/env python3
"""hscc api smoke <project> — end-to-end read+write proof for the iOS chat path.

WHAT IT PROVES (and why it exists): every unit test of the read path used
injected fakes; the operator's real failure (message reaches the terminal REPL
but nothing streams back, banner stuck on "Connecting…") lived in the seam
between the tmux REPL, state.db, the store-tail, and the WS fan-out. This
script exercises that seam the way the phone does — a real WS client against a
real API server — and exits non-zero on any contract violation.

Usage:
  hscc api smoke <project> [--text T] [--timeout S] [--url ws-url]
  (dev: python3 scripts/api_smoke.py <project> --registry <registry.yaml>)

CONTRACT asserted, in order:
  1. WS connects and the FIRST frame is the hello envelope with next_seq;
  2. sending {"kind":"send","text":T} appends a role=user echo frame (the
     app-visible echo) — seen live or via a fresh reconnect's replay;
  3. a system working frame with details.via == "tmux" arrives for T;
  4. an assistant message frame whose text matches the REPL's reply arrives
     WITHOUT reconnecting (store-tail liveness), within --timeout;
  5. frames arrive in strictly increasing seq order (no duplicate seqs);
  6. exactly ONE user frame for T (the 116+233 duplicate-message defect).

The reply is asked for by rule: the sent text itself instructs the REPL
("reply with exactly: SMOKE <nonce>") so the assertion is exact, not fuzzy.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

import websockets


def _token(hscc_dir):
    path = os.path.join(os.path.expanduser(hscc_dir), "api-token")
    with open(path) as f:
        return f.read().strip()


class Stream:
    """One WS connection; collects frames until a stop condition."""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.frames = []          # every frame ever seen, across connections
        self.ws = None

    async def connect(self):
        self.ws = await websockets.connect(
            self.url,
            additional_headers={"Authorization": "Bearer " + self.token},
            ping_interval=None)

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def pump(self, seconds):
        """Read frames for up to ``seconds`` of quiet; extend self.frames."""
        quiet = 1.5
        deadline = time.time() + seconds
        last = time.time()
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=quiet)
            except asyncio.TimeoutError:
                return
            except websockets.exceptions.WebSocketException:
                return
            self.frames.append(json.loads(raw))
            last = time.time()
            del last

    def first(self, pred, since=0):
        for f in self.frames[since:]:
            if pred(f):
                return f
        return None


def _is_hello(f):
    return f.get("type") == "hello"


def _msg(f, role=None, text=None):
    if f.get("type") != "message":
        return False
    p = f.get("payload") or {}
    if role is not None and p.get("role") != role:
        return False
    if text is not None and (p.get("delta") or "") != text:
        return False
    return True


def _working_tmux(f, text):
    if f.get("type") != "system":
        return False
    p = f.get("payload") or {}
    d = p.get("details") or {}
    return (p.get("kind") == "working" and d.get("via") == "tmux"
            and d.get("text") == text)


def _fail(step, detail):
    print(f"FAIL [{step}]: {detail}", file=sys.stderr)
    sys.exit(1)


async def run(args):
    token = args.token or _token(args.hscc_dir)
    url = args.url or (f"ws://127.0.0.1:{args.port}"
                       f"/v1/projects/{args.project}/session/ws")
    nonce = f"SMOKE {int(time.time()) % 100000:05d}"
    text = (args.text or
            f"Reply with exactly one word: {nonce}. No tools, no punctuation.")

    s = Stream(url, token)

    # -- 1. connect + hello first ------------------------------------------ #
    await s.connect()
    t0 = time.time()
    await s.pump(5)
    if not s.frames or not _is_hello(s.frames[0]):
        _fail("hello", f"first frame is not hello: {s.frames[:1]}")
    hello_seq = (s.frames[0].get("payload") or {}).get("next_seq")
    mark = len(s.frames)
    print(f"ok hello: next_seq={hello_seq}")

    # -- 2. send ------------------------------------------------------------ #
    await s.ws.send(json.dumps({"kind": "send", "text": text}))
    print(f"sent: {text[:60]}…")

    # -- 3./4. wait for echo + working(via=tmux) + assistant reply ---------- #
    deadline = time.time() + args.timeout
    echo = working = reply = None
    while time.time() < deadline:
        await s.pump(deadline - time.time())
        echo = echo or s.first(lambda f: _msg(f, "user", text), mark)
        working = working or s.first(lambda f: _working_tmux(f, text), mark)
        reply = reply or s.first(
            lambda f: _msg(f, "assistant")
            and nonce in (f["payload"].get("delta") or ""), mark)
        if echo and working and reply:
            break
        if reply and not (echo and working):
            # Everything we NEED may still be arriving (working could be
            # slightly late); keep pumping until deadline for it.
            pass

    if echo is None:
        # Rule 2 fallback: reconnect and check the STORE (replay) instead of
        # live arrival — the echo must at least be durable.
        await s.close()
        await s.connect()
        await s.pump(5)
        echo = s.first(lambda f: _msg(f, "user", text), mark)
        if echo is None:
            _fail("echo", "user echo for the sent text never arrived "
                          "(live nor via replay)")
        print("ok echo: seen via replay after reconnect")
    else:
        print(f"ok echo: seq={echo.get('seq')}")
    if working is None:
        _fail("working", f"no system working frame with via=tmux for the "
                        f"sent text within {args.timeout}s")
    wdet = ((working.get("payload") or {}).get("details") or {})
    print("ok working: via=tmux pane=%s" % wdet.get("pane"))
    if reply is None:
        _fail("reply", "assistant frame containing %r never arrived within "
                      "%.0fs (store-tail dead? REPL down?)"
                      % (nonce, args.timeout))
    print(f"ok reply: seq={reply.get('seq')} ts={reply.get('ts')}")

    # -- 5. seq monotonic + 6. single user frame ---------------------------- #
    seqs = [f["seq"] for f in s.frames[mark:] if isinstance(f.get("seq"), int)]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        _fail("order", f"frames not strictly seq-increasing after send: {seqs}")
    user_frames = [f for f in s.frames[mark:] if _msg(f, "user", text)]
    if len(user_frames) != 1:
        _fail("dedupe", f"expected exactly 1 user frame for the sent text, "
                        f"saw {len(user_frames)} (the 116+233 defect)")
    print(f"ok order+dedupe: {len(seqs)} frames, single user echo")

    elapsed = time.time() - t0
    print("PASS: full round trip in %.1fs "
          "(echo -> via=tmux -> assistant %r, in-order, no dupes)"
          % (elapsed, nonce))
    await s.close()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="hscc api smoke")
    ap.add_argument("project")
    ap.add_argument("--text", default=None)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--url", default=None,
                    help="full ws url (default ws://127.0.0.1:<port>/…)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--hscc-dir", default="~/.hscc")
    ap.add_argument("--token", default=None,
                    help="API token (default: read <hscc-dir>/api-token)")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
