#!/usr/bin/env python3
"""Mini probe: verify WS auth + /api/events channel semantics against the
isolated hermes serve on port 9211. No PTY yet — just prove token auth works
and channel subscribe/publish round-trips through /api/pub -> /api/events."""
import asyncio, json, sys
import websockets

HOST = "127.0.0.1"
PORT = 9211
TOKEN = "iso_probe_token_7f3a9c2e"
CHANNEL = "probechan_a1b2c3"

async def main():
    # 1) Open /api/events subscriber on the channel, with the token.
    ev_url = f"ws://{HOST}:{PORT}/api/events?token={TOKEN}&channel={CHANNEL}"
    print("subscribing:", ev_url)
    sub = await websockets.connect(ev_url)
    print("  /api/events connected OK")

    # 2) Open /api/pub publisher on the SAME channel, with the token, and
    #    publish a synthetic frame. This simulates the PTY-side gateway.
    pub_url = f"ws://{HOST}:{PORT}/api/pub?token={TOKEN}&channel={CHANNEL}"
    print("publishing:", pub_url)
    pub = await websockets.connect(pub_url)
    print("  /api/pub connected OK")

    frame = {"jsonrpc": "2.0", "method": "event",
             "params": {"type": "tool.start", "session_id": "sess_1",
                        "payload": {"tool_id": "tc_1", "name": "read_file", "context": "probe"}}}
    await pub.send(json.dumps(frame))
    print("  published synthetic tool.start")

    # 3) Expect the subscriber to receive it.
    try:
        got = await asyncio.wait_for(sub.recv(), timeout=3.0)
        print("  SUBSCRIBER GOT:", got[:200])
        print("  => /api/pub -> /api/events fan-out on same channel: VERIFIED")
    except asyncio.TimeoutError:
        print("  !! subscriber got nothing — fan-out FAILED")
    await sub.close(); await pub.close()

    # 4) Auth negative test: wrong token should be refused
    try:
        bad = await websockets.connect(
            f"ws://{HOST}:{PORT}/api/events?token=WRONG&channel={CHANNEL}")
        print("  !! wrong token ACCEPTED (unexpected)")
        await bad.close()
    except Exception as e:
        print(f"  wrong token refused as expected: {type(e).__name__}")

asyncio.run(main())
