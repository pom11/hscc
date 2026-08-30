#!/usr/bin/env python3
"""Instrumented real-driver integration: capture any thread exception + log."""
import json, sys, time, logging
from collections import Counter
sys.path.insert(0, "..")
logging.basicConfig(level=logging.DEBUG)
from session_event import reset_stores, get_store
from gateway_driver import GatewayConfig, GatewayDriver

reset_stores()
cfg = GatewayConfig(host="127.0.0.1", port=9211, token="iso_probe_token_7f3a9c2e", project="hscc")
drv = GatewayDriver(cfg)

# Wrap reader threads with exception capture (call the ORIGINAL methods).
_orig_events = drv._run_events_loop
_orig_pty = drv._run_pty_loop
results = {}
def _wrapped_events():
    try:
        _orig_events()
    except BaseException as e:
        results['events'] = repr(e)
def _wrapped_pty():
    try:
        _orig_pty()
    except BaseException as e:
        results['pty'] = repr(e)

drv._run_events_loop = _wrapped_events
drv._run_pty_loop = _wrapped_pty
drv.start()
print("connected:", drv._pty is not None, drv._events is not None)
# Let the TUI boot (node startup) before typing, mirroring the proven probe.
time.sleep(10)
ok = drv.send_user_message("read /tmp/probe_gateway_tool.txt")
print("sent:", ok)
time.sleep(35)
print("thread exceptions:", results)
store = get_store("hscc")
print("store count:", store.history(limit=0)["next_seq"]-1)
drv.stop()
