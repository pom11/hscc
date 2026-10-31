kind: schema
task: t_ec488ce3

- **Media client enablement (M6): `image_gen` → proxy recipe + fleet
  `video_gen` `openai_compat` plugin.** New ops recipe
  `docs/ops/hermes-media.md`: `image_gen.provider=openai` +
  `image_gen.openai.base_url=http://10.0.0.x:4000/v1` with the model id sent
  verbatim (ruled first tier: `Qwen-Image-2.1-FP8` gen,
  `lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang` edit, `Wan2.2-T2V-A14B`
  video — §8 Q1), plus how to revert. Hermes' `video_generate` had no
  OpenAI-compatible provider, so the fleet ships one as a Hermes **plugin**
  (`install/hscc-plugins/video_gen/openai_compat/`, the M0 plugin-first
  placement — no core-hermes edit): `POST /v1/videos` → poll → content
  download per §2, ruling §8 Q5 honoured (no deadline, no size/seconds caps;
  ~5 s poll pacing only), poll-404 fails fast as the process-local job it is.
  `install_payload.PAYLOAD_EXTRA_DESTS` (new): a payload entry may additionally
  deploy at plugins_dir destinations beyond its staging path — required because
  Hermes' scanner recurses only ONE category level below the plugins dir, so
  the plugin must ALSO land at `<plugins>/video_gen/openai_compat/` (key
  `video_gen/openai_compat`, what `plugins.enabled` must carry). Additive, so
  `verify.check_plugin_payload`'s same-name comparisons keep working.
---
kind: Verified
task: t_ec488ce3

- New `hscc-bootstrap/tests/test_video_gen_openai_compat.py` (29 cases, fake
  stdlib server — fixture job lifecycle per M0 §2, no live media) and
  `test_video_gen_openai_compat_e2e.py` (5 cases against the REAL
  `agent.video_gen_provider` ABC + real `openai` SDK over the same fake);
  `test_install_payload.py` 20 → **23** cases (extra-dest deploy at both
  locations + flattened backup names + payload membership).
  `bash scripts/run_tests.sh` ALL GREEN; `python3 scripts/address_guard.py
  --tracked` exit 0.
