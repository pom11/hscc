"""Role framework shared helpers: paths, toolset policy, spec load/validate, hashing."""
import hashlib
import os
import sys
import yaml

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROLES_DIR = os.path.join(_PLUGIN_DIR, "roles")
BASE_IDENTITY_PATH = os.path.join(_PLUGIN_DIR, "base-identity.md")


def _load_hermes_python():
    """Import ``hscc_daemon/hermes_python.py`` for the canonical root rule.

    Deferred + guarded, same pattern as ``ensure_review_feature._load_hermes_python``:
    the transform lives with the shared ladder (t_53f5b85a) so rolelib cannot
    drift from doctor/backup_util. Returns the module, or None when hscc_daemon
    is not deployed next to this plugin, in which case
    :func:`_fallback_hermes_root` applies the same rule.
    """
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        return None


def _fallback_hermes_root(home):
    """The canonical profiles-grandparent transform when hscc_daemon is absent."""
    home = os.path.expanduser(home) if home else ""
    if not home:
        return home
    parent = os.path.dirname(home)
    if parent and os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def _hermes_root(home):
    """Resolve the hermes ROOT directory from an ambient HERMES_HOME.

    Hermes sets HERMES_HOME to the PROFILE dir (e.g. ~/.hermes/profiles/<name>)
    whenever it runs under that profile. So the hermes root (and hence the
    profiles root, its `profiles/` sibling) is NOT generically $HERMES_HOME:
    when HERMES_HOME's parent is a directory named `profiles`, HERMES_HOME is
    itself a profile dir and the root is the grandparent. Otherwise HERMES_HOME
    is already the root. Robust to both run modes (hermes root vs profile dir).

    Delegates to the canonical ``hermes_python.hermes_root`` (guarded;
    same-rule fallback for a bootstrap-only install). Empty/unset stays empty —
    the caller's default applies elsewhere.
    """
    if not home:
        return ""
    hp = _load_hermes_python()
    if hp is not None:
        return hp.hermes_root(home)
    return _fallback_hermes_root(home)


# HERMES_HOME is the hermes ROOT, independent of whether the ambient env points
# at the root or at a profile dir. PROFILES_DIR is that root's `profiles/`
# sibling — the REAL profiles root (~/.hermes/profiles), never
# <profile-dir>/profiles (the double-nest bug this resolves).
HERMES_HOME = _hermes_root(os.environ.get("HERMES_HOME", "~/.hermes"))
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")

# ── profile-root INTENT: assert which hermes home a write is about to hit ───
#
# WHY THIS EXISTS (t_d39c720e). A bootstrap run that wanted a throwaway home set
# ``HERMES_HOME`` and it did not take: the run wrote the operator's live
# ``~/.hermes/profiles`` and reported success. The reason is a RULE, not an
# import-time accident — ``hermes_constants._hermes_root_of`` returns the native
# home whenever ``HERMES_HOME`` resolves *under* ``~/.hermes``, so a sandbox that
# lives inside the live tree (which is where every Hermes scratch dir lives:
# ``~/.hermes/profiles/<p>/cache/scratch/…``, ``~/.hermes/kanban/…``) silently
# resolves back to the live root. Measured at 9422c740:
#
#   HERMES_HOME=<scratch under ~/.hermes/…>/.hermes  -> ~/.hermes/profiles  (LIVE)
#   HERMES_HOME=/tmp/x/.hermes                       -> /tmp/x/profiles     (moved)
#
# The second reason it was silent: TWO roots are in play in one run and they can
# disagree. ``generator.generate_profile`` writes config.yaml/SOUL.md through
# hermes' own ``get_profile_dir`` (generator.py), while
# ``orchestrators.ensure_orchestrator`` seeds skills under ``PROFILES_DIR`` here.
# In the incident the second one followed the sandbox and the first one did not:
# the checker saw a sandboxed run, the writer hit the operator's tree.
#
# So an intent must be ASSERTED, against the same resolver the writers use,
# before anything is written — never merely computed. Callers that want a
# non-default home use the ``--hermes-home`` flag on the profile-writing
# commands (hscc.py), which applies it and then calls
# :func:`assert_profile_root`. A plain bootstrap passes no flag and changes
# nothing: this code is inert unless an explicit root was requested.

# A valid profile id (hermes_constants.PROFILE_ID_RE) that is not a real profile
# on any machine we run on. ``get_profile_dir`` is pure path arithmetic — it does
# not create or stat anything — so probing it costs nothing and writes nothing.
_PROFILE_ROOT_PROBE = "hscc-root-probe"


