# comfyui-sparkrun

ComfyUI custom nodes that generate media through the **sparkrun proxy**
(LiteLLM on port 4000) instead of talking to one hand-picked backend. The
proxy serves only models registered through hscc, so what the dropdown offers
is what the cluster actually runs.

| Node | Endpoint | Output |
|---|---|---|
| `SparkrunImage` | `POST /v1/images/generations` | `IMAGE` + `DICT` meta |
| `SparkrunImageEdit` | `POST /v1/images/edits` (multipart) | `IMAGE` + `DICT` meta |
| `SparkrunVideo` | `POST /v1/videos` + poll + download | `VIDEO` + `DICT` meta |

## Install

From a checkout of this repo:

```bash
bash comfyui-sparkrun/install.sh --dry-run /path/to/ComfyUI/custom_nodes   # preview
bash comfyui-sparkrun/install.sh /path/to/ComfyUI/custom_nodes             # copy
bash comfyui-sparkrun/install.sh --link /path/to/ComfyUI/custom_nodes      # symlink
```

`--link` keeps the pack live against the checkout, so `git pull` updates the
nodes without a reinstall; plain copy is the safer default when the checkout
moves around. Restart ComfyUI afterwards — custom nodes are loaded at startup.
Installation is deliberately NOT automated by anything in this repo: it is an
operator action against the operator's own ComfyUI.

## Configuration

`base_url` is a widget on every node. Its default is
`http://10.0.0.x:4000` — `10.0.0.x` is this repo's documented LAN placeholder
(the address guard rejects a real host in a commit), so point the nodes at the
real proxy either per-node or once, before ComfyUI starts:

```bash
export SPARKRUN_PROXY_BASE_URL=http://<your-proxy-host>:4000
```

| Variable | Default | Effect |
|---|---|---|
| `SPARKRUN_PROXY_BASE_URL` | `http://10.0.0.x:4000` | widget default for `base_url` |
| `SPARKRUN_MODEL_TTL_SECONDS` | `30` | how long the `/v1/models` dropdown result is cached |
| `SPARKRUN_DISABLE_MODEL_DISCOVERY` | unset | any value = never call `/v1/models` (dropdown shows the sentinel) |

## The model dropdown, and why it can be empty

Options come from `GET /v1/models` filtered on `model_info.mode` —
`image_generation` for the two image nodes, `video_generation` for the video
node. Those two strings are the LiteLLM mode enum owned by hscc's
`hscc-cluster/media_registration.py` (`KIND_TO_MODE`, media M3); when that
module is present in the tree, a repo test binds the pack's copy to it so the
client side and the registration side cannot drift apart.

Consequences worth knowing:

* **A model with no `model_info.mode`, or with `mode: chat`, never appears.**
  The proxy would reject it on a media endpoint anyway, and listing it would
  turn a routing fact into a confusing 400.
* **Before the media-M3 sparkrun fork patch is applied**, autodiscovered units
  carry no mode tag at all. The dropdown then reads
  `(no media models registered on proxy)`. That is the honest state, not a
  bug: select it and the node names the cause instead of firing a doomed
  request. You can also **type a model name** straight into the combo — the
  list is a convenience, not a whitelist.
* **Discovery never breaks the UI.** A dead or slow proxy degrades the dropdown
  to the sentinel; the request path reports the real error when you run the
  graph.

## Parameters

`size` is forwarded verbatim (`1024x1024` … — type your own if the backend
takes another). `steps` goes out as `steps`; `seed` goes out as `seed` and
**`0` is a real seed**, not "unset". The video node sends `fps` and the clip
length as `seconds`, then polls `GET /v1/videos/<id>` every `poll_interval`
seconds until a terminal state, honouring `poll_timeout`.

`SparkrunImageEdit` PNG-encodes the incoming `IMAGE` batch's first frame as the
`image` part, and an optional `MASK` becomes a grayscale `mask` part (whether
the backend honours it is backend-dependent).

## Requirements

Nothing beyond what ComfyUI already ships (Python 3.9+, Pillow, numpy, torch).
The pack uses `urllib` from the standard library — no `requests`, no extra
install step. See [`requirements.txt`](requirements.txt).

## Tests

```bash
bash scripts/run_tests.sh            # the pack runs as the `comfyui-sparkrun` leg
```

Fully offline: a stdlib fake proxy answers `/v1/models`, the image endpoints
and the video job lifecycle, and an autouse guard refuses any request that is
not loopback — so the suite cannot reach a live proxy even by accident. The
pack imports with **no torch and no ComfyUI installed** (the repo suite's
venv has neither); heavy use is guarded and the guard is asserted by an AST
scan in `tests/test_manifest.py`.
