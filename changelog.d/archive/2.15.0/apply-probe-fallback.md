kind: Fixed
task: apply-probe-fallback

- **Apply's already-serving probe no longer needs the daemon package.** When
  `hscc_daemon` is not importable (a bare `python` against the plugin dir) the
  probe fell back to "not serving", so apply ran `--ensure` and the ssh hang
  came back; it now does a plain stdlib GET of `/v1/models` instead.
