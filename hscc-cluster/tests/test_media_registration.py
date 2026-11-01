"""Offline gate for the media proxy-registration contract (t_966db5b3).

FAKE BACKEND ONLY (card rule): the "media server" is a stdlib http.server on
127.0.0.1:0 serving a fixture /v1/models shaped like SGLang Diffusion's
(mode-tagged, M0 §2); nothing here contacts :4000, sparkrun, or a fleet node,
and the suite's autouse sparkrun fence stays satisfied because no sparkrun
argv is ever built.

What is pinned, and why each pin exists:
* the kind<->mode pair and the task-type mapping — M4/M5/M6 must not each
  invent a spelling of "is this model an image model";
* the exact model_list entry shape sparkrun's build_litellm_config emits
  (engine.py:165-181) PLUS the one model_info.mode key the proposed fork patch
  adds — when the patch lands, the fork's output and this expectation are the
  same object (CI has no sparkrun, so the comparison is by literal shape, the
  same way test_media_recipes.py pins against a cached server_args fixture);
* autodiscovery as THE mechanism: the registration path must never grow a
  proxy.yaml write (the module's imports are asserted, not its prose);
* fail-closed modes: ambiguous/unknown task types and kind-vs-advertised
  contradictions raise instead of emitting a chat-listed media model.
"""

import http.server
import json
import threading

import pytest

import media_registration as mr


# ── fixture payloads (SGLang Diffusion shapes, M0 §2) ───────────────────────

WAN_MODELS = {
    "object": "list",
    "data": [{
        "id": "Wan-AI/Wan2.2-T2V-A14B",
        "object": "model",
        "created": 1,
        "owned_by": "sglang",
        "task_type": "T2V",
        "supported_task_types": ["T2V", "I2V"],
        "dit_precision": "fp8",
        "vae_precision": "fp16",
        "pipeline_class": "Wan22Pipeline",
    }],
}

QWEN_IMAGE_MODELS = {
    "object": "list",
    "data": [{
        "id": "Qwen/Qwen-Image-2.1",
        "object": "model",
        "created": 1,
        "owned_by": "sglang",
        "task_type": "T2I",
        "supported_task_types": ["T2I", "I2I"],
        "dit_precision": "fp8",
        "vae_precision": "fp16",
        "pipeline_class": "QwenImagePipeline",
    }],
}

CHAT_MODELS = {
    "object": "list",
    "data": [{
        "id": "worker-model", "object": "model", "created": 1,
        "owned_by": "vllm", "max_model_len": 131072,
    }],
}

MIXED_MODELS = {
    "object": "list",
    "data": [
        {"id": "chat-unit", "object": "model", "owned_by": "vllm",
         "max_model_len": 32768},
        {"id": "flux-dev", "object": "model", "owned_by": "sglang",
         "task_type": "T2I", "supported_task_types": ["T2I"]},
        {"id": "ltx-video", "object": "model", "owned_by": "sglang",
         "task_type": "T2V", "supported_task_types": ["T2V", "I2V"]},
    ],
}


# ── fake media server (the card's "fake media server" requirement) ──────────

class _FakeMediaHandler(http.server.BaseHTTPRequestHandler):
    """Serves one fixture /v1/models; any other path is a 404 (no real proxy)."""

    payload = CHAT_MODELS
    status = 200

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/v1/models":
            body = json.dumps(self.payload).encode()
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 (http.server API)
        pass


class _FakeMediaServer:
    """Loopback fixture server; context-managed so teardown always joins."""

    def __init__(self, payload):
        handler = type("Bound", (_FakeMediaHandler,), {"payload": payload})
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.thread.join(timeout=5)
        self.srv.server_close()

    @property
    def host_port(self):
        host, port, *_ = self.srv.server_address
        return f"{host}:{port}"

    @property
    def url(self):
        host, port, *_ = self.srv.server_address
        return f"http://{host}:{port}/v1/models"

    def fetch_models(self):
        import urllib.request
        with urllib.request.urlopen(self.url, timeout=5) as r:
            return json.loads(r.read().decode())


