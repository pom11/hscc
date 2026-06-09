"""Author new role specs from a name + description (the description->spec stage)."""
import os
import re
import yaml
import rolelib

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_DEFAULT_PRELOAD = ["test-driven-development", "verification-before-completion"]


def create_role(name, description, preload_skills=None):
    """Write a new role spec YAML from name + description. Returns the path.

    Raises ValueError on an invalid name or if the role already exists (never
    overwrites — a human/agent must delete first to recreate).
    """
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid role name {name!r} — use lowercase letters, digits, dashes")
    os.makedirs(rolelib.ROLES_DIR, exist_ok=True)
    path = os.path.join(rolelib.ROLES_DIR, f"{name}.yaml")
    if os.path.exists(path):
        raise ValueError(f"role {name!r} already exists at {path}")
    desc = " ".join((description or "").split()).strip()
    if not desc:
        raise ValueError("description is required to author a role")
    skills = preload_skills if preload_skills is not None else list(_DEFAULT_PRELOAD)
    identity = (
        f"You are the {name} specialist. {desc} "
        f"You apply the fleet's shared values and own exactly the task you are "
        f"given."
    )
    spec_yaml = yaml.safe_dump(
        {"name": name, "identity": identity + "\n", "preload_skills": skills},
        default_flow_style=False, sort_keys=False, allow_unicode=True,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(spec_yaml)
    return path
