# hscc-cli — Hermes Spark Cluster Control CLI wrapper

Thin pip package that exposes the `hscc` command, forwarding to the
`hscc_daemon` package's CLI entry point (`hscc_daemon.hscc.main`). It only
locates `hscc_daemon` and re-exports its `main`; all real logic lives in the
plugins.

## Install — by bootstrap, into the Hermes venv

The `hscc` CLI is installed **into the Hermes runtime venv** as part of
`hscc-bootstrap/bootstrap.sh` (stage 3b, via `hscc-bootstrap/install_cli.py`).
That is the supported path: it bakes the Hermes-venv interpreter into the
console script's shebang **by construction**, which is required because the CLI
reads Hermes runtime state and needs the venv's permissions.

Installing it into any *other* interpreter silently degrades several verbs:

- `hscc project chat / sessions / ask` import `hermes_cli` / `hermes_state`,
  which exist only in the Hermes venv — elsewhere they raise
  `HermesRuntimeUnavailable`.
- `hscc check` needs the macOS Local Network (TCC) grant the Hermes venv
  carries — under a different interpreter it fails against a healthy fleet.

So a bare `pip install ./hscc-cli` into a non-Hermes environment is **not** a
correct install: if you install manually, install with the Hermes venv's own
python:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install ./hscc-cli
```

`install_cli.py` is idempotent: it installs if missing, verifies the resolved
entry point's shebang points at the Hermes venv, and force-reinstalls if the
shebang has drifted. Skip it during bootstrap with `--skip-cli`.

## How it works

`hscc_cli/__init__.py` locates the `hscc_daemon` package from the standard
locations (side-by-side installed plugins, `~/.hermes/plugins`, a repo
checkout, or `~/dev/hscc` as a fallback), adds that directory to `sys.path`,
and re-exports `hscc_daemon.hscc.main`. `pyproject.toml` declares
`hscc = "hscc_cli:main"` as the console-script entry point.
