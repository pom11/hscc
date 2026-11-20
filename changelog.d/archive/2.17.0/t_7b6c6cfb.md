kind: Changed

- **Running, Needs Me and Merge Queue rebuilt on the Native kit (R3 / N3 /
  M1).** The Running tile leads with the longest-running card's elapsed clock,
  title and heartbeat (green under five minutes, orange after); Needs Me stays
  calm with a green "All clear" only after a successful fetch and draws the
  offline state otherwise; Merge Queue draws the head PR's rebase → gates →
  push → merge track, the waiting count, the last results, and "Merge queue off
  for <project>" when every queue is disabled. All three keep their WidgetKit
  kinds, use the shared `NativeStyleKit` timestamps and stale/offline rule, and
  the settings rows now call them Running, Needs Me and Merge Queue.

---
kind: Verified

- `ios-app/scripts/widget_registry.py --check`, `cards_running_widget_check.sh`,
  `widget_style_check.sh`, `widget_view_dispatch_check.sh`,
  `widget_settings_check.sh`, `model_decode_check.sh`, both app
  `build_check.sh` targets, `macos-app/scripts/widget_parity_check.sh`, every
  offline app gate (the app-build-checks glob),
  `pytest scripts/tests/test_widget_registry.py
  hscc-api/tests/test_widget_route_parity*.py` (14 passed), and
  `python3 scripts/address_guard.py --tracked` (clean).
