"""Role framework shared helpers: paths, toolset policy, spec load/validate, hashing."""
import hashlib
import os
import yaml

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROLES_DIR = os.path.join(_PLUGIN_DIR, "roles")
BASE_IDENTITY_PATH = os.path.join(_PLUGIN_DIR, "base-identity.md")
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")

# Full Hermes capability MINUS cluster control. Cluster ops (hscc-cluster) stay
# orchestrator-only — no worker role may change cluster shape. This is the single
# capability boundary in the role system; everything else is uniform.
_FULL_TOOLSETS = [
    "hermes-cli", "kanban", "web", "browser", "terminal", "file",
    "code_execution", "vision", "skills", "todo", "memory",
    "session_search", "clarify", "delegation", "cronjob", "messaging",
]


def role_toolsets():
    """Toolsets every generated role profile receives (cluster excluded)."""
    return list(_FULL_TOOLSETS)


def file_md5(path):
    """md5 of a file's bytes, or None if absent. Used for idempotent writes."""
    if not os.path.exists(path):
        return None
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


REQUIRED_FIELDS = ("name", "identity", "routing_description")


def load_spec(path):
    """Load + validate a role spec YAML. Returns a normalized dict.

    Required: name, identity, routing_description. Optional: preload_skills (list, default []).
    Raises ValueError on missing required fields so callers fail loudly.
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: spec must be a YAML mapping")
    for field in REQUIRED_FIELDS:
        if not data.get(field) or not str(data[field]).strip():
            raise ValueError(f"{path}: missing required field '{field}'")
    skills = data.get("preload_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    if not isinstance(skills, list):
        raise ValueError(f"{path}: preload_skills must be a list")
    return {
        "name": str(data["name"]).strip(),
        "identity": str(data["identity"]).rstrip() + "\n",
        "routing_description": str(data["routing_description"]).strip(),
        "preload_skills": [str(s).strip() for s in skills if str(s).strip()],
    }


def list_spec_files():
    """All role spec file paths under ROLES_DIR, sorted by name."""
    if not os.path.isdir(ROLES_DIR):
        return []
    return sorted(
        os.path.join(ROLES_DIR, f)
        for f in os.listdir(ROLES_DIR)
        if f.endswith(".yaml")
    )
