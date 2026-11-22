"""Throwaway probe for t_56bd1fd2 — DO NOT MERGE, DO NOT REBASE ONTO ANYTHING.

This file exists to prove exactly one thing about the repository's new posture:
with the repository variable ``HSCC_TESTS_ENFORCE=true``, a red test leg fails
its own job, the ``legs`` aggregator fails, and branch protection on ``main``
refuses the merge **even for an admin** (``enforce_admins=true``).

The assertion is deliberately unsatisfiable. What matters is *which* red this
produces: a **test** failure (``pytest exit 1`` plus a real summary line, which
``.github/scripts/leg_verdict.sh`` passes through to ``exit "$rc"`` under
ENFORCE), not a **harness** failure (the ``no summary`` or ``not a test-failure
code`` branches, which are red by design and are not cleared by the variable).

Delete the branch and close the PR once the three observations are recorded on
t_56bd1fd2.
"""


def test_enforce_probe_a_red_test_must_block_the_merge():
    assert 1 == 0, "t_56bd1fd2 probe: this failure is the payload, not a defect"
