kind: Added
task: t_602ee6ce

- **iOS: install button in Settings → About.** Under "Check for updates" a button opens the install page in Safari. It reads "Install <version>" when the last check found a newer release and opens that release's install URL; otherwise it reads "Open install page" and opens the public install page.
---
kind: Verified
task: t_602ee6ce

- `ios-app/scripts/build_check.sh` (all three targets, 0 warnings), `update_check.sh`, `widget_style_check.sh`. Compile only; never run on a device.
