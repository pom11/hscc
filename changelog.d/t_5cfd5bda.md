kind: Fixed
task: t_5cfd5bda

* `GET /v1/cluster/history` no longer claims a tier-0 residency it never
  measured: `store.tier0_resident` is written only by the branch that reads the
  2 s ring, so it is `true`/`false` when the served `tier` is 0 and `null` at
  tiers 1-2, where nobody measured it. It used to start as `true`, which made a
  `window=6h`/`7d` payload claim a resident recorder in a process that hosts none
  (measured on #413's iOS fixtures). The key stays on the wire and `note`/`speak`
  remain the signal for an empty tier-0 answer, so no client that parses the
  shape has to change.
---
kind: Verified
task: t_5cfd5bda

* `hscc-api/tests/test_cluster_history_parity.py` pins both halves: every durable
  window against a store with no resident recorder answers `tier0_resident`
  `None` with the key still present, and `window=15m` in the same process still
  answers `false`. Mutation-probed at the commit that adds it: reverting the
  default to `True`, and claiming residency from the durable branch, each redden
  that test. 28 passed in the parity file.