# ── the pair: hscc kind <-> LiteLLM mode ────────────────────────────────────

class TestKindModePair:
    def test_image_and_video_map_to_litellm_enums(self):
        assert mr.KIND_TO_MODE == {"image": "image_generation",
                                   "video": "video_generation"}
        assert mr.MEDIA_MODES == ("image_generation", "video_generation")

    def test_mode_for_kind_is_case_and_space_tolerant(self):
        assert mr.mode_for_kind("Image") == "image_generation"
        assert mr.mode_for_kind(" video ") == "video_generation"

    def test_chat_and_garbage_kinds_map_to_none_not_a_mode(self):
        # None (not "chat") — callers validate kind vocabulary themselves;
        # inventing a mode here would paper over a template bug.
        assert mr.mode_for_kind("chat") is None
        assert mr.mode_for_kind("audio") is None
        assert mr.mode_for_kind(None) is None


class TestTaskTypeMapping:
    """M0 §3 verbatim: *2I|i2i -> image_generation, *2V|i2v|t2v -> video."""

    @pytest.mark.parametrize("tok", ["T2I", "I2I", "t2i", " EDIT2I ",
                                     "image_generation", "IMAGE"])
    def test_image_shapes(self, tok):
        assert mr.mode_from_task_types(tok) == "image_generation"

    @pytest.mark.parametrize("tok", ["T2V", "I2V", "t2v", "vid2v",
                                     "video_generation", "VIDEO"])
    def test_video_shapes(self, tok):
        assert mr.mode_from_task_types(tok) == "video_generation"

    def test_primary_task_type_wins_over_the_list(self):
        got = mr.mode_from_task_types("T2I", "I2I", primary="T2V")
        assert got == "video_generation"

    def test_agreeing_tokens_map(self):
        assert mr.mode_from_task_types("T2V", "I2V") == "video_generation"

    def test_mixed_families_return_none_never_chat(self):
        # A unit advertising BOTH sides cannot get a per-deployment mode tag
        # from discovery; fail-closed so the operator declares kind instead.
        assert mr.mode_from_task_types("T2I", "T2V") is None

    def test_unknown_enums_return_none(self):
        # The doctor leg flags these UNVERIFIED (M0 §4) instead of guessing.
        assert mr.mode_from_task_types("EMBEDDING") is None
        assert mr.mode_from_task_types() is None


class TestAdvertisedModelsFromFakeServer:
    """fixture /v1/models over real (loopback) HTTP — the discovery input."""

    def test_wan_advertises_video_mode(self):
        with _FakeMediaServer(WAN_MODELS) as srv:
            media = mr.advertised_media_models(srv.fetch_models())
        assert media == [{"model_name": "Wan-AI/Wan2.2-T2V-A14B",
                          "mode": "video_generation",
                          "task_types": ["T2V", "I2V"]}]

    def test_qwen_image_advertises_image_mode(self):
        with _FakeMediaServer(QWEN_IMAGE_MODELS) as srv:
            media = mr.advertised_media_models(srv.fetch_models())
        assert [m["mode"] for m in media] == ["image_generation"]

    def test_chat_backend_advertises_nothing_media(self):
        with _FakeMediaServer(CHAT_MODELS) as srv:
            assert mr.advertised_media_models(srv.fetch_models()) == []

    def test_mixed_fleet_yields_only_the_media_entries(self):
        with _FakeMediaServer(MIXED_MODELS) as srv:
            media = mr.advertised_media_models(srv.fetch_models())
        assert {m["model_name"] for m in media} == {"flux-dev", "ltx-video"}
        assert {m["mode"] for m in media} == {"image_generation",
                                              "video_generation"}

    def test_unparseable_body_is_empty_not_a_crash(self):
        assert mr.advertised_media_models(None) == []
        assert mr.advertised_media_models({"data": "garbage"}) == []


