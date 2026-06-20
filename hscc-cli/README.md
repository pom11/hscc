# hscc-cli — Hermes Spark Cluster Control CLI wrapper

Thin pip package that forwards `hscc` command to the `hscc_daemon` package.

## Install

```bash
pip install ./hscc-cli
```

## How it works

The `__init__.py` probes known paths for `hscc_daemon`:

1. `../plugins` (side-by-side with installed hscc-* plugins)
2. `~/.hermes/plugins`
3. Parent-of-parent (repo root for dev)
4. `~/dev/hscc` (fallback)

Once found, it imports `hscc_daemon.hscc.main` and exposes it as the `hscc` CLI entry point via `project.scripts`.
