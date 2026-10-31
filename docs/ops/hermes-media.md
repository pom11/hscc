# Hermes media enablement behind the fleet proxy (M6)

**Card:** t_ec488ce3 (media workstream M6). **Design:** `docs/design/media-serving.md`
(M0, approved 2026-10-30) — this doc is the *client-side* enablement recipe its
§2 contract implies. No fleet action lives here: serving a media unit is
M1/M4's job; this page only tells a Hermes instance how to *call* media models
that are already registered at the proxy.

Conventions (same as M0): `10.0.0.x` = LAN-node placeholder — replace with the
real Mac/proxy LAN address at deploy time; the address-guard forbids real
addresses in this repo. `:4000` = the LiteLLM proxy, the single media entry
point (M0 §7 — Hermes never talks to a node port).

## 0. One endpoint, two tools

Hermes reaches fleet media through exactly one base URL — the proxy:

```
http://10.0.0.x:4000/v1        # LiteLLM on the Mac hub; LAN-only, bearer master key
```

Two Hermes tools are enabled by this page: `image_generate` (native
`openai` provider — config only, no plugin) and `video_generate` (the fleet's
`openai_compat` plugin shipped in this repo).

## 1. image_gen — config only (no Hermes code)

`image_gen` already ships an `openai` provider that takes
`image_gen.openai.base_url` and sends the configured model id **verbatim** to
the server (operator-verified; `plugins/image_gen/openai` in Hermes core).
Point it at the proxy:

```yaml
# ~/.hermes/config.yaml  (or the profile's config.yaml)
image_gen:
  provider: openai
  openai:
    base_url: http://10.0.0.x:4000/v1
    key_env: HSCC_PROXY_API_KEY     # env var NAME holding the proxy bearer key
    model: Qwen-Image-2.1-FP8       # generation alias, served verbatim
```

and export the bearer (`~/.hermes/.env` or the profile's `.env`):

```
HSCC_PROXY_API_KEY=***            # the proxy's LiteLLM master key
```

Notes:

- **Model ids are verbatim.** Whatever `image_gen.openai.model` (or
  `image_gen.model`) holds is what goes into the request's `model` field — it
  must be a name the proxy actually routes (sparkrun autodiscovery registers
  what units advertise on `/v1/models`, plus the `proxy.yaml` aliases).
- **First tier (operator ruling §8 Q1):** image generation =
  `Qwen-Image-2.1-FP8`; image **edit** = `lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang`.
  One `image_gen` model is active at a time — to edit, set the same key to the
  edit alias and set it back afterwards (Hermes has no per-mode model slot).
- `video_gen`/`image_gen` are in Hermes' `_DEFAULT_OFF_TOOLSETS`: the tools
  stay invisible until their toolset is enabled — `hermes tools` → Image
  Generation, or add `image_gen` to the top-level `toolsets` list.
- The proxy must actually route the alias before this works: `curl`
  `http://10.0.0.x:4000/v1/models` and look for it (a `chat`-mode tag on a
  media model is the M0 §3 `model_info.mode` gap — routing works regardless;
  the route dispatch is by URL).

## 2. video_gen — the fleet `openai_compat` plugin

`video_generate` has no OpenAI-compatible provider in Hermes core (only
fal/xai/deepinfra/openrouter), so the fleet ships one as a Hermes **plugin**
(the placement M0's plugin-first rule asked for):
`install/hscc-plugins/video_gen/openai_compat/` in this repo, deployed by
bootstrap to `<profile-home>/plugins/video_gen/openai_compat/` (the category
level Hermes' scanner keys on — see `PAYLOAD_EXTRA_DESTS` in
`hscc-bootstrap/install_payload.py`).

It speaks exactly M0 §2: `POST /v1/videos` → poll `GET /v1/videos/{id}` →
`GET /v1/videos/{id}/content` (root-relative content URLs resolve against the
configured base). Per ruling §8 Q5 it carries **no deadline and no
size/seconds caps** — the only pacing is a ~5 s poll interval (override:
`video_gen.openai_compat.poll_interval_s` or `HSCC_VIDEO_POLL_INTERVAL_S`).
Bootstrap copies it both under `<profile-home>/plugins/install/…` and — via
`PAYLOAD_EXTRA_DESTS` — at the scanned category level
`<profile-home>/plugins/video_gen/openai_compat/` (see
`hscc-bootstrap/install_payload.py`).

Enable (after running bootstrap so the payload lands):

```bash
hermes plugins enable video_gen/openai_compat
```

```yaml
video_gen:
  provider: openai_compat
  model: Wan2.2-T2V-A14B            # ruled first-tier video alias (§8 Q1), verbatim
  openai_compat:
    base_url: http://10.0.0.x:4000/v1
    api_key_env: HSCC_PROXY_API_KEY  # same bearer as image_gen
```

`HSCC_VIDEO_BASE_URL` env overrides `base_url` (env wins over config). The
`video_gen` toolset must be enabled the same way as `image_gen` (`hermes
tools` → Video Generation writes `video_gen.provider` + toolset for you).

Behaviour worth knowing:

- **A 404 mid-poll fails fast** with `job_failed` and a "re-submit" hint:
  video job stores are process-local (M0 §2 sticky-job rule), so a 404 means
  the serving unit restarted and the job is genuinely gone — polling forever
  would be the bug.
- **The server owns validation.** A size/length the model can't do comes back
  as the server's own 400 (`api_error`), unclamped — that's the Q5 ruling:
  client defaults are defaults, not ceilings.
- Finished MP4s land in `$HERMES_HOME/cache/videos/` (core `provider_media`).

## 3. Revert

Everything here is config plus one enable flag — reverting is mechanical:

```yaml
# image_gen: delete the image_gen block (or set provider back to a SaaS one)
# video_gen: delete the video_gen block, then
hermes plugins disable video_gen/openai_compat
```

Unsetting `HSCC_PROXY_API_KEY` removes the credentials entirely. Nothing fleet
state is touched by any of these keys; no unit restart is needed (the proxy
never learns that a client stopped calling).

## 4. Verified by

`hscc-bootstrap/tests/test_video_gen_openai_compat.py` (29 fake-server
contract tests — fixture job lifecycle per M0 §2, stdlib fake only) and
`test_video_gen_openai_compat_e2e.py` (5 tests against the real
`agent.video_gen_provider` ABC + real `openai` SDK over the same fake). Per
the card ruling there are **no live-media tests**; the live smoke is an
operator action: `POST http://10.0.0.x:4000/v1/videos {"model":
"Wan2.2-T2V-A14B","prompt":"a corgi on a skateboard"}` against a running unit,
then poll the returned id.
