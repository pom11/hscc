kind: Fixed
task: t_611dbad7

- **`hscc node resume` confirms a restart from `/health` and `/v1/models`.**
  The resume no longer treats the launch client's exit code (or an inference
  token) as the verdict. A unit that answers both endpoints is reported
  RESUMED and its pause is cleared even when the client was cut loose or exited
  non-zero; a unit that answers neither keeps the pause. The launch-path probe
  is unchanged.
