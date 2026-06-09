"""Role framework shared helpers: paths, toolset policy, spec load/validate, hashing."""
import hashlib
import os

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