class ProfileRootMismatch(RuntimeError):
    """The profiles root the writers resolve is NOT the root the caller asked for.

    Raised by :func:`assert_profile_root` before any write. Nothing has been
    touched when this fires — that is the whole point of the pre-flight.
    """


def _canonical(path):
    """Realpath of an expanded path, for comparing two path *identities*."""
    return os.path.realpath(os.path.expanduser(str(path)))


def native_profiles_dir():
    """The profiles root HERMES' own resolver hands the writer, or ``None``.

    This is the value that decides where ``generate_profile`` actually writes, so
    it is the only thing worth trusting: ``PROFILES_DIR`` can (and in the
    incident did) disagree with it. ``None`` when hermes is not importable — the
    thin-interpreter case, where :data:`PROFILES_DIR` *is* the writer's root.
    """
    try:
        from hermes_cli.profiles import get_profile_dir
    except Exception:      # noqa: BLE001 - thin interpreter / broken install
        return None
    try:
        return os.path.dirname(os.path.normpath(str(get_profile_dir(_PROFILE_ROOT_PROBE))))
    except Exception:      # noqa: BLE001 - a resolver that cannot answer is a mismatch
        return None


def writer_profiles_dir():
    """THE profiles root a profile write will land in.

    Hermes' answer when hermes is importable (that is what the native writer
    uses), else :data:`PROFILES_DIR` (the fallback writer's answer).
    """
    native = native_profiles_dir()
    return native if native is not None else PROFILES_DIR


def apply_hermes_home(root):
    """Point THIS process — hscc's paths and hermes' resolver alike — at *root*.

    The only documented way to target a non-default home for tooling and tests.
    Sets ``HERMES_HOME`` (which hermes' resolver does honour, for a root outside
    the native one) and re-derives :data:`HERMES_HOME` / :data:`PROFILES_DIR`
    from it, so the native writer and the fallback writer and every read path
    agree. Never call this from a real bootstrap: with no argument nothing here
    runs, and the shipped behaviour (write the real home) stays what it is.

    Returns the resolved hermes root. Does NOT verify anything — that is
    :func:`assert_profile_root`, and it must be called after this.
    """
    global HERMES_HOME, PROFILES_DIR
    expanded = os.path.realpath(os.path.expanduser(str(root)))
    os.environ["HERMES_HOME"] = expanded
    HERMES_HOME = _hermes_root(expanded) or expanded
    PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")
    return HERMES_HOME


def assert_profile_root(expected, purpose=""):
    """Fail loudly unless EVERY root the writers resolve *is* ``expected``.

    ``expected`` is the profiles dir the caller intends to be written (the dir
    that will contain ``<profile>/config.yaml``). Both roots in play are checked
    against it — hermes' ``get_profile_dir`` (what the native writer uses) and
    this module's :data:`PROFILES_DIR` (what the fallback writer and the skill
    seeding use) — because a run that pins one and not the other is exactly how
    the t_10ccaf80 incident reported success while writing the live tree.

    Returns the canonical :func:`writer_profiles_dir` on success. Raises
    :class:`ProfileRootMismatch` naming every divergent root, the intent, and the
    env that produced it. Callers must treat that as a hard stop: no write has
    happened yet.
    """
    want = _canonical(expected)
    native = native_profiles_dir()
    divergent = []
    if native is not None and _canonical(native) != want:
        divergent.append(("hermes get_profile_dir", native))
    if _canonical(PROFILES_DIR) != want:
        divergent.append(("rolelib.PROFILES_DIR", PROFILES_DIR))
    if divergent:
        env = ", ".join(f"{k}={os.environ.get(k)!r}"
                        for k in ("HOME", "HERMES_HOME", "HERMES_REAL_HOME"))
        lines = [f"refusing to write: profile-root intent not honoured"
                 f"{' (' + purpose + ')' if purpose else ''}.",
                 f"  requested profiles root: {expected}",
                 f"  env: {env}"]
        for name, got in divergent:
            lines.append(f"  DIVERGENT {name} -> {got}")
        lines.append(
            "  A requested home that lives INSIDE the live hermes root resolves "
            "back to that live root (hermes_constants._hermes_root_of): the write "
            "would land in the operator's tree. Use a home outside it.")
        raise ProfileRootMismatch("\n".join(lines))
    return writer_profiles_dir()


