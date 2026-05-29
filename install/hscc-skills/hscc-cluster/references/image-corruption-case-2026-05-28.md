# Node-Specific Docker Image Corruption — 2026-05-28

## Symptom
Container shows as "Up" but vLLM doesn't respond on port 8000. No HTTP response on `/v1/models`.

## Root Cause
Node 248 had `sparkrun-eugr-vllm:latest` image `sha256:605c3d51...` (local build, 19.4GB) with **CPU-only PyTorch 2.10.0+cpu** instead of the correct CUDA-enabled image `sha256:4d99250c...` (19.2GB) with **PyTorch 2.11.0+cu130**.

## Evidence
```
# PyTorch version check:
ssh spark@192.0.2.13 "docker run --rm --gpus all sparkrun-eugr-vllm:latest python3 -c \"import torch; print(torch.__version__); print(torch.cuda.is_available())\""
PyTorch: 2.10.0+cpu
CUDA: False

# Image ID mismatch:
ssh spark@192.0.2.13 "docker inspect sparkrun-eugr-vllm:latest --format '{{.Id}}'"
sha256:605c3d51fe5ddfd682b1011908f6ab39d27a71bf78c9e845b0a41592eeaa4f2a

ssh spark@192.0.2.11 "docker inspect sparkrun-eugr-vllm:latest --format '{{.Id}}'"
sha256:4d99250c3bfd128983439da5a7560835081007aa46afa5308ce41e96c1bfaa05

# vLLM crash:
ImportError: libtorch_cuda.so: cannot open shared object file: No such file or directory
```

## Fix
Copy correct image from working node over LAN:
```bash
ssh spark@192.0.2.11 "docker save sparkrun-eugr-vllm:latest | gzip -9" \
  | ssh spark@192.0.2.13 "gzip -dc | docker load"
```
Then re-run the sparkrun job.

## Prevention
After `docker pull` on any node, always verify:
```bash
ssh spark@<IP> "docker inspect sparkrun-eugr-vllm:latest --format '{{.Id}}'"
```
Compare against known-good node (.246 or .247). If IDs differ, the pull got a different/older version.

## Timeline
- 248 had a newer timestamp (pulled 1h ago, 19.4GB) but WRONG content
- 244/246 had older timestamp but correct content (19.2GB)
- Docker pull over SSH can sometimes get stale/incorrect manifests
