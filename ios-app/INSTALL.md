# Install HSCC on an iPhone

HSCC is distributed as an **Ad Hoc** iOS build from GitHub. Apple only lets it
install on iPhones the maintainer has registered, so do step 0 once per device.

0. **Get your device registered.** Connect the iPhone to a Mac. In **Finder**,
   select the iPhone and click the line showing its model and capacity until the
   **UDID** appears (right-click to copy). **Apple Configurator** shows the UDID
   too. Send it to the maintainer, who adds it at
   [developer.apple.com → Devices](https://developer.apple.com/account/resources/devices/list)
   and re-runs the release. Your device must be in the profile before install
   will work.
1. **Open the install page in Safari on the iPhone:**
   <https://pom11.github.io/hscc/ios/>. Other browsers do not handle
   `itms-services://` links.
2. Tap **Install HSCC**, then confirm **Install** in the dialog.
3. Settings → General → VPN & Device Management is **not** needed for an Ad Hoc
   build. Only if iOS explicitly asks you to trust the developer, trust it there.
4. Open **HSCC**, add your cluster (addresses and token), then add widgets
   (long-press the Home Screen or Lock Screen → **+**) and start the Live
   Activities you want.
5. **To update:** open the same page again and tap **Install HSCC**. The new
   build replaces the old one and keeps your settings.

## Troubleshooting

- **“Unable to install HSCC”** — the iPhone is not in the provisioning profile.
  Send the maintainer its UDID (step 0), then re-run the release and install
  again.
- **Nothing happens when you tap Install** — the page is not open in Safari, or
  it is not served over HTTPS. Copy the link into Safari.
- **Widgets / Live Activities cannot see your cluster** — configure the app
  itself first, then re-add the widget from the gallery.

Maintainer release steps: [RELEASE.md](RELEASE.md).
