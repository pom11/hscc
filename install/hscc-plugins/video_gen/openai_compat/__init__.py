"""OpenAI-compatible video generation backend (HSCC fleet).

Services the fleet's media path: the LiteLLM proxy at ``:4000`` fronts SGLang
Diffusion units, which natively implement the OpenAI async video surface
(M0 design ``docs/design/media-serving.md`` §2: ``POST /v1/videos`` -> poll
``GET /v1/videos/{id}`` -> ``GET /v1/videos/{id}/content``). Selection:
``video_gen.provider: openai_compat`` in config.yaml.

Endpoint: ``HSCC_VIDEO_BASE_URL`` env -> ``video_gen.openai_compat.base_url``
config -> ``http://localhost:4000/v1`` (the proxy, never a node address —
media-serving.md §7). Key: env named by ``video_gen.openai_compat.api_key_env``
-> ``HSCC_PROXY_API_KEY`` -> ``OPENAI_API_KEY`` (the proxy's bearer master key).
Model: ``video_gen.model`` -> ``video_gen.openai_compat.model`` -> the ruled
first-tier video alias (media-serving.md §8 Q1: ``Wan2.2-T2V-A14B``).

Operator ruling Q5 (2026-10-30) binds this provider: NO client-side deadline
and NO size/seconds caps — poll until the job reaches a terminal status, with
one sane pacing rule: ~5 s between polls (``poll_interval_s`` config /
``HSCC_VIDEO_POLL_INTERVAL_S`` env). The server is the authority on what a
model supports; a rejected size/seconds comes back as the server's own 400.

A 404 on poll is final, not transient: video job stores are process-local
(media-serving.md §2 sticky-job rule) and a 404 means the accepting unit
restarted and the job is gone — the client must re-submit, so we fail fast
with ``job_failed`` rather than poll a ghost forever.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    VideoGenProvider,
    error_response,
    save_bytes_video,
    save_url_video,
    success_response,
)
from agent.secret_scope import get_secret_str

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:4000/v1"
DEFAULT_MODEL = "Wan2.2-T2V-A14B"
DEFAULT_POLL_INTERVAL_S = 5.0  # ruling Q5: pacing only, never a deadline

ENV_BASE_URL = "HSCC_VIDEO_BASE_URL"
ENV_API_KEY = "HSCC_PROXY_API_KEY"
ENV_KEY_ENV = "HSCC_PROXY_API_KEY_ENV"  # names another env var holding the key
ENV_POLL_INTERVAL_S = "HSCC_VIDEO_POLL_INTERVAL_S"

# Terminal job statuses. "completed"/"succeeded" are the good ones (SGLang
# uses "completed"; accept the DeepInfra-family synonym), the rest fail.
_TERMINAL = frozenset({"completed", "succeeded", "failed", "error", "cancelled", "canceled", "expired"})
_OK = frozenset({"completed", "succeeded"})

# Tier x aspect-ratio -> OpenAI-style "WxH" request size. These are REQUEST
# DEFAULTS, not ceilings (ruling Q5): an unmapped tier sends no size at all and
# the server applies its own default; a size the model does not support comes
# back as the server's 400. Heights match the COMMON_RESOLUTIONS tiers.
_HEIGHT = {"480p": 480, "540p": 540, "720p": 720, "768p": 768, "1080p": 1080}
_AR = {
    "16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4),
    "3:2": (3, 2), "2:3": (2, 3), "21:9": (21, 9),
}


def _size_for(resolution: Optional[str], aspect_ratio: Optional[str]) -> Optional[str]:
    """``WxH`` for a known tier+ratio; ``None`` (omit the field) when unknown."""
    if not resolution or resolution not in _HEIGHT or aspect_ratio not in _AR:
        return None
    h = _HEIGHT[resolution]
    w_num, w_den = _AR[aspect_ratio]
    w = round(h * w_num / w_den / 2) * 2  # even dimensions are what encoders want
    return f"{w}x{h}"


def _config_block() -> Dict[str, Any]:
    """``video_gen.openai_compat`` from config.yaml; ``{}`` on any failure."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        block = section.get("openai_compat") if isinstance(section, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception as exc:  # noqa: BLE001 — config is best-effort, picker-safe
        logger.debug("Could not load video_gen.openai_compat config: %s", exc)
        return {}


