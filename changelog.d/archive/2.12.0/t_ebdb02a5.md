kind: Changed
task: t_ebdb02a5

- **sparkrun runtime bump v0.4.0 -> v0.4.1** (operator-run live cutover,
  2026-10-20 08:37, via the detached #56 script): `runtime-versions.json`
  now pins `v0.4.1`. Live fork branch re-cut as
  `feat/proxy-least-in-flight-041` @ `1aaa6479` (upstream `v0.4.1` + the
  macOS-spawn test fix), editable at `~/dev/sparkrun-041-verify`, venv
  `.venv-sparkrun-041`; `-040` kept as the rollback line. The 0.4.1
  proxy/balancer surfaces hscc rides are byte-identical to 0.4.0 (empty
  diff over `proxy/config.py`, `plugins/litellm/engine.py`, `serving/`);
  proxy/balancer tests 563 pass + 1 pre-existing-on-both-tags failure.
  `dep_bump_guard` constants and the `sparkrun_bridge` live-build comment
  re-pointed to the new topology. Compatibility record:
  `docs/audits/SPARKRUN_041_COMPAT_2026-10-20.md`.
