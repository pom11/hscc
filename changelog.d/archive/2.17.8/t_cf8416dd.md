kind: Verified
task: t_cf8416dd

- **`hscc_daemon/tests/test_api_cli_stop_unavailable_supervisor_row_t_cf8416dd.py`:
  3 cases, green in 0.62 s on the interpreter the queue gates with
  (`~/.hermes/hermes-agent/venv/bin/python`).** `_handle_stop` has two "nothing to
  stop" rows and they make *different claims about the supervisor*: the launchd-aware
  one may state `launchd: unit not loaded` only because launchd actually answered,
  while the plain one is what a host with no `launchctl` at all prints, where
  `launchd_status` read `rc None` — "the supervisor could not be asked", never a
  statement about the unit. Nothing in the suite could tell them apart: the only
  assertion of the row anywhere was `test_api_cli_no_ansi.py`'s
  `assert "HSCC API is not running" in out`, and the launchd-aware row CONTAINS that
  string as a prefix, so it is satisfied by the wrong branch. Collapsing the
  `if sup.get("available")` guard was therefore invisible, and its result is the same
  false-verdict class t_8bf34130 exists to kill — a launchd verdict from a run that
  never asked launchd. The new file pins the pair from both sides, every case
  DECLARING its posture through the seam (`launchd_status(_run_probe=...)`, never a
  leg-wide default — the refusal fence is the feature) and pinning the seam's
  `launchctl print` call count so a case cannot pass by the verb having stopped
  asking: (1) launchctl unaskable (`rc None`) and no pid file -> the plain row, and
  `"unit not loaded"`/`"launchd"` absent from the output; (2) the control half, so
  the pin may not be met by amnesia — `launchctl print`'s real rc 113 could-not-find
  answer (the value pinned at probe level in `test_api_cli_launchd.py`) must still
  earn the verdict row; (3) the other road to `available: False`, a non-macOS host,
  where the row must still be the plain one AND `probe.calls == []` pins that not one
  doomed `launchctl` is forked. All canned answers are read-side, so
  `_stop_launchd`'s `launchctl bootout` (unfenced until t_e1c7c925) is never reached,
  and `_launchctl_run` is recorded rather than execed.
- **Measured, at base `origin/main@6c025066`, that the gap is real and that this file
  is the only thing that closes it.** Seven plants into `hscc_daemon/api_cli.py` all
  BIT (plain row renamed; the `available` guard collapsed; guard inverted; branch
  bodies swapped; `rc None` parsed as `available: True`; the verb's `sup =
  launchd_status() if _launchd_matters() else {...}` replaced by the hardcoded row;
  `_launchd_matters` widened past the platform test), each with `api_cli.py`
  byte-identical after restore (sha256-verified per plant). A four-arm control then
  separated the plant from the base's own reds: (A) pristine `origin/main` is already
  3 red in `test_api_cli_no_ansi.py` — the unsheamed cases #792 exists to fix, not
  this card's — so the naive one-arm control there is confounded; (B) `origin/main` +
  #792 = 238 passed; (C) the same tree with the plain row renamed and this file
  ABSENT = **238 passed, the plant invisible**, i.e. #792 does not retire the premise;
  (D) plus this file = 241 passed clean, and under the same plant exactly 2 red, both
  in this file and in no other. Anchoring note for the next worker: the `sup =
  launchd_status() if _launchd_matters() else {...}` pair is byte-identical in
  `_handle_status`, so a plant anchored on it alone edits the wrong verb and proves
  nothing — it needs the `# RULING (t_8bf34130)` comment that only `_handle_stop`
  carries.
