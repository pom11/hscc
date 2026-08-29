#!/usr/bin/env python3
"""Probe 3: drive a tool call through /api/pty and capture the real tool.start
/ tool.complete / message.delta frames via /api/events. Prompt forces a tool
call (read a known file) rather than a free-form reply."""
import asyncio, json, time
import websockets

HOST, PORT = "127.0.0.1", 9211
TOKEN = "iso_probe_token_7f3a9c2e"
CHANNEL = "probechan_tool_a1c3e5"
PROMPT = "Use the read_file tool to read /tmp/probe_gateway_tool.txt and tell me the first word."

async def main():
    ev_url = f"ws://{HOST}:{PORT}/api/events?token={TOKEN}&channel={CHANNEL}"
    pty_url = (f"ws://{HOST}:{PORT}/api/pty?token={TOKEN}&channel={CHANNEL}"
               f"&profile=default")
    print("subscribing:", ev_url)
    sub = await websockets.connect(ev_url)
    print("opening pty:", pty_url)
    pty = await websockets.connect(pty_url, max_size=None)
    print("pty connected — TUI spawning...\n")

    notifs = []
    def record_notif(raw):
        # keep only JSON (ignore ANSI bytes)
        notifs.append(raw)

    async def reader(ws, tag):
        try:
            while True:
                m = await ws.recv()
                if isinstance(m, str):
                    record_notif(m)
        except Exception as e:
            pass

    t1 = asyncio.create_task(reader(sub, "EVENT"))
    await asyncio.sleep(6)  # let the TUI boot
    print("boot done — typing prompt (forces a tool call)...")
    for ch in PROMPT:
        await pty.send(ch.encode())
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)
    await pty.send(b"\r")   # Enter
    print("sent prompt — waiting for tool round-trip...")
    await asyncio.sleep(40)  # generous for model + tool call

    # classify
    events = [json.loads(n) for n in notifs if '"method": "event"' in n or '"method":"event"' in n]
    print(f"\n=== captured {len(events)} event notifications ===")
    from collections import Counter
    types = Counter(e.get("params", {}).get("type") for e in events)
    for t, c in types.items():
        print(f"  {t}: {c}")

    with open("probe_03_tool_frames.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    print("\nwrote probe_03_tool_frames.jsonl")
    t1.cancel()

asyncio.run(main())
