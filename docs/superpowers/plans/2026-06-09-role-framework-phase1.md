# Role Framework (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `hscc-roles` plugin — role specs (data) + a generator that turns each spec into a loadable Hermes profile with a layered SOUL and full-capability-minus-cluster toolset, plus a `create` command that authors new role specs.

**Architecture:** A new pure-Python stdlib plugin at `~/.hermes/plugins/hscc-roles/`. Roles are YAML spec files under `roles/`. `generator.py` composes each role's SOUL from `base-identity.md` + the spec's `identity` + a thin operational block, then materializes a Hermes profile via the native `hermes profile create --clone-from` mechanism, overwriting SOUL.md + description + toolsets. Idempotent via md5 hash-diff (mirrors `hscc-skills`). No Hermes-core changes in Phase 1 — only profile artifacts are produced. No services started.

**Tech Stack:** Python 3 stdlib only (json, os, hashlib, subprocess, pathlib, yaml via the hermes venv), pytest. Mirrors existing `hscc-skills` / `hscc-cluster` plugin conventions.

**Scope:** Phase 1 ONLY (role framework). Pipeline, reviewer loop, autonomy governor, and the spawn base_url core change are later phases. This plan produces working, testable software on its own: after it, the 4 starter role profiles exist and load.

**Out of scope (later phases):** dispatch/spawn changes, brainstorm→kanban pipeline, reviewer loop, autonomy switch, auto-role-creation during decompose.

---

## File Structure

- `plugins/hscc-roles/hscc.py` — CLI entry (`create`, `generate`, `list`, `validate`). Thin dispatch over the modules below.
- `plugins/hscc-roles/rolelib.py` — spec load/validate, paths, hash helpers, the full-capability-minus-cluster toolset constant.
- `plugins/hscc-roles/generator.py` — SOUL composition + profile materialization (the spec→profile build).
- `plugins/hscc-roles/author.py` — `create`: name+description → new role spec file (the description→spec author).
- `plugins/hscc-roles/base-identity.md` — Layer-1 shared character text.
- `plugins/hscc-roles/roles/{orchestrator,architect,coder,reviewer,qa}.yaml` — the 5 starter specs.
- `plugins/hscc-roles/plugin.yaml` — plugin manifest.
- `plugins/hscc-roles/tests/{conftest.py,test_rolelib.py,test_generator.py,test_author.py}` — pytest suite.

Each file has one responsibility: `rolelib` = data+validation, `generator` = build, `author` = create, `hscc.py` = CLI glue.

---

## Task 1: Plugin skeleton + rolelib paths/constants

**Files:**
- Create: `plugins/hscc-roles/rolelib.py`
- Create: `plugins/hscc-roles/tests/conftest.py`
- Test: `plugins/hscc-roles/tests/test_rolelib.py`

- [ ] **Step 1: Write the failing test**

`plugins/hscc-roles/tests/test_rolelib.py`:
```python
import os
import rolelib


def test_full_toolset_excludes_cluster():
    ts = rolelib.role_toolsets()
    assert "hscc-cluster" not in ts
    assert "hermes-cli" in ts
    assert "kanban" in ts


def test_paths_are_under_plugin_dir():
    assert rolelib.ROLES_DIR.endswith("hscc-roles/roles")
    assert rolelib.BASE_IDENTITY_PATH.endswith("hscc-roles/base-identity.md")
```

`plugins/hscc-roles/tests/conftest.py`:
```python
import os
import sys

# Make the plugin modules importable as top-level (mirrors hscc-cluster tests).
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rolelib'`

- [ ] **Step 3: Write minimal implementation**

`plugins/hscc-roles/rolelib.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/rolelib.py hscc-roles/tests/conftest.py hscc-roles/tests/test_rolelib.py
git commit -m "feat(hscc-roles): plugin skeleton + toolset policy (cluster excluded)"
```

---

## Task 2: Spec loading + validation

**Files:**
- Modify: `plugins/hscc-roles/rolelib.py`
- Test: `plugins/hscc-roles/tests/test_rolelib.py`

- [ ] **Step 1: Write the failing test**

