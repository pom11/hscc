kind: Added
task: t_e89b0eef

- **New top-level pack `comfyui-sparkrun/` — three ComfyUI custom nodes that
  talk to the sparkrun proxy (:4000) instead of a hand-picked backend.**
  `SparkrunImage` (POST `/v1/images/generations`), `SparkrunImageEdit` (POST
  `/v1/images/edits`, multipart PNG) and `SparkrunVideo` (POST `/v1/videos` +
  job polling + content download), each with a `base_url` widget (default
  `http://10.0.0.x:4000` — the documented LAN placeholder; override via env
  `SPARKRUN_PROXY_BASE_URL` or per-node), and a `model` dropdown populated
  from `GET /v1/models` filtered by `model_info.mode` (image nodes list
  `image_generation`, the video node lists `video_generation` — the exact
  enum strings `hscc-cluster/media_registration.py::KIND_TO_MODE` pins; a
  repo test binds the two so they cannot drift). HTTP is stdlib-only
  (`urllib`), the pack imports `comfy`/`torch` only behind guards so the repo
  suite — which has neither — still collects it. ComfyUI 0.43.2's loader
  contract (read off the operator install, `nodes.py:2312-2349`) is
  `NODE_CLASS_MAPPINGS` **or** `comfy_entrypoint`, and no `comfy.plugin.json`
  is read anywhere in the tree, so the pack ships the classic
  `__init__.py` + `NODE_CLASS_MAPPINGS` layout and no plugin manifest.
  `install.sh` copies (or `--link`s) the pack into a ComfyUI `custom_nodes/`
  dir with `--dry-run`; installation is operator-run, not part of the suite.
---
kind: Verified
order: 1

- `comfyui-sparkrun/tests/` runs OFFLINE through the node functions against a
  stdlib `http.server` fake proxy (fixture `/v1/models`, fake b64 image
  responses, fake video job lifecycle queued→processing→completed→content):
  see the card comment for counts. The pack imports with no `comfy`, no
  `torch` and no network; the repo's own torch-less venv is the proof.
  `bash scripts/run_tests.sh` ALL GREEN with `comfyui-sparkrun` registered as
  a runner leg; `python3 scripts/address_guard.py --tracked` rc=0.
