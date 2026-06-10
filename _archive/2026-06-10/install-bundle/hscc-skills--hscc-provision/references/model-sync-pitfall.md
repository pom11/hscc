# HuggingFace Model Sync Pitfall

Problem: `test -d` on HuggingFace cache dirs returns true for empty dirs. rsync creates directory structure before downloading files, so in-progress syncs appear cached.

Fix: Count actual files instead. Use `find /path/to/blobs -type f | wc -l` — if count > 0 the model is cached.

Affects: `model_on_host()` in `~/.sparkrun-local/scripts/model-check.py`. When syncing from NAS to cluster hosts, directories are created before files. Use file counting, not directory existence checks.

Pattern: Always count files not directories when verifying cache state. Check both blobs and snapshots.