Append to `plugins/hscc-roles/tests/test_rolelib.py`:
```python
import pytest


def test_load_spec_valid(tmp_path):
    spec_file = tmp_path / "coder.yaml"
    spec_file.write_text(
        "name: coder\n"
        "identity: |\n"
        "  You build things.\n"
        "preload_skills: [test-driven-development]\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["name"] == "coder"
    assert "You build things." in spec["identity"]
    assert spec["preload_skills"] == ["test-driven-development"]


def test_load_spec_missing_name_raises(tmp_path):
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text("identity: hi\n")
    with pytest.raises(ValueError, match="missing required field 'name'"):
        rolelib.load_spec(str(spec_file))


def test_load_spec_missing_identity_raises(tmp_path):
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text("name: x\n")
    with pytest.raises(ValueError, match="missing required field 'identity'"):
        rolelib.load_spec(str(spec_file))


def test_load_spec_defaults_preload_skills_empty(tmp_path):
    spec_file = tmp_path / "min.yaml"
    spec_file.write_text("name: x\nidentity: hi\n")
    spec = rolelib.load_spec(str(spec_file))
    assert spec["preload_skills"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py -v`
Expected: FAIL with `AttributeError: module 'rolelib' has no attribute 'load_spec'`

- [ ] **Step 3: Write minimal implementation**

Append to `plugins/hscc-roles/rolelib.py`:
```python
import yaml

REQUIRED_FIELDS = ("name", "identity")


def load_spec(path):
    """Load + validate a role spec YAML. Returns a normalized dict.

    Required: name, identity. Optional: preload_skills (list, default []).
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/rolelib.py hscc-roles/tests/test_rolelib.py
git commit -m "feat(hscc-roles): role spec load + validation"
```

---

## Task 3: Base identity + starter role specs

**Files:**
- Create: `plugins/hscc-roles/base-identity.md`
- Create: `plugins/hscc-roles/roles/orchestrator.yaml`
- Create: `plugins/hscc-roles/roles/architect.yaml`
- Create: `plugins/hscc-roles/roles/coder.yaml`
- Create: `plugins/hscc-roles/roles/reviewer.yaml`
- Create: `plugins/hscc-roles/roles/qa.yaml`
- Test: `plugins/hscc-roles/tests/test_rolelib.py`

- [ ] **Step 1: Write the failing test**

Append to `plugins/hscc-roles/tests/test_rolelib.py`:
```python
def test_starter_specs_all_load():
    files = rolelib.list_spec_files()
    names = {rolelib.load_spec(f)["name"] for f in files}
    assert {"orchestrator", "architect", "coder", "reviewer", "qa"}.issubset(names)


def test_base_identity_exists_and_nonempty():
    assert os.path.exists(rolelib.BASE_IDENTITY_PATH)
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        assert len(f.read().strip()) > 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py::test_starter_specs_all_load -v`
Expected: FAIL (no spec files yet → empty name set)

- [ ] **Step 3: Write the files**

`plugins/hscc-roles/base-identity.md`:
```markdown
You are an agent in the Hermes fleet — a coordinated team of AI agents running
on a private DGX Spark GPU cluster. You share these values with every other
agent in the fleet:

- **Correctness over speed.** A right answer late beats a wrong answer now.
  You verify before you claim; you never fabricate output or pretend a blocked
  path succeeded.
- **Simple over clever.** Prefer the plainest solution that works. Three clear
  lines beat one cryptic one. You do not add abstraction, configuration, or
  features beyond what the task needs.
- **Honest signals.** You report real status. If something is broken, blocked,
  or uncertain, you say so plainly rather than papering over it.
- **Own your scope.** You do exactly the task you were given. If you discover
  adjacent work, you record it as a new task instead of scope-creeping.
- **Frequent, small commits.** You commit working increments with clear
  messages so others can follow and recover.
- **Leave a trail.** Your comments and task metadata let the next agent (or the
  human) pick up cold, with no hidden context.
```

`plugins/hscc-roles/roles/orchestrator.yaml`:
```yaml
name: orchestrator
identity: |
  You are Hermes, the operations orchestrator. You are the brain that turns
  ideas into structured work and routes it to the fleet — you do NOT do project
  work yourself. You think in dependency graphs: decompose ruthlessly, run
  independent work in parallel, gate dependent work behind its parents. You are
  terse and decisive. You observe live state before acting, and you alone hold
  authority over the physical cluster — guard it carefully.
preload_skills: [brainstorming, writing-plans]
```