def _first_secret(names: List[str]) -> str:
    """First non-empty value among env-var NAMES, through the profile secret
    scope (os.environ fallback when no scope/multiplexing — see agent.secret_scope)."""
    for name in names:
        if not name:
            continue
        try:
            val = get_secret_str(name).strip()
        except Exception:  # noqa: BLE001 — UnscopedSecretError etc. must not raise out of is_available()
            val = ""
        if val:
            return val
    return ""


class OpenAICompatVideoGenProvider(VideoGenProvider):
    """Video over any OpenAI-compatible ``/v1/videos`` server (fleet proxy)."""

    name = "openai_compat"
    display_name = "OpenAI-compatible video"

    # ---------------------------------------------------------------- config

    def _base_url(self) -> str:
        return (os.environ.get(ENV_BASE_URL, "").strip()
                or str(_config_block().get("base_url") or "").strip()
                or DEFAULT_BASE_URL).rstrip("/")

    def _api_key(self) -> str:
        named = (os.environ.get(ENV_KEY_ENV, "").strip()
                 or str(_config_block().get("api_key_env") or "").strip())
        return _first_secret([named, ENV_API_KEY, "OPENAI_API_KEY"])

    def _poll_interval(self) -> float:
        raw = os.environ.get(ENV_POLL_INTERVAL_S, "").strip() or _config_block().get("poll_interval_s", "")
        try:
            val = float(raw)
            return val if val > 0 else DEFAULT_POLL_INTERVAL_S
        except (TypeError, ValueError):
            return DEFAULT_POLL_INTERVAL_S

    def is_available(self) -> bool:
        """Cheap + offline (picker contract): a bearer key must exist; the base
        URL always has a value (proxy default)."""
        try:
            return bool(self._api_key())
        except Exception:  # noqa: BLE001 — is_available must never raise
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        """Catalog from config only — the proxy's ``/v1/models`` is not
        media-filterable until the ``model_info.mode`` tag lands (M0 §3), so
        live discovery would list chat models here. The picker must stay
        offline anyway: a single configured entry, sent verbatim."""
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly()
            section = cfg.get("video_gen") if isinstance(cfg, dict) else None
            top = section.get("model") if isinstance(section, dict) else None
        except Exception:  # noqa: BLE001
            top = None
        model = (str(top).strip() if isinstance(top, str) else "") \
            or str(_config_block().get("model") or "").strip() or DEFAULT_MODEL
        return [{"id": model, "display": model}]

    def capabilities(self) -> Dict[str, Any]:
        """No ``min_duration``/``max_duration`` keys on purpose: the dynamic
        ``video_generate`` schema only clamps duration when BOTH are present,
        and ruling Q5 forbids client-side length caps."""
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": ["480p", "540p", "720p", "768p", "1080p"],
            "supports_audio": False,
            "supports_negative_prompt": True,
            "supports_seed": True,
            "supports_upscale": False,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {"name": self.display_name, "badge": "self-hosted",
                "tag": "Any OpenAI-compatible /v1/videos server — the HSCC proxy at :4000 (SGLang Diffusion)",
                "env_vars": [{"key": ENV_API_KEY, "prompt": "Proxy bearer key (LiteLLM master key)",
                              "url": "http://<mac-lan>:4000 — see docs/ops/hermes-media.md"}]}

    # ---------------------------------------------------------------- create

    def _reference_kwargs(self, image_url: Optional[str]) -> Dict[str, Any]:
        """Map ``image_url`` onto the wire form. http(s) -> ``reference_url``
        (server fetches it, media-serving.md §2 multipart/reference_url); a
        ``data:`` URL or a local path -> ``input_reference`` bytes (multipart)."""
        if not image_url:
            return {}
        if image_url.startswith("data:"):
            _, _, b64 = image_url.partition(",")
            try:
                return {"input_reference": base64.b64decode(b64)}
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"image_url data URL is not valid base64: {exc}") from exc
        if image_url.startswith(("http://", "https://")):
            return {"reference_url": image_url}
        # local path (tool confinement can hand one over)
        with open(image_url, "rb") as fh:
            return {"input_reference": fh.read()}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = "16:9", resolution: str = "720p",
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,  # unknown kwargs ignored per ABC contract
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            return error_response(error="prompt is required", error_type="invalid_request", provider=self.name)
        api_key = self._api_key()
        if not api_key:
            return error_response(
                error=f"no bearer key: set {ENV_API_KEY} (or video_gen.openai_compat.api_key_env)",
                error_type="missing_credentials", provider=self.name)
        try:
            import openai
        except ImportError:
            return error_response(error="openai Python package not installed (pip install openai)",
                                  error_type="missing_dependency", provider=self.name)

        model_id = (model or "").strip() or (self.list_models()[0]["id"])
        base_url = self._base_url()

        def fail(error: str, error_type: str) -> Dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect_ratio)

        call_kwargs: Dict[str, Any] = {"model": model_id, "prompt": prompt}
        if duration:
            call_kwargs["seconds"] = str(duration)
        size = _size_for(resolution, aspect_ratio)
        if size:
            call_kwargs["size"] = size
        extra_body: Dict[str, Any] = {}
        if negative_prompt:
            extra_body["negative_prompt"] = negative_prompt
        if seed is not None:
            extra_body["seed"] = seed
        if not image_url and reference_image_urls:
            image_url = reference_image_urls[0]
        try:
            call_kwargs.update(self._reference_kwargs(image_url))
        except (ValueError, OSError) as exc:
            return fail(f"image reference could not be read: {exc}", "invalid_request")
        if extra_body:
            call_kwargs["extra_body"] = extra_body

        client_kwargs: Dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        try:  # env-only-proxy client so a macOS system proxy can't swallow the LAN endpoint (#64888)
            from agent.process_bootstrap import build_keepalive_http_client

            http_client = build_keepalive_http_client(base_url)
            if http_client is not None:
                client_kwargs["http_client"] = http_client
        except Exception:  # noqa: BLE001 — plain SDK client still works
            logger.debug("keepalive http client unavailable; using SDK default", exc_info=True)
        client = openai.OpenAI(**client_kwargs)

        try:
            try:
                job = client.videos.create(**call_kwargs)
                # Poll until terminal — ruling Q5: no client deadline. The ONLY
                # early exit is a lost job (404): process-local store, gone with
                # the unit (§2), so re-polling can never succeed.
                while getattr(job, "status", None) not in _TERMINAL:
                    time.sleep(self._poll_interval())
                    try:
                        job = client.videos.retrieve(job.id)
                    except Exception as exc:  # noqa: BLE001 — classify below
                        status_code = getattr(exc, "status_code", None)
                        if status_code == 404:
                            return fail(
                                f"video job {job.id} vanished (status poll 404) — the serving unit "
                                "restarts its job store; re-submit the request", "job_failed")
                        raise
            except Exception as exc:  # noqa: BLE001 — surface any SDK/API failure uniformly
                logger.debug("openai_compat video generation failed", exc_info=True)
                return fail(f"openai_compat video generation failed: {exc}", "api_error")

            status = getattr(job, "status", None)
            if status not in _OK:
                job_error = getattr(job, "error", None)
                return fail(str(job_error) if job_error else f"video job ended with status={status!r}",
                            "job_failed")

            # Output: delivery URL in data[] (relative URLs are resolved against
            # the proxy base, media-serving.md §2 b64_json/url exactness), else
            # the download endpoint GET /v1/videos/{id}/content.
            url = None
            for item in getattr(job, "data", None) or []:
                raw_url = (item.get("url") if isinstance(item, dict) else getattr(item, "url", None)) or None
                if raw_url:
                    # SGLang's url form is root-relative ("/v1/videos/<id>/content");
                    # urljoin against the base replaces the path from the origin, so
                    # this yields .../v1/videos/<id>/content — not a doubled /v1/v1.
                    url = urljoin(base_url, raw_url) if raw_url.startswith("/") else raw_url
                    break
            try:
                if url:
                    # URL built from the operator's own configured base_url =>
                    # trusted origin (LAN proxy is allowed without the SSRF gate).
                    video_ref = str(save_url_video(url, prefix=self.name,
                                                   headers={"Authorization": f"Bearer {api_key}"},
                                                   trusted_origin=True))
                else:
                    video_ref = str(save_bytes_video(client.videos.download_content(job.id).read(),
                                                     prefix=self.name))
            except Exception as exc:  # noqa: BLE001
                if not url:
                    return fail(f"video job succeeded but no output could be retrieved: {exc}", "empty_response")
                logger.debug("openai_compat: local save failed (%s); returning URL", exc)
                video_ref = url

            return success_response(
                video=video_ref, model=model_id, prompt=prompt,
                modality="image" if image_url else "text", aspect_ratio=aspect_ratio,
                duration=duration or 0, provider=self.name,
                extra={"job_id": getattr(job, "id", "")})
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()


def register(ctx) -> None:
    """Plugin entry point — wire ``OpenAICompatVideoGenProvider`` into the registry."""
    ctx.register_video_gen_provider(OpenAICompatVideoGenProvider())
