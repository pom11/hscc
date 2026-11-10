kind: Fixed
task: t_2ab0eda1

- **The cloud memory provider no longer shadows the installed `memori` SDK.**
  The repo staged it as `memori/` — a top-level package with the same import name
  as the third-party SDK the providers import (`from memori import Memori`).
  Any process with the repo root or `~/.hermes/plugins` on `sys.path` (e.g.
  `python -m pytest` from the repo root, `scripts/dep_pr_watcher.py`) resolved
  `memori` to the provider and the SDK became unimportable at all, because the
  SDK's own `__init__` performs absolute intra-package imports. Renamed the dir
  to `memori_cloud/`, matching the `memori_byodb/` sibling; the deployed plugin
  key and `plugin.yaml` name follow, and Hermes still loads it by file location.

  Existing hosts are migrated rather than left with both dirs:
  `install_payload.RETIRED_PAYLOAD = {"memori": "memori_cloud"}` moves a stale
  deployed `~/.hermes/plugins/memori/` to
  `plugins-backups/memori.retired.bak-<stamp>` on the next bootstrap, only when
  the replacement is installed in the same pass, and never deletes it.

  Guards: `scripts/verify_memori_sdk_shadowing.py` spawns a child per path root
  and FAILs (exit 1) unless `from memori import Memori, embed_texts` reaches the
  SDK; `scripts/tests/test_memori_sdk_shadowing.py` covers the mechanism with
  hermetic fixtures, pins that no top-level `memori/` returns, and loads the
  renamed provider the way Hermes does (`hermes_plugins.memori_cloud`).
  Measurement and the rejected options:
  `docs/audits/memori-sdk-shadowing-t_2ab0eda1.md`.