`plugins/hscc-roles/roles/architect.yaml`:
```yaml
name: architect
identity: |
  You are a software architect. Given a spec, you design the approach and
  decompose it into a dependency-ordered set of small, self-contained tasks.
  You think in interfaces and boundaries: each unit has one responsibility and
  a clear contract. You apply YAGNI hard — no task exists that the spec does
  not require. You prefer many small parallel tasks over few large serial ones.
preload_skills: [writing-plans, brainstorming]
```

`plugins/hscc-roles/roles/coder.yaml`:
```yaml
name: coder
identity: |
  You are a focused implementation engineer. You own exactly one kanban task,
  executed in your own git worktree. You write the code and its tests, run the
  tests, and commit working increments. When your task produces a change that
  needs review (most code), you block it for review honestly rather than
  marking it done yourself. You do not drift into the next task — you record
  follow-up work as new tasks.
preload_skills: [test-driven-development, verification-before-completion]
```

`plugins/hscc-roles/roles/reviewer.yaml`:
```yaml
name: reviewer
identity: |
  You are a code reviewer. You are skeptical but fair. You read diffs
  adversarially, looking for correctness bugs, missed edge cases, and silent
  failures. You trust nothing until tests prove it: you run the task's tests
  and confirm they pass, and you check the work actually matches the task spec
  — well-written code that solved the wrong thing is a reject. You approve only
  when the diff is sound, tests are green, AND the spec is met. Otherwise you
  write precise, actionable change requests and send it back.
preload_skills: [verification-before-completion, test-driven-development]
```

`plugins/hscc-roles/roles/qa.yaml`:
```yaml
name: qa
identity: |
  You are a QA engineer. Your instinct is to break things. You write and run
  test suites that probe edge cases, boundary conditions, and failure paths,
  not just the happy path. You reproduce a problem before claiming it exists,
  and you confirm a fix actually fixes it. You report findings with exact
  reproduction steps so they can be acted on without guesswork.
preload_skills: [test-driven-development, systematic-debugging]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_rolelib.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/base-identity.md hscc-roles/roles/
git commit -m "feat(hscc-roles): base identity + 5 starter role specs"
```

---

## Task 4: SOUL composition

**Files:**
- Create: `plugins/hscc-roles/generator.py`
- Test: `plugins/hscc-roles/tests/test_generator.py`

- [ ] **Step 1: Write the failing test**

`plugins/hscc-roles/tests/test_generator.py`:
```python
import os
import rolelib
import generator


def test_compose_soul_has_all_three_layers():
    spec = {"name": "reviewer", "identity": "You review code.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE-CHAR-MARKER")
    # Layer 1 base
    assert "BASE-CHAR-MARKER" in soul
    # Layer 2 role
    assert "You review code." in soul
    # Layer 3 operational (thin: mentions role + worktree/kanban)
    assert "reviewer" in soul
    assert "worktree" in soul.lower() or "kanban" in soul.lower()


def test_compose_soul_orchestrator_skips_worker_ops():
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    # Orchestrator is not a kanban worker — must NOT claim to run in a worktree.
    assert "your own git worktree" not in soul.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'generator'`

- [ ] **Step 3: Write minimal implementation**

`plugins/hscc-roles/generator.py`:
```python
"""Generate Hermes profiles from role specs: SOUL composition + materialization."""
import os
import rolelib

_WORKER_OPS = (
    "## Operational\n\n"
    "You run as the **{name}** role on a worker GPU node of the cluster, "
    "executing a single kanban task in your own git worktree. The task "
    "lifecycle (claim, heartbeat, complete or block-for-review) is provided to "
    "you at runtime — follow it exactly.\n"
)

_ORCH_OPS = (
    "## Operational\n\n"
    "You run as the **{name}** on the gateway node. You route work through the "
    "native kanban board and hold sole authority over the physical cluster.\n"
)


def compose_soul(spec, base_identity):
    """Compose a profile SOUL from base + role disposition + thin operational.

    Orchestrator gets an operational block that does NOT describe worktree
    execution (it is not a worker); all other roles get the worker block.
    """
    name = spec["name"]
    ops = _ORCH_OPS if name == "orchestrator" else _WORKER_OPS
    return (
        f"{base_identity.rstrip()}\n\n"
        f"## Role: {name}\n\n"
        f"{spec['identity'].rstrip()}\n\n"
        f"{ops.format(name=name)}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/generator.py hscc-roles/tests/test_generator.py
git commit -m "feat(hscc-roles): layered SOUL composition (base+role+ops)"
```

