kind: Fixed
task: t_611dbad7

- **`hscc node resume` confirms a restart from `/health` and `/v1/models`.**
  The resume no longer treats the launch client's exit code (or an inference
  token) as the verdict. A unit that answers both endpoints is reported
  RESUMED and its pause is cleared even when the client was cut loose or exited
  non-zero. When the client failed or was cut loose and the unit is still
  loading, the interactive resume keeps re-asking both endpoints every 5 s for
  up to 900 s (`HSCC_RESUME_CONFIRM_BUDGET`; 0 = ask once) before deciding it
  is NOT confirmed; the wait only reads the endpoints and never relaunches. The
  daemon expiry stream always uses the single ask (budget 0), so a slow unit
  cannot hold its stream thread. The launch-path probe is unchanged.
