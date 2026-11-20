kind: Changed

- **One Live Activity left: Cluster watch (A2).** It reuses the Cluster Health
  activity's plumbing and redraws the four surfaces the operator picked — Lock
  Screen header + four node temperatures/util columns + the
  `tok/s · daemon · autodown` footer, compact `4/4` and `72° · 231 t/s`,
  minimal hottest temperature, and the Lock Screen layout expanded. It is
  operator-started from the app's cluster section, ends after eight hours or on
  Stop, and is never auto-started by a foreground transition. Wake, Monitor,
  Running Cards and Board Progress leave the bundle, their drivers/attributes,
  settings rows, registry manifests, BG task identifiers and gates.
- **Stale Cluster watch data keeps its real time.** A `.stale` snapshot's
  `updatedAt` is the snapshot's date (the oldest read the push draws), so the
  age line and `staleDate` cannot present last-known data as fresh. Refresh
  "Off" now stops both the foreground loop and the BG submit instead of
  becoming a one-minute poll.

---
kind: Verified

- `ios-app/scripts/widget_registry.py --check`, `live_activity_rehydration_check.sh`,
  `cards_running_widget_check.sh`, `widget_style_check.sh`,
  `widget_settings_check.sh`, `model_decode_check.sh`, `shared_store_check.sh`,
  `update_check.sh`, both app `build_check.sh` targets, every offline app gate
  (the app-build-checks glob), `macos-app/scripts/widget_parity_check.sh`,
  `pytest scripts/tests/test_widget_registry.py scripts/tests/test_apps_release.py`,
  and `python3 scripts/address_guard.py --tracked` (clean). No CLI verb changed;
  no live HSCC/Hermes state was written.
