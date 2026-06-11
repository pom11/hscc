# Offline NAS -> node HF cache

The cluster serves models **offline**. Weights live once on the NAS and are
copied to each node's local HuggingFace cache. This avoids a per-node internet
download and lets `HF_HUB_OFFLINE=1` containers start instantly.

## The layout

| Location | Path | Notes |
|----------|------|-------|
| NAS (mounted on gateway) | `/mnt/nas/hub/<repo>/` | single source of truth |
| Node-local cache | `/home/spark/.cache/huggingface/hub/<repo>/` | what the container reads |
| In container | `/cache/huggingface` | bind-mount of the node-local dir |

`<repo>` = `org/model` with slashes doubled to dashes:
`nvidia/Qwen3.6-35B-A3B-NVFP4` -> `models--nvidia--Qwen3.6-35B-A3B-NVFP4`.

A complete repo has `refs/main` (the snapshot hash), `snapshots/<hash>/` (the
files the container actually reads), and `blobs/`.

## Why pre-populate (the sparkrun trap)

sparkrun's `run` has a **"Distributing model"** step that does an **online**
`hf download <model>` into the *node-local* cache — NOT from NAS, NOT into
`/mnt/nas`. On a fresh node that means a ~22 GB internet pull (and HF Hub
rate-limits to ~2 MB/s, so it can take hours and may hang on the Xet protocol).

If the node-local cache is already complete, that step becomes a ~10 s cache-hit.
So: **rsync NAS -> node first**, then `sparkrun run`.

## The rsync-from-gateway pattern

macOS rsync (2.6.9-era) **cannot** do remote->remote
(`rsync host1:/src host2:/dst`). The gateway has `/mnt/nas` mounted locally, so
run rsync **from the gateway** to each node:

```bash
ssh spark@<gateway> \
  'rsync -a -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no" \
   /mnt/nas/hub/<repo>/ spark@<node>:/home/spark/.cache/huggingface/hub/<repo>/'
```

`onboard.py cache <model>` does exactly this for every serving node, idempotently.

## Rules

- **Additive only.** Use `rsync -a` **without** `--delete`. Create the parent
  with `mkdir -p` first. Never `rm -rf` a cache dir (the old `model-check.py` did;
  do not copy that behavior).
- **Completeness check = ref + file count**, not directory existence. A node is
  "ready" when its `refs/main` equals NAS's and its `snapshots/<hash>/` has at
  least as many files as NAS. `onboard.py` checks this before and after sync.
- **Leftover `*.incomplete` blobs** from an aborted `hf download` are harmless —
  the container reads `snapshots/<hash>/` (real files after rsync), not the junk
  in `blobs/`. They don't trigger a re-download once `refs/main` + snapshot are
  complete.
- **Getting weights onto NAS** (only if missing): download once on the gateway
  into `/mnt/nas/hub` — `HF_HUB_OFFLINE=0 hf download <model> --cache-dir /mnt/nas/hub`.
  If `hf` hangs on large Xet repos, use the direct-HTTP fallback (download each
  snapshot file over HTTPS instead of the Xet protocol).

## Quick verification

```bash
# ref hash on NAS vs a node
ssh spark@<gateway> 'cat /mnt/nas/hub/<repo>/refs/main'
ssh spark@<node>    'cat /home/spark/.cache/huggingface/hub/<repo>/refs/main'
# snapshot file counts should match
ssh spark@<node> 'find /home/spark/.cache/huggingface/hub/<repo>/snapshots/<hash>/ -type f | wc -l'
```
