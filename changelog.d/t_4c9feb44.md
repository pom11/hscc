kind: Fixed
task: t_4c9feb44

The `scripts` test leg stalled at test ~233 of 468 and never resumed: every
test in `scripts/tests/test_cli_help_surface.py` hung, including the trivial
first one. The leg was not hung — the module `report` fixture was crawling.
`hscc_cli_help_audit.Prober` binds the api ports without `listen()` as a
defensive hold and asked that port "is a server listening?" once per probe. A
port bound without `listen()` does not answer a SYN on macOS, so each question
returned EAGAIN only after burning its full 0.25 s socket timeout instead of an
instant ECONNREFUSED: 0.232 s x 961 probes = 223.05 s of a 307.64 s audit wall.
`_port_busy()` now reads the answer off the hold when the Prober holds the port
(a stray server cannot bind it then, so the probe could never have told us
anything) and still probes for real when the bind failed. Same report — 169
surface paths, 331 probes, 20 findings — measured 59.69 s, 5.2x faster. Pinned
by three tests, one of which pins the kernel behaviour itself so the
assumption is tested rather than commented.
