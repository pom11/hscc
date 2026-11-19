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

The backend alert feed (`hscc notify`, `POST/GET /v1/alerts`), the device
roster (`/v1/devices`) and the APNs sender remain server-side features; the
apps do not consume them. This document is kept only as the record of the
dropped design, not as a plan of record.
