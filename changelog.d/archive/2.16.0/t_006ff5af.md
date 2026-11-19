kind: Fixed
task: t_006ff5af

- **The Cluster Lock Screen no longer re-types the CLI's serving note.**
  `CLILockScreenCopy.voice` printed its own copy of `no backends behind the
  proxy`, the exact sentence `CLIMonitorNote.noBackends` owns; it now reads
  that owner's `text`, so the words exist once and cannot drift from the CLI's
  dim line. The missing-section case — where the CLI prints nothing at all —
  keeps the app's neutral `serving poll did not answer`.
- **`cluster_widget_check.sh` can no longer print PASSED while a check is
  red.** The compiled harness printed `CLUSTER WIDGET CHECK PASSED` whenever
  its executed proofs passed, even when a structural proof above it had
  already failed and the shell was about to exit 1. The harness now reports
  only its own `executed proofs passed — N checks` line; the gate's single
  PASSED verdict — or the new FAILED line — is printed by the shell after
  every check's rc is known.

---
kind: Verified
task: t_006ff5af

- `ios-app/scripts/cluster_widget_check.sh` exits 0 with exactly one PASSED
  line: all structural proofs plus 125 executed checks. A planted
  `"proxy /v1/model/info` literal made it print `CLUSTER WIDGET CHECK FAILED`
  with rc 1 and no PASSED line; the file was then restored byte-identically
  and the gate re-ran green.
- `ios-app/scripts/build_check.sh` (78/39/6/22 files, 0 errors, 0 warnings)
  and `macos-app/scripts/build_check.sh` (37/39 files, 0 errors, 0 warnings)
  are clean; `widget_parity_check.sh`, `widget_view_dispatch_check.sh`,
  `widget_style_check.sh`, `widget_data_client_check.sh`,
  `projects_widget_check.sh`, `cards_running_widget_check.sh`,
  `kanban_monitor_widget_check.sh`, `cluster_graph_check.sh` and
  `model_decode_check.sh` (349/349) are green.
- `python3 scripts/address_guard.py --tracked` is clean.
