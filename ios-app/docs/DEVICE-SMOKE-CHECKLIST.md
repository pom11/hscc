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

## Interpretation

| Observed | What it means |
| --- | --- |
| All five pass | Ship it — hand to the operator for a normal in-usage run. |
| Confirmed-green but a wrong token also shows "can't reach" | Auth isn't being distinguished from transport — the client is not mapping the 401 properly. Flag to ios-engineer. |
| A message is lost on backgrounding | The persisted-job resume path is broken. Flag to ios-engineer. |
| QR scan never reads, or camera stays black | Camera permission / QRScannerView issue. Flag to ios-engineer. |
| App won't launch at all | Likely the **7-day free-cert expiry** — re-run **⌘R** from Xcode to re-sign, then re-trust. Not a code bug. |
