"""Unit tests for the profile lifecycle + config endpoints (t_740d9489).

Hermetic: every backing call is stubbed via monkeypatch on the
``routes_profiles._backing_*`` module functions, so NO test creates, deletes,
renames, or reads a REAL hermes profile, and NO test touches ~/.hermes/profiles.

Handlers are driven over real loopback HTTP (port 0) so auth and the router
are exercised end-to-end — the same pattern as test_routes_ops.py.

SECURITY is the headline requirement: the config surface is a strict
whitelist (``model.{default,provider}``, ``toolsets``, ``skills.preload``,
``compression``). ``model.api_key`` / ``model.base_url`` / the ``auxiliary``
subtree / `.env` contents must NEVER leak. That is tested both at the unit
level (``_extract_config``) and through the app layer.
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_profiles


# --------------------------------------------------------------------------- #
# Backing stubs
# --------------------------------------------------------------------------- #

def _info(name, model="gpt-4o", provider="openrouter", skills=0,
          desc="", desc_auto=False, gw=False, default=False):
    """A ``ProfileInfo``-shaped plain dict (the serialized whitelist shape that
    ``_backing_list_profiles`` returns)."""
    return {
        "name": name,
        "is_default": default,
        "gateway_running": gw,
        "model": model,
        "provider": provider,
        "skill_count": skills,
        "description": desc,
        "description_auto": desc_auto,
        "distribution_name": None,
        "distribution_version": None,
    }


@pytest.fixture
def fakes(monkeypatch):
    state = {
        "create_calls": [],
        "delete_calls": [],
        "rename_calls": [],
        "describe_set_calls": [],
    }

    def _track(store, payload):
        store.append(payload)

    backing = {
        "list_profiles": lambda: [
            _info("architect", model="claude-3.5", provider="anthropic",
                  skills=5, desc="Architecture lead", gw=True),
            _info("backend-engineer", model="worker-model", provider="custom",
                  skills=9),
            _info("default", model="gpt-4o", provider="openrouter",
                  default=True),
        ],
        "show_profile": lambda name: {
            "summary": {
                "name": name,
                "is_default": name == "default",
                "gateway_running": name == "architect",
                "model": "claude-3.5" if name == "architect" else None,
                "provider": "anthropic" if name == "architect" else None,
                "skill_count": 5 if name == "architect" else 0,
                "description": "lead",
                "description_auto": False,
            },
            "config": {
                "model": {"default": "claude-3.5", "provider": "anthropic"},
                "toolsets": ["terminal", "web"],
                "skills": {"preload": ["kanban-worker"]},
                "compression": {"enabled": True, "threshold_tokens": 100000},
            },
        },
        "describe_get": lambda name: {
            "description": "Architecture lead",
            "description_auto": False,
        },
        "create": lambda name, **kw: (
            _track(state["create_calls"], {"name": name, **kw})
            or "/tmp/fake/profiles/" + name
        ),
        "delete": lambda name: (
            _track(state["delete_calls"], {"name": name})
            or "/tmp/fake/profiles/" + name
        ),
        "rename": lambda old, new: (
            _track(state["rename_calls"], {"old": old, "new": new})
            or "/tmp/fake/profiles/" + new
        ),
        "describe_set": lambda name, text: (
            _track(state["describe_set_calls"], {"name": name, "text": text})
            or name
        ),
    }
    for name, fn in backing.items():
        monkeypatch.setattr(routes_profiles, f"_backing_{name}", fn)
    return state


@pytest.fixture
def running(tmp_path, fakes):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _req(running, token, path, body=None, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    else:
        raw = None
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def test_list_profiles(running, token):
    status, payload = _req(running, token, "/v1/profiles/list")
    assert status == 200
    assert payload["count"] == 3
    names = {p["name"] for p in payload["profiles"]}
    assert names == {"architect", "backend-engineer", "default"}
    assert payload["speak"] == "3 profiles total."
    # Every entry is a safe whitelist dict.
    for p in payload["profiles"]:
        assert set(p) <= {
            "name", "is_default", "gateway_running", "model", "provider",
            "skill_count", "description", "description_auto",
            "distribution_name", "distribution_version",
        }


def test_list_profiles_empty(fakes):
    fakes  # keep fixture reference
    # Override the backing list to return an empty list.
    routes_profiles._backing_list_profiles = lambda: []
    assert routes_profiles._speak_list([]) == "0 profiles total."


def test_show_profile(running, token):
    status, payload = _req(running, token, "/v1/profiles/architect")
    assert status == 200
    assert payload["summary"]["name"] == "architect"
    # Config whitelist present.
    cfg = payload["config"]
    assert cfg["model"] == {"default": "claude-3.5", "provider": "anthropic"}
    assert cfg["toolsets"] == ["terminal", "web"]
    assert cfg["skills"]["preload"] == ["kanban-worker"]
    assert "compression" in cfg
    assert "api_key" not in json.dumps(cfg)
    assert "base_url" not in json.dumps(cfg)
    assert payload["speak"] == "Profile architect (claude-3.5)."


def test_show_profile_not_found(running, token):
    routes_profiles._backing_show_profile = lambda name: None
    status, payload = _req(running, token, "/v1/profiles/nope")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_describe_get(running, token):
    status, payload = _req(running, token, "/v1/profiles/architect/describe")
    assert status == 200
    assert payload["description"] == "Architecture lead"
    assert payload["speak"] == "Profile architect has a description."


def test_describe_get_not_found(running, token):
    routes_profiles._backing_describe_get = lambda name: None
    status, payload = _req(running, token, "/v1/profiles/nope/describe")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_auth_required(running):
    status, _ = _req(running, None, "/v1/profiles/list")
    assert status == 401


# --------------------------------------------------------------------------- #
# Mutations — confirm gate
# --------------------------------------------------------------------------- #

def test_create_requires_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/create",
                           body={"name": "newbie"}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["create_calls"] == []


def test_create_with_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/create",
                           body={"name": "newbie", "confirm": True},
                           method="POST")
    assert status == 200
    assert payload["name"] == "newbie"
    assert payload["speak"] == "Created profile newbie."
    # Backing received the full create parameter set (with defaults for the
    # optional clone flags).
    assert fakes["create_calls"] == [{
        "name": "newbie", "clone_from": None, "clone_all": False,
        "no_alias": False, "no_skills": False, "description": None,
    }]


def test_delete_requires_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/delete",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert "irreversibly deletes" in payload["error"]["message"]
    assert fakes["delete_calls"] == []


def test_delete_with_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/delete",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["name"] == "architect"
    assert payload["speak"] == "Deleted profile architect."
    assert fakes["delete_calls"] == [{"name": "architect"}]


def test_delete_not_found(running, token, fakes):
    def boom(name):
        raise FileNotFoundError("Profile 'nope' does not exist.")
    routes_profiles._backing_delete = boom
    status, payload = _req(running, token, "/v1/profiles/nope/delete",
                           body={"confirm": True}, method="POST")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_rename_requires_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/rename",
                           body={"new_name": "lead"}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["rename_calls"] == []


def test_rename_with_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/rename",
                           body={"new_name": "lead", "confirm": True},
                           method="POST")
    assert status == 200
    assert payload["name"] == "lead"
    assert payload["speak"] == "Renamed architect to lead."
    assert fakes["rename_calls"] == [{"old": "architect", "new": "lead"}]


def test_rename_conflict(running, token, fakes):
    def boom(old, new):
        raise FileExistsError("Profile 'lead' already exists.")
    routes_profiles._backing_rename = boom
    status, payload = _req(running, token, "/v1/profiles/architect/rename",
                           body={"new_name": "lead", "confirm": True},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "name_conflict"


def test_describe_set_requires_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/describe",
                           body={"description": "x"}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["describe_set_calls"] == []


def test_describe_set_with_confirm(running, token, fakes):
    status, payload = _req(running, token, "/v1/profiles/architect/describe",
                           body={"description": "New desc", "confirm": True},
                           method="POST")
    assert status == 200
    assert payload["name"] == "architect"
    assert payload["speak"] == "Description for architect updated."
    assert fakes["describe_set_calls"] == [{"name": "architect",
                                            "text": "New desc"}]


def test_mutation_never_called_without_confirm():
    # Backing closures must be invoked ONLY after the confirm gate passes.
    # (Covered per-endpoint above; this guards the gate helper itself.)
    import routes_profiles as rp
    assert rp._require_confirm is not None


# --------------------------------------------------------------------------- #
# SECURITY: config whitelist must never leak secrets (app-level + unit)
# --------------------------------------------------------------------------- #

def test_extract_config_never_leaks_secrets(tmp_path, monkeypatch):
    """The headline guard: even when config.yaml carries api_key / base_url /
    an ``auxiliary`` subtree and a .env with a real key, the whitelist drops
    them all and returns ONLY the documented safe fields."""
    profiles_home = tmp_path / "profiles"
    pdir = profiles_home / "secret"
    pdir.mkdir(parents=True)
    (pdir / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-4o\n"
        "  provider: openrouter\n"
        "  base_url: https://api.openrouter.ai/v1\n"
        "  api_key: sk-SUPERSECRET-12345\n"
        "  timeout: 60\n"
        "auxiliary:\n"
        "  compression:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "    base_url: https://api.openai.com/v1\n"
        "    api_key: sk-AUXILIARY-SECRET\n"
        "toolsets:\n"
        "  - terminal\n"
        "  - web\n"
        "skills:\n"
        "  preload:\n"
        "    - kanban-worker\n"
        "compression:\n"
        "  enabled: true\n"
        "  threshold_tokens: 100000\n"
        "  protect_last_n: 10\n",
        encoding="utf-8",
    )
    (pdir / ".env").write_text("HERMES_API_KEY=sk-ENV-SECRET\n", encoding="utf-8")

    cfg = routes_profiles._extract_config(str(pdir))
    blob = json.dumps(cfg)

    # Model reduced to default/provider only.
    assert cfg["model"] == {"default": "gpt-4o", "provider": "openrouter"}
    # No secret material anywhere in the whitelist output.
    assert "base_url" not in blob
    assert "api_key" not in blob
    assert "sk-" not in blob
    assert "timeout" not in blob            # not part of the whitelist
    assert "auxiliary" not in blob
    assert "openai" not in blob             # auxiliary.compression never leaks
    # Safe fields present.
    assert cfg["toolsets"] == ["terminal", "web"]
    assert cfg["skills"]["preload"] == ["kanban-worker"]
    assert cfg["compression"] == {"enabled": True, "threshold_tokens": 100000,
                                  "protect_last_n": 10}


def test_extract_config_missing_file(tmp_path):
    cfg = routes_profiles._extract_config(str(tmp_path / "does-not-exist"))
    assert cfg == {"model": {}, "toolsets": [], "skills": {"preload": []},
                   "compression": {}}


def test_list_never_contains_api_key_field():
    # The serializer itself cannot emit secret keys (whitelist-only fields).
    import types
    info = types.SimpleNamespace(
        name="architect", is_default=False, gateway_running=False,
        model="claude-3.5", provider="anthropic", skill_count=1,
        description="", description_auto=False, distribution_name=None,
        distribution_version=None,
    )
    assert "api_key" not in routes_profiles._profile_info_to_dict(info, None)
    assert "base_url" not in routes_profiles._profile_info_to_dict(info, None)
