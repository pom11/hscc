# sparkrun-hermes

The official Hermes plugin for sparkrun: a single **guarded** `sparkrun_exec`
tool — a raw `sparkrun …` CLI passthrough (orchestrator-only) for the operations
the typed `hscc-cluster` tools don't cover (browse/search recipes, benchmark,
tune, proxy, cluster definitions, export). State-changing commands confirm first;
pure reads (`sparkrun status`, `sparkrun list`) run directly.

Pairs with the run/setup/registry skills for sparkrun usage. Registered via
`register(ctx)`.
