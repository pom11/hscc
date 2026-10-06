"""NEGATIVE CONTROL (t_163fa09f): disable the drain (no signal, no join).

Proves quiesce-before-clear is load-bearing. With the drain neutered,
``test_boundary_drain_joins_a_slow_relay_worker`` and
``test_polluter_then_stop_noop_pair_is_isolated`` both fail — i.e. this
reproduces the original flake's class of failure on demand. Run from hscc-api:

  PYTHONPATH=<repo>/scripts/audits:<repo>/hscc-api \\
    python -m pytest -q tests/test_ws_stop_noop_isolation.py -p ws_relay_mut_nodrain
"""
def pytest_configure(config):
    import routes_orchestrator as ro
    ro.drain_workers = lambda timeout=5.0: []