# ── THE emitted entry: model_list shape + mode (card acceptance core) ────────

class TestEmittedModelListEntry:
    def test_image_unit_entry_carries_image_generation_mode(self):
        # The fixture media unit of the acceptance sentence: a declared
        # kind=image unit at 10.0.0.11:30002 must register as an
        # image_generation deployment.
        e = mr.media_model_list_entry("Qwen/Qwen-Image-2.1", "10.0.0.11:30002",
                                      kind="image")
        assert e == {
            "model_name": "Qwen/Qwen-Image-2.1",
            "litellm_params": {
                "model": "openai/Qwen/Qwen-Image-2.1",
                "api_base": "http://10.0.0.11:30002/v1",
                "api_key": "not-needed",
            },
            "model_info": {"mode": "image_generation"},
        }

    def test_video_unit_entry_carries_video_generation_mode(self):
        e = mr.media_model_list_entry("Wan-AI/Wan2.2-T2V-A14B",
                                      "10.0.0.12:30003", kind="video")
        assert e["model_info"]["mode"] == "video_generation"
        assert e["litellm_params"]["api_base"] == "http://10.0.0.12:30003/v1"

    def test_shape_matches_sparkrun_emission_plus_the_one_mode_key(self):
        # sparkrun build_litellm_config (engine.py:165-181) emits exactly
        # model_name + litellm_params{model, api_base, api_key} (+ model_info
        # only when max_model_len is known). The proposed patch adds mode to
        # model_info. With a known window BOTH keys must appear — pinning the
        # merged shape so the fork patch and this contract cannot drift.
        e = mr.media_model_list_entry("ltx-video", "10.0.0.13:30004",
                                      kind="video", max_input_tokens=8192)
        assert set(e) == {"model_name", "litellm_params", "model_info"}
        assert set(e["litellm_params"]) == {"model", "api_base", "api_key"}
        assert e["model_info"] == {"mode": "video_generation",
                                   "max_input_tokens": 8192}

    def test_no_window_media_units_get_mode_only(self):
        # M0 §3: diffusion /v1/models reports max_model_len null — the key
        # must be ABSENT (sparkrun's `if ep.max_model_len:` gate), not null.
        e = mr.media_model_list_entry("ltx-video", "10.0.0.13:30004",
                                      kind="video")
        assert e["model_info"] == {"mode": "video_generation"}

    def test_explicit_http_url_passes_through(self):
        e = mr.media_model_list_entry("m", "http://10.0.0.9:30010/v1",
                                      kind="image")
        assert e["litellm_params"]["api_base"] == "http://10.0.0.9:30010/v1"

    def test_kind_must_agree_with_advertised_task_types(self):
        # kind: video on a unit advertising T2I is a template bug — the entry
        # would route video traffic to an image engine. Refused, naming both.
        with pytest.raises(mr.MediaRegistrationError) as exc:
            mr.media_model_list_entry("flux-dev", "10.0.0.11:30002",
                                      kind="video", task_types=["T2I", "I2I"])
        msg = str(exc.value)
        assert "video" in msg and "T2I" in msg

    def test_discovery_side_task_types_need_no_kind(self):
        e = mr.media_model_list_entry("flux-dev", "10.0.0.11:30002",
                                      task_types=["T2I"])
        assert e["model_info"]["mode"] == "image_generation"

    def test_no_decidable_mode_refuses_instead_of_listing_as_chat(self):
        with pytest.raises(mr.MediaRegistrationError) as exc:
            mr.media_model_list_entry("mystery", "10.0.0.11:30002",
                                      task_types=["EMBEDDING"])
        assert "chat" in str(exc.value)

    def test_bad_api_base_refused(self):
        with pytest.raises(mr.MediaRegistrationError):
            mr.media_model_list_entry("m", "not-a-host-port", kind="image")


