# Must NOT trip: the sanctioned fixture user (t_3480956c)

A test that must build a POSIX-shaped path uses the sanctioned names, never an
operator-shaped one. This is the home-path analogue of the 100.64.0.0/24 fixture
block: a named allowance, not an exempted directory.

    home = "/Users/hscc-fixture/.venv/bin/python"
    home = "/home/hscc-fixture/work/queue.json"
    home = "/home/runner/work/_temp/pytest-of-runner/pytest-0"   # GitHub's CI account
    link: ](/Users/hscc-fixture/deploy/run.sh)
    bare: /Users/hscc-fixture

The allowance is the WHOLE name and nothing wider — `/Users/` + a fixture name +
`-2` is an offender, and that case is pinned in
`scripts/tests/test_address_guard.py` rather than here, because writing it as a
contiguous path in a tracked file would make this fixture the offence it refutes.
