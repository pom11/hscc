# HSCC iOS — Device smoke checklist (what cannot be automated)

There is **no iOS runtime on the build host** — the Swift compiler and the model
decode checks are the only automated verification. The items below exercise UI,
camera, app-lifecycle, and token states that only a real device can show. Run
them on the phone **before** handing the build to the operator for testing.

Setup: the API is running with the tailnet bind on the Mac
(`hscc api start --tailscale`), the iPhone has Tailscale **Connected** on the
same tailnet, the app is installed via Xcode (⌘R), and the dev cert is trusted
(`Settings → General → VPN & Device Management`). **Total: under ten minutes.**

Check off each line; anything that fails, note exactly what appeared.

---

## 1. Pair by QR  (≈2 min)

- [ ] In the Mac terminal run `hscc api status` so it prints the setup QR code.
- [ ] In the app: **Settings → Scan QR**, allow camera access when prompted.
- [ ] Point the camera at the code. It should read once (session pauses) and show
      a **confirm** dialog with the host/port filled in (token not shown in full).
- [ ] Tap confirm. The app fills Settings and **auto-tests the connection** — it
      should report **Connected / ping OK**, not an error.
- [ ] Back out to the main screen; the connection banner should show the cluster
      as reachable, and the Projects tab should load real projects.

**Pass if:** scanning once → confirm → auto-test green with no manual typing.

---

## 2. Send a message  (≈2 min)

- [ ] Open a project → **Chat** tab.
- [ ] Type a short message (e.g. "summarize the board in one line") and send.
- [ ] Confirm dialog appears (mutations are confirm-gated) → confirm.
- [ ] The prompt appears in the transcript immediately, an in-flight footer
      shows elapsed time, and the reply lands within a few seconds.

**Pass if:** prompt shows instantly and a real reply arrives without a hang.

---

## 3. Background mid-reply and return  (≈2 min)

- [ ] Send a message that will take a little while (a broad or complex ask).
- [ ] While the in-flight footer is still counting, **press Home** to background
      the app.
- [ ] Wait ~20–30 s (the server finishes the reply), then reopen the app.
- [ ] The transcript should **resume polling the persisted job** and show the
      finished reply — not lose it, not hang, not restart the send.

**Pass if:** the reply appears after returning, proving the job survived
backgrounding/relaunch.

---

## 4. Scan with a wrong token  (≈1.5 min)

- [ ] In Settings, open **Scan QR** and scan the CURRENT code but with the token
      deliberately changed (e.g. edit the last character in the terminal before
      re-printing a code with `hscc api status`, or decode and alter it).
- [ ] The confirm dialog appears; confirm.
- [ ] The auto-test should **fail with an auth error** — the app should say
      something like **"Not authorized — check your token,"** not "can't reach".
- [ ] Optionally: scan a malformed code (random text / QR of a website) — it
      should be rejected up front as **"That isn't a valid HSCC setup code"**
      before any connection is attempted.

**Pass if:** a wrong token is clearly an *auth* failure (not connectivity), and
a malformed code is rejected outright without touching settings.

---

## 5. Revoke the token and observe the error  (≈2 min)