---

## Task 5: Profile materialization (spec → profile dir)

**Files:**
- Modify: `plugins/hscc-roles/generator.py`
- Test: `plugins/hscc-roles/tests/test_generator.py`

**Note on approach:** the generator writes the profile dir directly (config.yaml + SOUL.md + profile.yaml) rather than shelling to `hermes profile create`, so it is testable in isolation against a temp HERMES_HOME with no Hermes runtime. The produced layout matches what `hermes profile create` yields for the fields Phase 1 needs (model block omitted on purpose — base_url is injected at spawn in a later phase; until then a role profile inherits the top-level config's model when run, which is acceptable for Phase 1 "profiles exist + load").

- [ ] **Step 1: Write the failing test**

Append to `plugins/hscc-roles/tests/test_generator.py`:
```python
import yaml


def test_generate_profile_writes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": ["test-driven-development"]}
    changed = generator.generate_profile(spec, base_identity="BASE")
    pdir = os.path.join(str(tmp_path / "profiles"), "coder")
    assert os.path.isdir(pdir)
    assert os.path.exists(os.path.join(pdir, "SOUL.md"))
    cfg = yaml.safe_load(open(os.path.join(pdir, "config.yaml")))
    assert "hscc-cluster" not in cfg["toolsets"]
    assert "hermes-cli" in cfg["toolsets"]
    prof = yaml.safe_load(open(os.path.join(pdir, "profile.yaml")))
    assert prof["description_auto"] is False
    assert changed is True  # first write reports changed


def test_generate_profile_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False  # unchanged content → no rewrite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py -v`
Expected: FAIL with `AttributeError: module 'generator' has no attribute 'generate_profile'`

- [ ] **Step 3: Write minimal implementation**

Append to `plugins/hscc-roles/generator.py`:
```python
import yaml


def _short_desc(spec):
    """First sentence of the role identity, for the kanban decomposer roster."""
    text = " ".join(spec["identity"].split())
    first = text.split(". ")[0].strip()
    return (first[:200] + ".") if first else f"The {spec['name']} role."


def _write_if_changed(path, content):
    """Write content only if it differs from what's on disk. Returns changed?"""
    if os.path.exists(path):
        with open(path) as f:
            if f.read() == content:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return True


def generate_profile(spec, base_identity):
    """Materialize a Hermes profile dir for a role spec. Idempotent.

    Writes SOUL.md (composed), config.yaml (toolsets = full minus cluster,
    preload skills), and profile.yaml (decomposer-facing description). Returns
    True if any file was written/changed this call, else False.
    """
    pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
    soul = compose_soul(spec, base_identity)
    config = {
        "toolsets": rolelib.role_toolsets(),
        "skills": {"preload": spec["preload_skills"]},
    }
    profile = {
        "description": _short_desc(spec),
        "description_auto": False,
    }
    changed = False
    changed |= _write_if_changed(os.path.join(pdir, "SOUL.md"), soul)
    changed |= _write_if_changed(
        os.path.join(pdir, "config.yaml"),
        yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
    )
    changed |= _write_if_changed(
        os.path.join(pdir, "profile.yaml"),
        yaml.safe_dump(profile, default_flow_style=False, sort_keys=False),
    )
    return changed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/generator.py hscc-roles/tests/test_generator.py
git commit -m "feat(hscc-roles): materialize profile dir from spec (idempotent)"
```

---

## Task 6: `create` — author a new role spec from a description

**Files:**
- Create: `plugins/hscc-roles/author.py`
- Test: `plugins/hscc-roles/tests/test_author.py`

**Note:** Phase 1 `create` writes a well-formed spec deterministically from the given name + description (description becomes the identity seed; preload_skills defaults to a sensible coding set). LLM-drafted dispositions are a later enhancement — Phase 1 keeps it dependency-free and testable. A human can edit the generated spec before `generate`.

- [ ] **Step 1: Write the failing test**

`plugins/hscc-roles/tests/test_author.py`:
```python
import os
import rolelib
import author


def test_create_writes_valid_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    path = author.create_role("financial-analyst",
                              "Analyzes budgets, models cash flow, reports risk.")
    assert os.path.exists(path)
    spec = rolelib.load_spec(path)  # must be loadable/valid
    assert spec["name"] == "financial-analyst"
    assert "cash flow" in spec["identity"]
    assert isinstance(spec["preload_skills"], list)


def test_create_rejects_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    import pytest
    with pytest.raises(ValueError, match="invalid role name"):
        author.create_role("Bad Name!", "desc")


def test_create_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    author.create_role("coder", "Builds code.")
    import pytest
    with pytest.raises(ValueError, match="already exists"):
        author.create_role("coder", "Builds code again.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_author.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'author'`

- [ ] **Step 3: Write minimal implementation**

`plugins/hscc-roles/author.py`:
```python
"""Author new role specs from a name + description (the description->spec stage)."""
import os
import re
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
    import yaml
    spec_yaml = yaml.safe_dump(
        {"name": name, "identity": identity + "\n", "preload_skills": skills},
        default_flow_style=False, sort_keys=False, allow_unicode=True,
    )
    with open(path, "w") as f:
        f.write(spec_yaml)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_author.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/author.py hscc-roles/tests/test_author.py
git commit -m "feat(hscc-roles): create command authors a new role spec"
```

---

## Task 7: CLI entry + plugin manifest

**Files:**
- Create: `plugins/hscc-roles/hscc.py`
- Create: `plugins/hscc-roles/plugin.yaml`
- Test: `plugins/hscc-roles/tests/test_generator.py`

- [ ] **Step 1: Write the failing test**

Append to `plugins/hscc-roles/tests/test_generator.py`:
```python
import subprocess
import sys


def test_cli_generate_all_runs(tmp_path):
    """End-to-end: `hscc.py generate` builds all 5 starter profiles into a temp home."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    plugin_dir = os.path.dirname(os.path.abspath(generator.__file__))
    venv_py = os.path.join(plugin_dir, "..", "..", "hermes-agent", "venv", "bin", "python")
    result = subprocess.run(
        [venv_py, os.path.join(plugin_dir, "hscc.py"), "generate"],
        capture_output=True, text=True, env=env, cwd=plugin_dir,
    )
    assert result.returncode == 0, result.stderr
    for role in ("orchestrator", "architect", "coder", "reviewer", "qa"):
        assert os.path.exists(os.path.join(str(tmp_path), "profiles", role, "SOUL.md"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py::test_cli_generate_all_runs -v`
Expected: FAIL (hscc.py does not exist → nonzero return / file-not-found)

- [ ] **Step 3: Write the implementation**

`plugins/hscc-roles/hscc.py`:
```python
#!/usr/bin/env python3
"""HSCC Roles — author + generate role-specialized Hermes profiles.

Usage: hscc-roles <command> [args]

Commands:
  generate                 Build/refresh all role profiles from roles/*.yaml
  create <name> <desc...>  Author a new role spec from a description
  list                     List role specs and whether their profile exists
  validate                 Validate every role spec (load + required fields)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rolelib
import generator
import author


def _base_identity():
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        return f.read()


def cmd_generate():
    base = _base_identity()
    out = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        changed = generator.generate_profile(spec, base)
        out.append({"role": spec["name"], "changed": changed})
    print({"generated": out})
    return 0


def cmd_create(argv):
    if len(argv) < 2:
        print("Usage: hscc-roles create <name> <description...>")
        return 1
    name = argv[0]
    desc = " ".join(argv[1:])
    path = author.create_role(name, desc)
    print({"created": name, "spec": path,
           "next": "run `hscc-roles generate` to build the profile"})
    return 0


def cmd_list():
    rows = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
        rows.append({"role": spec["name"],
                     "profile_exists": os.path.isdir(pdir)})
    print({"roles": rows})
    return 0


def cmd_validate():
    errs = []
    for spec_file in rolelib.list_spec_files():
        try:
            rolelib.load_spec(spec_file)
        except ValueError as e:
            errs.append(str(e))
    print({"ok": not errs, "errors": errs})
    return 1 if errs else 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if cmd == "generate":
        return cmd_generate()
    if cmd == "create":
        return cmd_create(sys.argv[2:])
    if cmd == "list":
        return cmd_list()
    if cmd == "validate":
        return cmd_validate()
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

`plugins/hscc-roles/plugin.yaml`:
```yaml
name: hscc-roles
version: 1.0.0
description: "Role framework — author + generate role-specialized Hermes profiles (architect, coder, reviewer, qa, ...) with layered SOULs. Capability uniform; cluster control excluded from all roles."
author: NousResearch
kind: backend
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (all generator tests incl. CLI e2e)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/hscc.py hscc-roles/plugin.yaml hscc-roles/tests/test_generator.py
git commit -m "feat(hscc-roles): CLI (generate/create/list/validate) + manifest"
```

---

## Task 8: Generate real profiles + verify they load

**Files:**
- (no source changes — this task runs the tool against the live `~/.hermes` and verifies)

- [ ] **Step 1: Run full test suite**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/ -v`
Expected: PASS (all tasks' tests green)

- [ ] **Step 2: Generate the real role profiles**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python hscc.py generate`
Expected: prints `{'generated': [{'role': 'architect', 'changed': True}, ... 5 roles]}`

- [ ] **Step 3: Verify profiles exist + SOUL has all layers**

Run:
```bash
for r in orchestrator architect coder reviewer qa; do
  echo "=== $r ==="
  grep -c "fleet" ~/.hermes/profiles/$r/SOUL.md   # base layer present
  grep -c "Role: $r" ~/.hermes/profiles/$r/SOUL.md # role layer present
  python3 -c "import yaml; c=yaml.safe_load(open('$HOME/.hermes/profiles/$r/config.yaml')); assert 'hscc-cluster' not in c['toolsets']; print('toolset ok')"
done
```
Expected: each role prints `1`, `1`, `toolset ok`.

- [ ] **Step 4: Verify a role profile loads in Hermes (no gateway start)**

Run: `cd ~/.hermes && hermes-agent/venv/bin/python -m hermes_cli.main profile show reviewer 2>&1 | head -5`
Expected: shows the reviewer profile details without error (confirms Hermes recognizes the generated profile).

- [ ] **Step 5: Commit the generated profiles + update hscc-skills bundling note**

```bash
cd ~/.hermes/plugins
# Generated profiles live under ~/.hermes/profiles (NOT this repo) — they are
# build artifacts. Only commit a note that they are generated.
git add hscc-roles/
git commit -m "chore(hscc-roles): phase 1 complete — 5 starter role profiles generated"
git push origin main
```

---

## Self-Review

**Spec coverage (Phase 1 portions of the design):**
- Role framework plugin (`hscc-roles`) → Tasks 1,7 ✓
- Role spec format (name/identity/preload_skills) → Task 2 ✓
- `create` (author new role) → Task 6 ✓
- `generate` (spec→profile) → Tasks 5,7 ✓
- Layered identity (base+role+ops) → Tasks 3,4 ✓
- base-identity.md shared character → Task 3 ✓
- 5 starter roles (orchestrator/architect/coder/reviewer/qa) → Task 3 ✓
- Capability uniform minus cluster → Task 1 (`role_toolsets`), enforced in Task 5 ✓
- Idempotent generation (hash/diff) → Task 5 ✓
- Profiles load in Hermes → Task 8 ✓

Deferred to later phases (correctly NOT in this plan): spawn base_url injection, pipeline, reviewer loop, autonomy governor, auto-role-creation during decompose. ✓

**Placeholder scan:** none — every step has real code/commands. ✓

**Type consistency:** `load_spec` returns `{name, identity, preload_skills}` used identically in generator (`spec["preload_skills"]`, `spec["identity"]`, `spec["name"]`) and author writes the same three keys. `role_toolsets()` / `generate_profile()` / `compose_soul()` / `create_role()` signatures consistent across tasks and the CLI. `rolelib.PROFILES_DIR` / `ROLES_DIR` patched consistently in tests. ✓

**Note on the model block:** Phase 1 profiles intentionally omit a `model` block (no base_url). When run before the later spawn-injection phase, a role profile inherits the top-level `config.yaml` model (.244) — acceptable for "profiles exist + load." The base_url-at-spawn core change is a documented later phase.
