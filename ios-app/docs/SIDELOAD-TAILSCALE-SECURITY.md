# HSCC iOS App — Sideload · Tailscale · Security

A focused reference for sideloading the private HSCC iOS client onto your own
iPhone and connecting it to your HSCC cluster over Tailscale. This is a
companion to `../README.md` (which covers what the app is and how to build it);

here we go deep on **getting it onto a device**, **wiring the connection**, and
**keeping it safe**.

> The app is **unbuilt and untested** — treat this as the plan for when you
> actually run it, and expect to fix small things on the first real build.

---

## 1. Sideloading (no paid account needed)

The app is installed **directly from Xcode** onto your iPhone — it is never on
the App Store and never distributed. A **free Apple ID personal team** is all
that's required; no $99/year account.

### Sign with your personal team

1. In Xcode, open the **HSCC** target → **Signing & Capabilities**.
2. Check **Automatically manage signing** and pick **your Apple ID** as the
   **Team**. (Xcode creates a free personal development team on first use.)
3. If signing fails with a bundle-id collision — free personal teams share a
   limited namespace and your `com.hscc.ios` may already be taken by another
   free app on your device — change the **Bundle Identifier** to something
   unique (e.g. `com.yourname.hscc`). For the XcodeGen path, mirror the change
   in `project.yml` (`options.bundleIdPrefix` and the target's
   `PRODUCT_BUNDLE_IDENTIFIER`).
4. Connect your iPhone, select it as the **Run** destination, and press
   **Run (⌘R)**. Xcode installs the app onto the device.

### Trust the developer certificate on the device

The first time you open a sideloaded app, iOS blocks it until you trust the
certificate. On the **iPhone**:

**Settings → General → VPN & Device Management** → tap your Apple ID under
**Developer App** → **Trust "Apple Development: <your name>"** → Trust.

After that the app opens normally.

### ⚠️ The 7-day certificate expiry (the #1 surprise)

A free personal-team signature is only valid for **7 days**. After that, iOS
**refuses to launch** the app (it's still installed, but won't run). This is
not a bug — it's how free signing works.

**To keep using the app:** reconnect the iPhone, open `HSCC.xcodeproj` in Xcode,
and press **Run (⌘R)** again. Xcode re-signs and reinstalls, giving you another
7 days. Your settings survive (the token lives in the Keychain, which persists
across re-sign/reinstall). Plan to re-run from Xcode roughly **weekly**.

### Free-tier limits (mostly irrelevant here)

Free personal teams support only a handful of app ids and devices, and no push
notifications, CloudKit entitlements, iCloud, or Watch complications. For a
solo one-user client none of this matters — but be aware that Apple enforces a
cap on the number of active free apps per device/account.

---

## 2. Connecting over Tailscale

The app talks to the HSCC API **only over your Tailscale tailnet**. There is no
internet path, and the API refuses to bind a public interface.

### Same tailnet, both sides

- The **iPhone** must have Tailscale installed, running, and **Connected**
  (green checkmark in the Tailscale app), signed into the **same account /
  tailnet** as the Mac.
- The **Mac** (the cluster host) must also be on that tailnet.

### Find the Mac's tailnet IP

This is the **host** you enter in the app. Any of these work:

- **Tailscale menu bar app** on the Mac — your IP (a `100.x.y.z`) is displayed.
- **Tailscale CLI.** On this Mac Tailscale is the macOS **App Store** build, so
  the CLI is **not on PATH**; use the full path:

  ```sh
  /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
  ```

  If your Tailscale was installed via `brew`/standalone, a plain
  `tailscale ip -4` may work instead. The HSCC API also resolves the tailnet
  IP itself and prints it via `hscc api status`.

> Example only — this host's tailnet IP is `100.64.0.1`. **Your IP will
> differ.** Do not reuse this example value verbatim; substitute the output of
> the commands above / `hscc api status`.

### Enter it in the app

Open the app's **Settings** screen and fill in:

- **Host** — the Mac's tailnet IP (e.g. `100.64.0.1`) or hostname.
- **Port** — `8788` by default on this deployment.
- **Token** — see §3.

Tap **Test connection** — the app calls `GET /v1/ping` against those settings
and shows either a green "Connected to HSCC API vX" or a red explanation.

---

## 3. Running the API + getting the token

### Start the API with the tailnet bind

On the Mac:

```sh
hscc api start --tailscale   # bind to the tailnet IP — reachable from the phone
hscc api status              # confirm running; prints the bound host:port
hscc api stop                # when done
```

- **Loopback is the default and the safe choice.** `hscc api start` with no
  flag binds `127.0.0.1` only — fine for local use, **unreachable** from the
  phone. To reach it from the iPhone you must opt in with `--tailscale`.
- `0.0.0.0` / a non-specific bind is **refused by design** — the API can
  start/stop GPU work on your cluster and must never be reachable publicly.
- You can also configure the default in `~/.hscc/api.json`
  (`{"bind": "loopback" | "tailscale", "port": 8787}`), but `--tailscale` on
  the command line is the simplest.

### Read the token

The bearer token is auto-generated on first `start` and written to
**`~/.hscc/api-token`** (mode `0600`, atomic write). Read it to copy into the
app:

```sh
cat ~/.hscc/api-token
```

Copy that single line into the app's **Token** field. **This document never
prints a real token value** — and you shouldn't paste one into a shared chat,
issue, or screenshot either.

### Rotate the token

If it may have leaked:

```sh
hscc api stop
rm ~/.hscc/api-token
hscc api start --tailscale   # writes a fresh token
```

Then update the app (the old token now gets `401 unauthorized`).

---

## 4. Security model (short & honest)

| Concern | Reality |
| --- | --- |
| Transport encryption | **Tailscale (WireGuard)** is the encrypted transport. The HSCC API itself does **NOT** terminate TLS — it serves plain HTTP over the tailnet. |
| Authentication | The bearer token is required on **every** request, reads included. No anonymous endpoints. |
| Token storage on device | **iOS Keychain** only. Never in UserDefaults, a plist, or source code. |
| Public exposure | Forbidden. The API refuses `0.0.0.0`; never port-forward or expose the port off the tailnet. |
| Blast radius | Anyone with the token **+** tailnet access can dispatch work and stop workloads. Treat it like an SSH key; rotate on suspicion. |

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Can't connect | Tailscale **down** on the phone | Open the Tailscale app → confirm **Connected** |
| Can't connect | Mac on a different tailnet / account | Sign both devices into the **same** tailnet/account |
| Can't connect | Wrong host or port | `hscc api status` shows the real bound host:port; match it in the app |
| Can't connect | API not running | `hscc api start --tailscale` on the Mac |
| Can't connect | API bound to loopback | Start with `--tailscale` (loopback is phone-unreachable) |
| `401 unauthorized` | Token mismatch / rotated | Re-copy the current `~/.hscc/api-token` value into the app |
| Won't launch after ~a week | **7-day cert expiry** | Re-run from Xcode (**⌘R**) to re-sign; re-trust cert if prompted |
| iOS blocks the app | Cert not yet trusted | Settings → General → VPN & Device Management → trust your Apple ID |