- [ ] With the app paired and working, rotate the token on the Mac:
      `hscc api stop && rm ~/.hscc/api-token && hscc api start --tailscale`
      (this writes a fresh token — the app's stored token is now stale).
- [ ] Without changing the app, tap into any live surface (e.g. **Test
      connection** in Settings, or open a project). 
- [ ] It should fail with the **auth** message — **"Not authorized — check your
      token"** (a `401` mapped from the `unauthorized` code), **not** a
      connectivity error and not a crash.
- [ ] Re-scan the fresh QR (or paste the new token) and confirm the app recovers
      to green.

**Pass if:** a revoked token yields a clear, calm auth error and the app
recovers once the new token is applied.

---

## 6. Timestamps show real numbers  (≈30 s) — **NEW, most likely to be visibly wrong**

Until 2026-08-30 every relative time in the app rendered as the literal text
`\(s)s` instead of `5s`, because the string interpolation was escaped. If the
fix did not land you will see it instantly, everywhere.

- [ ] Open **Fleet**, **Cluster**, **Activity feed**, **Approvals**, **Autodown**
      and **Board hygiene**.
- [ ] Every "time ago" label reads like `5s` / `12m` / `3h` / `2d`.
- [ ] **FAIL** if you see a literal `\(s)s` or `\(m)m` anywhere.

---

## 7. Session and memory actions actually work  (≈2 min) — **NEW**

These four operations used to POST to a literal, unmatched URL and always fail.

- [ ] **Sessions** tab → pick a session → **Retire**. Confirm. It succeeds and
      the row updates.
- [ ] Pick another session → **Compact**. Confirm. It succeeds.
- [ ] **Memory** tab → **Delete** a memory. Read the confirmation text before
      confirming: it must name the memory, e.g. *Delete the memory "foo"?* —
      **FAIL** if the prompt contains a raw `\(titleDisplay(item))`.
- [ ] **Edit** a memory, change the text, save. It persists.

---

## 8. Sessions and Memory are filtered to the right profile  (≈1 min) — **NEW**

The profile filter was silently dropped from the request, so these lists showed
everything rather than the project's own.

- [ ] Open a project → **Sessions**. The list shows only that project's
      orchestrator sessions, not every profile on the cluster.
- [ ] Same for **Memory**.
- [ ] **FAIL** if you see entries that clearly belong to another project.

---

## 9. Offline cache shows the right screen's data  (≈2 min) — **NEW**

Every cached read shared a single cache key, so screens could show each other's
data when offline.

- [ ] While connected, visit **Fleet**, **Cluster** and **Activity feed** so each
      caches.
- [ ] Turn on **Airplane mode**.
- [ ] Visit those three screens again. Each shows **its own** last-known data,
      clearly marked stale.
- [ ] **FAIL** if one screen shows another screen's content.

---

## 10. Live Activity appears  (≈2 min) — **NEW, never runtime-tested**

`NSSupportsLiveActivities` was missing from the app's Info.plist entirely, so
ActivityKit refused to start *any* Live Activity. This has never run on a device.

- [ ] Trigger a fleet wake (or a long chat that starts a session activity).
- [ ] A Live Activity appears on the **Lock Screen**, and in the **Dynamic
      Island** if your phone has one.
- [ ] It updates as state changes, and ends cleanly.
- [ ] **FAIL** if nothing ever appears — that means the plist key still is not
      reaching the built app.

---

## 11. Home Screen widget  (≈2 min) — **NEW, never runtime-tested**

- [ ] Long-press the Home Screen → **+** → find **HSCC** → add the small widget,
      then the medium one.
- [ ] Both render cluster state rather than a placeholder or "unable to load".
- [ ] They refresh (give it a few minutes, or toggle the fleet).

---

## 12. Streaming chat rejects a rotated token honestly  (≈2 min) — **NEW**

Previously a rejected token made the stream reconnect forever with no
explanation.

- [ ] Open a project **Chat** and get a live stream going.
- [ ] On the Mac, rotate the token so the current one is invalid.
- [ ] Send another message / let the socket drop.
- [ ] The app must show **"The cluster rejected this stream — your token may have
      rotated. Check it in Settings."** and STOP retrying.
- [ ] **FAIL** if it sits on "Reconnecting…" indefinitely.
- [ ] Restore the token and confirm the stream recovers.

---

## 13. Leave a chat mid-reply  (≈1 min) — **NEW**

An uncancelled poll used to keep running after the view was gone.

- [ ] Send a message, and **navigate away** before the reply arrives.
- [ ] Come back a minute later. The reply is there **once** — not duplicated.
- [ ] The phone does not get warm / battery does not visibly drain while the
      app sits on another screen.

---

## 14. Brief network drop recovers quickly  (≈1 min) — **NEW**

The reconnect backoff never reset, so after one drop every later reconnect
waited the full 15 s ceiling.

- [ ] With a live stream, toggle Airplane mode on and off quickly.
- [ ] It reconnects and resumes with no missing and no repeated messages.
- [ ] Do it a second and third time — recovery should stay **fast**, not get
      progressively slower.

---

## Interpretation

| Observed | What it means |
| --- | --- |
| All five pass | Ship it — hand to the operator for a normal in-usage run. |
| Confirmed-green but a wrong token also shows "can't reach" | Auth isn't being distinguished from transport — the client is not mapping the 401 properly. Flag to ios-engineer. |
| A message is lost on backgrounding | The persisted-job resume path is broken. Flag to ios-engineer. |
| QR scan never reads, or camera stays black | Camera permission / QRScannerView issue. Flag to ios-engineer. |
| App won't launch at all | Likely the **7-day free-cert expiry** — re-run **⌘R** from Xcode to re-sign, then re-trust. Not a code bug. |
