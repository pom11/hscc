"""Test harness for the openai_compat video plugin (card t_ec488ce3).

The suite runs against a real HTTP server on 127.0.0.1 built from the STANDARD
LIBRARY http.server — a fixture, never a live media unit (operator ruling:
fake server only). The job lifecycle (queued -> in_progress -> completed) and
the URL form of the finished job (root-relative ``/v1/videos/<id>/content``)
mirror docs/design/media-serving.md §2.
"""

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_PLUGIN_PATH = (Path(__file__).resolve().parents[2]
                / "install" / "hscc-plugins" / "video_gen" / "openai_compat" / "__init__.py")

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42FAKE-VIDEO-BYTES-PADDING-PADDING0"

# ── fake hermes-core seams (offline suite: no operator env, no live secrets,
#    no real cache dir) ───────────────────────────────────────────────────────

_FAKE_SECRETS = {}   # env-var NAME -> value, populated per test
_CFG = {}            # stand-in for config.yaml (video_gen section)


class _SecretScope:
    """get_secret_str(name) — reads _FAKE_SECRETS, '' when unset (mirrors
    agent.secret_scope with nothing configured)."""

    @staticmethod
    def get_secret_str(name, default=""):
        return _FAKE_SECRETS.get(name, default)


class _ProviderMedia:
    """agent.provider_media save helpers — record calls, write into tmp_path."""

    calls = []
    tmp = None

    @staticmethod
    def save_bytes(kind, raw, *, prefix, extension):
        _ProviderMedia.calls.append(("bytes", kind, len(raw), prefix, extension))
        p = Path(_ProviderMedia.tmp) / f"saved.{extension}"
        p.write_bytes(raw)
        return p

    @staticmethod
    def save_url(kind, url, *, headers=None, trusted_origin=False, **kw):
        _ProviderMedia.calls.append(("url", kind, url, headers, trusted_origin))
        p = Path(_ProviderMedia.tmp) / "saved_from_url.mp4"
        p.write_bytes(FAKE_MP4)
        return p


class _VideoGenProvider:
    """ABC stand-in carrying the module-level helpers the plugin imports."""

    name = ""
    COMMON_ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9")

    def capabilities(self):
        return {}

    def list_models(self):
        return []

    def default_model(self):
        models = self.list_models()
        return models[0].get("id") if models else None

    @staticmethod
    def error_response(**kw):
        return {**kw, "success": False, "video": None}

    @staticmethod
    def success_response(**kw):
        # Mirrors core: `extra` keys merge at top level without overriding.
        extra = kw.pop("extra", None) or {}
        payload = {**kw, "success": True}
        for k, v in extra.items():
            payload.setdefault(k, v)
        return payload

    @staticmethod
    def save_bytes_video(raw, prefix="video", extension="mp4"):
        return _ProviderMedia.save_bytes("videos", raw, prefix=prefix, extension=extension)

    @staticmethod
    def save_url_video(url, prefix="video", headers=None, trusted_origin=False, **kw):
        return _ProviderMedia.save_url("videos", url, headers=headers, trusted_origin=trusted_origin)


