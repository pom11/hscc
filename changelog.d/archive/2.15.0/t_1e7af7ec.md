kind: Fixed
task: t_1e7af7ec

- **`hscc-cluster/README.md` still told readers the unified `hscc orch inject`
  surface did not exist.** The "Orchestrator-to-orchestrator messaging" section
  was written while `main()` had no `orch` group branch, so it carried a
  "Unified-CLI status (as of this writing)" blockquote saying the verb
  dispatched only from the plugin's own entry point, plus a
  `python3 hscc-cluster/hscc.py orch inject … --dry-run` fallback line in the
  usage block and a pointer to use that spelling "until it lands". t_5da7fd56
  (PR #134) landed the wiring — `git grep -n '"orch"' origin/main --
  hscc_daemon/hscc.py` shows the group branch and `hscc help` lists `orch` — so
  the note had self-announced its own expiry and the README was now lying about
  the surface operators actually type. Deleted the blockquote and the
  fallback lines, kept the unified usage form, listed `hscc orch inject …` in
  the CLI section alongside `template`/`cluster`, and rewrote the refusal-
  rendering sentence to name the dispatching entry point honestly (the unified
  branch renders one reported line; the standalone engine path keeps its themed
  panel). Acceptance proof is the gate itself: backticked `hscc orch inject`
  now **passes** `scripts/readme_lint.py --strict` (0 findings), where before
  the wiring it was rejected by the same oracle. Docs only — no code change.
