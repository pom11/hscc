# The `hscc_daemon` thin-sibling green is partly order-dependent: a file run ALONE can FAIL where the full leg passes

**Card:** t_572b4856 (opened from t_7c5f5051's review, which found and fixed one
instance of it). **Status: IN PROGRESS** — this page is being filled as the
measurement lands.

## Question

`hscc_daemon` prints `ALL GREEN` on the thin sibling
(`HSCC_TEST_PY=~/miniconda3/envs/p313/bin/python HSCC_TEST_LEGS=hscc_daemon`),
which the dual-interpreter stamp reads as "no regression". That reading assumes
the leg's answer is a property of the **interpreter**. If a file that passes
inside the leg FAILS when run alone under the same interpreter, the leg is
answering a question about test **order**.

The known mechanism: `hermes_cli` is not importable at all on the thin sibling
(no `__editable__.hermes_agent-0.0.0.pth`), but something during a full-leg run
leaves the real agent tree on `sys.path`, after which `hermes_cli` resolves for
the rest of the process. `conftest._hermetic_hermes_cli_imports`
(t_2be3b04e) purges *planted temp-root* paths and modules; the residue here is
the **real** agent tree, which that fixture does not and should not remove.

## Method

`scripts/run_tests.sh` runs a leg as ONE pytest process over the whole dir:

```
"$PY" -m pytest -q -rs "$ROOT/hscc_daemon/tests" -p no:cacheprovider
```

So the reference state is that command, and the comparison state is one process
per file:

```
"$PY" -m pytest -q -rs "$ROOT/hscc_daemon/tests/test_X.py" -p no:cacheprovider
```

Both sides run with `--junit-xml` (report-only; it does not touch execution) so
the diff is per-file **and** per-nodeid, not just a tail line. 131 test files.

## Findings

_(filled below as each measurement lands)_