class _Obj(dict):
    """Dict with attribute access, like the openai SDK's pydantic models
    (the plugin reads job.status via getattr)."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _to_obj(d):
    if isinstance(d, dict):
        return _Obj({k: _to_obj(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_to_obj(v) for v in d]
    return d


# ── the fake server (SGLang-Diffusion-shaped /v1/videos) ────────────────────

class FakeSGLang:
    def __init__(self):
        self.mode = "happy"          # happy | url | fail | poll404 | create400
        self.base_url = ""
        self.create_calls = []       # parsed JSON bodies of POST /v1/videos
        self.create_multipart = []   # (content_type, raw_body) when multipart
        self.poll_count = 0
        self.content_headers = []    # Authorization headers on content GETs
        self.auth_seen = set()       # every Authorization header observed

    def status_for(self, n):
        if self.mode == "fail":
            return ("failed", {"code": "server_error", "message": "denoise OOM"}) if n >= 1 else ("queued", None)
        if self.mode == "poll404":
            return ("in_progress", None) if n == 1 else ("__gone__", None)
        return ("completed", None)


class _HttpError(Exception):
    def __init__(self, status_code, msg=""):
        super().__init__(msg)
        self.status_code = status_code


def make_handler(fake: FakeSGLang):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence per-request stderr spam
            pass

        def _record_auth(self):
            auth = self.headers.get("authorization", "")
            fake.auth_seen.add(auth)
            return auth

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            auth = self._record_auth()
            if self.path == "/v1/videos":
                ctype = self.headers.get("content-type", "")
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                if fake.mode == "create400":
                    return self._json(400, {"error": {
                        "message": "Invalid size: 1024x1024 is not supported",
                        "type": "invalid_request_error"}})
                model = None
                if "multipart/form-data" in ctype:
                    fake.create_multipart.append((ctype, raw))
                else:
                    body = json.loads(raw or b"{}")
                    fake.create_calls.append(body)
                    model = body.get("model")
                return self._json(200, {"id": "job1", "object": "video", "status": "queued",
                                        "progress": 0, "model": model, "created": 1730000000})
            return self._json(404, {"error": {"message": f"no route {self.path}"}})

        def do_GET(self):
            auth = self._record_auth()
            if self.path == "/v1/videos/job1":
                fake.poll_count += 1
                status, err = fake.status_for(fake.poll_count)
                if status == "__gone__":
                    return self._json(404, {"error": {"message": "job not found"}})
                job = {"id": "job1", "object": "video", "status": status,
                       "progress": 100 if status == "completed" else 40}
                if err:
                    job["error"] = err
                if status == "completed" and fake.mode == "url":
                    job["data"] = [{"url": "/v1/videos/job1/content"}]
                return self._json(200, job)
            if self.path == "/v1/videos/job1/content":
                fake.content_headers.append(auth)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(FAKE_MP4)))
                self.end_headers()
                self.wfile.write(FAKE_MP4)
                return
            return self._json(404, {"error": {"message": f"no route {self.path}"}})

        def do_DELETE(self):
            self._record_auth()
            return self._json(200, {"id": "job1", "object": "video", "deleted": True})

    return Handler


@pytest.fixture()
def fake_server():
    fake = FakeSGLang()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    srv.daemon_threads = True  # keep-alive handlers must not outlive the fixture
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        yield fake
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# ── openai-SDK-shaped stub over httpx (same transport, no SDK import) ───────

def _make_openai_stub():
    import httpx

    mod = type(sys)("openai")

    class OpenAI:
        def __init__(self, api_key, base_url, http_client=None):
            self.api_key = api_key
            self.base_url = base_url.rstrip("/")
            self._client = http_client or httpx.Client(timeout=30.0)
            self.videos = _Videos(self)

        def close(self):
            self._client.close()

    class _Videos:
        def __init__(self, owner):
            self.owner = owner

        def _headers(self):
            return {"Authorization": f"Bearer {self.owner.api_key}"}

        def create(self, *, prompt, model=None, seconds=None, size=None, extra_body=None,
                   input_reference=None, reference_url=None, **kw):
            data = {"prompt": prompt, "model": model}
            if seconds:
                data["seconds"] = seconds
            if size:
                data["size"] = size
            if extra_body:
                data.update(extra_body)
            if reference_url:
                data["reference_url"] = reference_url
            if input_reference is not None:
                # multipart: prompt etc. as fields, the bytes as a file part —
                # the wire shape POST /v1/videos multipart expects.
                files = {"input_reference": ("reference.png", bytes(input_reference))}
                r = self.owner._client.post(self.owner.base_url + "/videos",
                                            headers=self._headers(), data=data, files=files)
            else:
                r = self.owner._client.post(self.owner.base_url + "/videos",
                                           headers=self._headers(), json=data)
            if r.status_code >= 400:
                raise _HttpError(r.status_code, r.text[:200])
            r.raise_for_status()
            return _to_obj(r.json())

        def retrieve(self, video_id):
            r = self.owner._client.get(f"{self.owner.base_url}/videos/{video_id}",
                                       headers=self._headers())
            if r.status_code == 404:
                raise _HttpError(404, "job not found")
            r.raise_for_status()
            return _to_obj(r.json())

        def download_content(self, video_id):
            r = self.owner._client.get(f"{self.owner.base_url}/videos/{video_id}/content",
                                       headers=self._headers())
            if r.status_code >= 400:
                raise _HttpError(r.status_code, r.text[:200])
            r.raise_for_status()

            class _R:
                def read(self_inner):
                    return r.content

            return _R()

    mod.OpenAI = OpenAI
    return mod


# ── plugin import fixture (stubbed seams, restored after) ───────────────────

@pytest.fixture()
def plugin(tmp_path):
    """Import the plugin file with hermes-core seams stubbed. Yields the module."""
    _ProviderMedia.calls = []
    _ProviderMedia.tmp = str(tmp_path)
    _FAKE_SECRETS.clear()
    _CFG.clear()

    spec = importlib.util.spec_from_file_location("hscc_openai_compat_video_plugin", _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None, f"plugin file missing: {_PLUGIN_PATH}"
    module = importlib.util.module_from_spec(spec)

    # Each seam must be a real module object: `from X import Y` resolves Y as an
    # attribute of sys.modules["X"], and a parent needs __path__ to be a package.
    def _mod(name, **attrs):
        m = type(sys)(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    agent_mod = _mod("agent")
    agent_mod.__path__ = []  # package, so submodule attribute imports resolve

    hermes_cli_config = _mod("hermes_cli.config", load_config_readonly=lambda: {"video_gen": _CFG})

    stubs = {
        "agent": agent_mod,
        "agent.secret_scope": _mod("agent.secret_scope", get_secret_str=_SecretScope.get_secret_str),
        "agent.video_gen_provider": _mod(
            "agent.video_gen_provider",
            VideoGenProvider=_VideoGenProvider,
            COMMON_ASPECT_RATIOS=_VideoGenProvider.COMMON_ASPECT_RATIOS,
            error_response=_VideoGenProvider.error_response,
            success_response=_VideoGenProvider.success_response,
            save_bytes_video=_VideoGenProvider.save_bytes_video,
            save_url_video=_VideoGenProvider.save_url_video,
        ),
        "hermes_cli": _mod("hermes_cli"),
        "hermes_cli.config": hermes_cli_config,
        "openai": _make_openai_stub(),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, obj in saved.items():
            if obj is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = obj
        _CFG.clear()
        _FAKE_SECRETS.clear()


def set_key(**names):
    """Declare which fake env vars hold the bearer key (name -> value)."""
    _FAKE_SECRETS.update(names)


def set_cfg(**video_gen):
    """Populate the video_gen config stand-in for one test."""
    _CFG.clear()
    _CFG.update(video_gen)
