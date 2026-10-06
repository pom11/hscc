"""NEGATIVE CONTROL (t_163fa09f): disable the job-generation epoch guard.

Proves the guard is load-bearing rather than decorative. With it disabled,
``test_retired_worker_cannot_register_a_job`` fails (a retired worker's write
lands) and, when combined with the drain mutation, the whole isolation file goes
red. Run from the hscc-api dir:

  PYTHONPATH=<repo-root>:<hscc-api-dir> \\
    python -m pytest -q tests/test_ws_stop_noop_isolation.py -p ws_relay_mut_noepoch
"""
def pytest_configure(config):
    import routes_orchestrator as ro
    # Every thread now looks untracked => _new_job accepts every write.
    ro._calling_job_generation = lambda: None
