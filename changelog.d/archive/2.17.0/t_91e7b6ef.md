kind: Fixed
task: t_91e7b6ef

* The live session WebSocket now answers client pings. `ws_frame.iter_data_messages`
  dropped control frames while reassembling data, so the `OP_PING` branch in
  `routes_ws._drain_socket` was dead: a client keepalive ping was never ponged and
  a strict client eventually closed the stream. A new `ws_frame.iter_frames` yields
  ping/pong/close in arrival order (data messages still reassembled);
  `_drain_socket` consumes it, echoing a pong for every ping and completing the
  close handshake. `iter_data_messages` keeps its existing data-only contract.
* `test_detector_matches_only_live_cli_surface` is hermetic: it monkeypatches
  `hermes_cli.profiles.profile_exists` and points `HERMES_HOME` at `tmp_path`
  instead of depending on a real `hscc-orch` profile on the host. The production
  `detect_active_cli_owner` check is unchanged.
---
kind: Verified
task: t_91e7b6ef

* Gate under `~/.hermes/hermes-agent/venv/bin/python`:
  `test_ws_frame.py` + `test_ws_route.py` + `test_send_working_indicator.py`
  = **40 passed**. The new real-client ping test
  (`test_client_ping_gets_pong`, the `websockets` client) timed out against the
  pre-fix tree; the detector test failed with an empty `HERMES_HOME` before the
  hermeticity fix.
* `hscc api --help` smoke-tested with an isolated `HSCC_HOME` (argparse prints
  the unchanged verb help); no server was started.
* `python3 scripts/address_guard.py --tracked` clean.
