#!/usr/bin/env python3
"""Probe 2: open /api/pty to spawn the real TUI, subscribe to /api/events on
the same channel, capture real Hermes frames as the TUI boots and during a
prompt. Writer sends raw bytes to the PTY (the same keystrokes a user would)."""
import asyncio, json, sys, time
import websockets

HOST, PORT = "127.0.0.1", 9211
TOKEN = "iso_probe_token_7f3a9c2e"
CHANNEL = "probechan_pty_e5g7i9"

async def main():
    ev_url = f"ws://{HOST}:{PORT}/api/events?token={TOKEN}&channel={CHANNEL}"
    pty_url = (f"ws://{HOST}:{PORT}/api/pty?token={TOKEN}&channel={CHANNEL}"
               f"&profile=default")
    print("subscribing events:", ev_url)
    sub = await websockets.connect(ev_url)
    print("events connected OK")
    print("opening pty:", pty_url)
    pty = await websockets.connect(pty_url, max_size=None)
    print("pty connected OK — TUI spawning...\n")

    frames = []
    def log(tag, data):
        line = f"[{tag}] {data}"
        frames.append(line)
        print(line[:300])

    async def reader(ws, tag):
        try:
            while True:
                m = await ws.recv()
                if isinstance(m, bytes):
                    log(tag, "<BYTES " + repr(m[:140]))
                else:
                    log(tag, m[:400])
        except Exception as e:
            log(tag, f"!! closed: {e}")

    t1 = asyncio.create_task(reader(sub, "EVENT"))
    t2 = asyncio.create_task(reader(pty, "PTY "))
    await asyncio.sleep(6)
    print("\n--- boot phase done, typing 'say hi' char-by-char + Enter ---")
    for ch in "say hi":
        await pty.send(ch.encode())
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    await pty.send(b"\r")  # Enter in raw TTY mode
    await asyncio.sleep(18)
    print(f"\n=== captured {len(frames)} frames ===")
    t1.cancel(); t2.cancel()
    with open("probe_02_captured_raw.txt", "w") as f:
        f.write("\n".join(frames))
    print("wrote probe_02_captured_raw.txt")

asyncio.run(main())
