# hscc-cli — the `hscc` command wrapper

Thin pip package that exposes the `hscc` command, forwarding to the
`hscc_daemon` package's CLI entry point (`hscc_daemon.hscc.main`). It only
locates `hscc_daemon` and re-exports its `main`; all real logic lives in the
plugins ([`../hscc_daemon/README.md`](../hscc_daemon/README.md)).

## When you touch it

Only when the package-lookup or the install/shebang story changes. New verbs
belong to `hscc_daemon/hscc.py`, not here.

## Install — by bootstrap, into the Hermes venv

The `hscc` CLI is installed **into the Hermes runtime venv** as part of
`hscc-bootstrap/bootstrap.sh` (via `hscc-bootstrap/install_cli.py`). That is
the supported path: it bakes the Hermes-venv interpreter into the console
script's shebang **by construction** — the CLI reads Hermes runtime state and
needs the venv's permissions.

Installing into any *other* interpreter silently degrades verbs:
`hscc project chat / sessions / ask` import `hermes_cli` / `hermes_state`,
which exist only in the Hermes venv (elsewhere they raise
`HermesRuntimeUnavailable`); `hscc check` needs the macOS Local Network (TCC)
grant the Hermes venv carries, so under a different interpreter it fails
against a healthy fleet. A manual install therefore goes through the Hermes
venv's own python:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install ./hscc-cli
```

`install_cli.py` is idempotent: it installs if missing, verifies the resolved
entry point's shebang points at the Hermes venv, and force-reinstalls on
drift. Skip it during bootstrap with `--skip-cli`.

## How it works

`hscc_cli/__init__.py` locates the `hscc_daemon` package from the standard
locations (side-by-side installed plugins, `~/.hermes/plugins`, a repo
checkout, or `~/dev/hscc` as a fallback), adds that directory to `sys.path`,
and re-exports `hscc_daemon.hscc.main`. `pyproject.toml` declares
`hscc = "hscc_cli:main"` as the console-script entry point.

## How to test / Links

The package has no suite of its own; the installer that ships it does —
`python3 -m pytest hscc-bootstrap/tests/test_install_cli.py -q`.
Links: [hscc_daemon](../hscc_daemon/README.md) ·
[bootstrap](../hscc-bootstrap/README.md)