# Full Hermes capability MINUS cluster control. Cluster ops (hscc-cluster) stay
# orchestrator-only — no worker role may change cluster shape. This is the single
# capability boundary in the role system; everything else is uniform.
_FULL_TOOLSETS = [
    "hermes-cli", "kanban", "web", "browser", "terminal", "file",
    "code_execution", "vision", "skills", "todo", "memory",
    "session_search", "clarify", "delegation", "cronjob", "messaging",
]

# The OTHER side of the same single boundary: orchestrator profiles get the full
# set PLUS cluster control. Where a worker may not change cluster shape, an
# orchestrator is the only role that may — it spawns subagents, creates and
# monitors kanban worker tasks, and drives the cluster. `hscc-cluster` and
# `sparkrun` are the cluster tools that must stay orchestrator-only. Everything
# else is identical to _FULL_TOOLSETS; this is the one capability boundary
# visible here so the two sides read side by side.
_ORCH_TOOLSETS = _FULL_TOOLSETS + ["hscc-cluster", "sparkrun"]


def role_toolsets():
    """Toolsets every generated ROLE (worker) profile receives (cluster excluded)."""
    return list(_FULL_TOOLSETS)


def orchestrator_toolsets():
    """Toolsets orchestrator profiles receive: the full set PLUS cluster control."""
    return list(_ORCH_TOOLSETS)


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
VALID_MODEL_TIERS = ("fast", "strong")
# Abstract reasoning tiers. The concrete effort string per tier is MODEL
# vocabulary, so it lives in the applied cluster template's `reasoning:` block
# (recorded at REASONING_POLICY_PATH), never in a role spec.
VALID_REASONING_TIERS = ("max", "standard", "light")
REASONING_POLICY_PATH = os.path.expanduser(
    os.environ.get("HSCC_REASONING_POLICY", "~/.hscc/state/reasoning_policy.json"))


def load_spec(path):
    """Load + validate a role spec YAML. Returns a normalized dict.

    Required: name, identity, routing_description.
    Optional: preload_skills (list, default []), model_tier (str, default "fast"),
    model_endpoint (str, default None), model_name (str, default None).
    Raises ValueError on missing required fields so callers fail loudly.

    model_tier controls which GPU tier a role uses:
      - "fast"  (default): worker proxy at :4000 with 27B-FP8
      - "strong": orchestrator node at :8000 with 35B-A3B-NVFP4

    Optional per-role override (specialized model routing):
      - model_endpoint: a base_url like "http://host:port/v1"
      - model_name: a model name to use with that endpoint
    When model_endpoint is set, it supersedes the tier-resolved endpoint.
    model_name defaults to the tier's model when only the endpoint is set.
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

    model_tier = (data.get("model_tier") or "fast")
    model_tier = str(model_tier).strip().lower()
    if model_tier not in VALID_MODEL_TIERS:
        raise ValueError(
            f"{path}: model_tier must be one of {VALID_MODEL_TIERS}, got '{model_tier}'"
        )

    # Optional per-role model endpoint override (specialized model routing).
    model_endpoint = data.get("model_endpoint")
    if model_endpoint is not None:
        model_endpoint = str(model_endpoint).strip()
        if not model_endpoint:
            raise ValueError(f"{path}: model_endpoint must be a non-empty string")
        if not model_endpoint.startswith("http"):
            raise ValueError(
                f"{path}: model_endpoint must start with 'http', got '{model_endpoint}'"
            )

    model_name = data.get("model_name")
    if model_name is not None:
        model_name = str(model_name).strip()
        if not model_name:
            raise ValueError(f"{path}: model_name must be a non-empty string")

    # Optional reasoning tier; None = orchestrators `max`, everyone else
    # `standard` (see generator.reasoning_tier).
    reasoning_tier = data.get("reasoning_tier")
    if reasoning_tier is not None:
        reasoning_tier = str(reasoning_tier).strip().lower()
        if reasoning_tier not in VALID_REASONING_TIERS:
            raise ValueError(
                f"{path}: reasoning_tier must be one of {VALID_REASONING_TIERS}, "
                f"got '{reasoning_tier}'"
            )

    return {
        "name": str(data["name"]).strip(),
        "identity": str(data["identity"]).rstrip() + "\n",
        "routing_description": str(data["routing_description"]).strip(),
        "preload_skills": [str(s).strip() for s in skills if str(s).strip()],
        "model_tier": model_tier,
        "model_endpoint": model_endpoint,
        "model_name": model_name,
        "reasoning_tier": reasoning_tier,
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
