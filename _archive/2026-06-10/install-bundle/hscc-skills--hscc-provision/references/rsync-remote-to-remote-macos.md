# Remote-to-Remote Rsync Pitfall on macOS

## Problem

macOS ships with an old rsync (2.6.9 compatible) that **does not support
remote-to-remote transfers**:

```bash
# THIS FAILS on macOS:
rsync -avz -e "ssh spark@key" spark@host1:/path/ spark@host2:/dest/
# Error: "both source and destination cannot be remote files"
```

## Why

The system rsync on macOS (from Apple's rsync 2.6.9 port, not GNU rsync) lacks
the `--rsh` flag capability needed for remote-to-remote sync. Even GNU rsync 3.x
may need `--rsh` to be set explicitly, and Homebrew's rsync may not be in PATH.

## Solutions

### 1. Run rsync FROM the source host (recommended)

SSH into the source machine and run rsync there, pointing to the destination:

```bash
ssh spark@SOURCE_HOST \
  "rsync -avz --progress -e 'ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no' \
   /local/source/path/ spark@DEST_HOST:/dest/path/"
```

This is the pattern used by `model-check.py` — NAS has local file access, so
rsync from NAS to targets works reliably.

### 2. Download to local first, then upload

```bash
# NAS -> local machine
scp -r spark@NAS_HOST:/path/to/model ~/local-model/

# local machine -> target
scp -r ~/local-model/ spark@TARGET:/dest/
```

Works for smaller models but wasteful for 35GB+ models.

### 3. Use SSH pipe (no rsync needed)

```bash
ssh spark@SOURCE "tar czf - -C /source/path ." \
  | ssh spark@TARGET "tar xzf - -C /dest/path"
```

Simple but no delta transfer (transfers full archive every time).

### 4. Install GNU rsync via Homebrew

```bash
brew install rsync
# Then use /opt/homebrew/bin/rsync or /usr/local/bin/rsync
```

Only works if Homebrew is available.

## Verification

To confirm rsync version on the system:

```bash
rsync --version | head -1
# macOS default: rsync version 2.6.9 compatible
# GNU rsync: rsync version 3.x.x
```

## In Practice

For the DGX Spark cluster workflow, always use **solution #1**: run rsync
from the NAS host (`192.0.2.10`) since it has direct local file access
to `/mnt/nas/` and SSH connectivity to all target nodes.

Example from model-check.py:
```python
cmd = f'rsync -avz -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no" {src} spark@{target}:{dst}'
subprocess.run(["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
    f"spark@{NAS_HOST}", cmd])
```