class TestEntriesForUnits:
    def test_media_units_emit_one_entry_per_advertised_name(self):
        # M0 §3 step 1: --served-model-name "<concrete> <alias>" — both names
        # must carry the SAME mode (an alias listed without mode is how a
        # media model gets believed to be chat).
        units = [{"model": "flux-dev", "alias": "image-model",
                  "kind": "image", "node": "10.0.0.11", "port": 30002}]
        entries = mr.media_entries_for_units(units)
        assert [e["model_name"] for e in entries] == ["flux-dev",
                                                      "image-model"]
        assert all(e["model_info"]["mode"] == "image_generation"
                   for e in entries)
        assert entries[1]["litellm_params"]["model"] == "openai/image-model"

    def test_chat_units_are_skipped_byte_identically(self):
        # §7 doctrine: a template with no media units must gain ZERO entries —
        # discovery keeps owning chat registrations exactly as before.
        units = [{"model": "big", "alias": "worker-model", "node": "10.0.0.5",
                  "port": 30001},
                 {"model": "orch", "node": "10.0.0.1", "port": 30000}]
        assert mr.media_entries_for_units(units) == []

    def test_mixed_plan_registers_only_media(self):
        units = [
            {"model": "big", "alias": "worker-model", "node": "10.0.0.5",
             "port": 30001},
            {"model": "Wan-AI/Wan2.2-T2V-A14B", "kind": "video",
             "node": "10.0.0.12", "port": 30003},
            {"model": "flux-dev", "kind": "image", "node": "10.0.0.11",
             "port": 30002, "task_types": ["T2I"]},
        ]
        entries = mr.media_entries_for_units(units)
        assert [e["model_name"] for e in entries] == [
            "Wan-AI/Wan2.2-T2V-A14B", "flux-dev"]
        assert [e["model_info"]["mode"] for e in entries] == [
            "video_generation", "image_generation"]

    def test_discovery_derived_units_register_without_declaration(self):
        # The autodiscovery loop end-to-end (fake server -> entries): a unit
        # nobody declared kind for still tags correctly from its enums.
        with _FakeMediaServer(WAN_MODELS) as srv:
            host_port = srv.host_port
            media = mr.advertised_media_models(srv.fetch_models())
        entries = mr.media_entries_for_units(
            [{"model": m["model_name"], "task_types": m["task_types"],
              "node": host_port.split(":")[0], "port": int(host_port.split(":")[1])}
             for m in media if m["mode"]])
        assert [e["model_info"]["mode"] for e in entries] == ["video_generation"]

    def test_duplicate_registration_is_deduped_like_sparkrun(self):
        u = {"model": "flux-dev", "alias": "image-model", "kind": "image",
            "node": "10.0.0.11", "port": 30002}
        assert len(mr.media_entries_for_units([u, dict(u)])) == 2

    def test_media_unit_without_endpoint_refused(self):
        with pytest.raises(mr.MediaRegistrationError) as exc:
            mr.media_entries_for_units([{"model": "flux-dev",
                                         "kind": "image"}])
        assert "node+port" in str(exc.value)

    def test_ambiguous_media_unit_refused_naming_the_model(self):
        with pytest.raises(mr.MediaRegistrationError) as exc:
            mr.media_entries_for_units([{"model": "confused-unit",
                                         "task_types": ["T2I", "T2V"],
                                         "node": "10.0.0.14", "port": 30005}])
        assert "confused-unit" in str(exc.value)


class TestMechanismDecisionPinned:
    """The card's decision is autodiscovery — hscc must NEVER emit proxy.yaml
    model_list entries (they would be unread by sparkrun AND read as drift by
    declaration_agreement). Import-guard instead of prose: if a future change
    reaches for the proxy write path from this module, this test fails first.
    """

    def test_module_never_touches_proxy_writes_or_subprocess(self):
        import ast
        import pathlib
        src = pathlib.Path(mr.__file__).read_text()
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        assert "subprocess" not in imported
        assert "sparkrun_bridge" not in imported
        assert "yaml" not in imported
        # And the decision itself stays stated where a reader will hit it.
        assert "autodiscovery" in src.split('"""')[1]
