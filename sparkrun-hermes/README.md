# sparkrun-hermes

The official Hermes plugin for sparkrun: a single **guarded** `sparkrun_exec`
tool — a raw `sparkrun …` CLI passthrough for the operations the typed
`hscc-cluster` tools don't cover (browse/search recipes, benchmark, tune, proxy,
cluster definitions, export). The guard requires the command to start with
`sparkrun`, so the tool can't be used as a general shell; it runs the command
directly and captures stdout/stderr.

Pairs with the run/setup/registry skills for sparkrun usage. Registered via
`register(ctx)`.
