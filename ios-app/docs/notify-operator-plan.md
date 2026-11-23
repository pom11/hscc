# Notify the operator — DROPPED

Status: **dropped by operator decision, 2026-11-19.**

The plan below (local notification alerts, background polling, and later APNs
push) was built and then removed from both apps. The operator
decided the hscc apps carry **no notifications at all** — neither local nor
push. Widgets and Live Activities stay exactly as they were; they are data
surfaces, not notifications.

What was removed on the app side:

- iOS: the remote-notification registration and device-token upload to
  `/v1/devices`, the push entitlement, notification authorization requests,
  `NotificationCoordinator`, `NeedsOperatorNotifier`, `OperatorAlert` and their
  foreground/background polling, `PushRegistrar`, and the Notifications section
  (status, per-class toggles, Send test) in Settings.
- macOS: the 5 s `GET /v1/alerts` poll, the local notification posts and
  authorization request, and the Alerts section/badge in the menu bar
  dropdown.

The backend alert feed (`hscc notify`, `POST/GET /v1/alerts`) remains a
server-side feature. The device roster and the APNs sender do not: `3073e15c`
deleted `hscc_daemon/devices.py`, `hscc_daemon/apns.py` and the `/v1/devices`
routes on the same date, and
`hscc-api/tests/test_contract_swift_routes.py` fails if either route is ever
registered again. Nothing in the push design described below survives on
either side of the wire. This document is kept only as the record of the
dropped design, not as a plan of record.
