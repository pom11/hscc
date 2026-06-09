# Autonomy Governor (Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the fleet a master autonomy switch + a "do it autonomously" phrase trigger so the orchestrator runs the full idea→spec→tasks pipeline hands-off, plus tell the orchestrator to auto-create a missing role when decomposing. All prompt-level + a tiny flag CLI — NO Hermes-core surgery.

**Architecture:** Three pieces, all low-risk: (1) a small `autonomy` module + CLI command in the `hscc-roles` plugin managing a `~/.hscc/autonomy` flag file (reviving the archived coordinator's exact pattern); (2) SOUL.md guidance so the orchestrator reads/sets the flag, treats "do it autonomously" (and similar) as the trigger to flip it on + skip the interactive brainstorm, and pauses for approval when the flag is off; (3) SOUL.md guidance to `hscc-roles create` a missing role during decomposition then assign to it. The behavior lives in the orchestrator's prompt (SOUL.md is read every turn — live, no restart needed for prompt changes beyond a gateway reload), and the flag is a file both the human and the orchestrator can flip via the CLI.

**Tech Stack:** Python 3 stdlib (hscc-roles plugin), SOUL.md (`~/.hermes/SOUL.md` — the orchestrator identity), pytest.

**Branch:** All changes in `pom11/hscc` (plugin CLI + the SOUL.md edit is to `~/.hermes/SOUL.md` which is NOT in a repo — back it up). NO hermes-agent core changes this phase (per user decision: skip spawn base_url injection).

**Scope:** Phase 4 as scoped by the user — autonomy switch + phrase trigger + auto-role-creation. EXPLICITLY OUT: spawn base_url injection (roles stay on the shared .244 model — zero payoff while all nodes serve one model), review→running auto-retry, n8n.

**Safety model:** "Relentless, not infinite." The autonomy flag only governs whether the orchestrator pauses for human approval at the spec gate. The actual execution safety ceilings already exist: the dispatcher's spawn-failure breaker (`failure_limit`), `max_in_progress`/`per_profile` caps, and the reviewer gate (Phase 3). Autonomy does NOT bypass the reviewer or the integration-branch-only merge — main stays human-gated regardless.

---

## File Structure

- `plugins/hscc-roles/autonomy.py` — flag read/write (`is_on()`, `set_state()`, `AUTONOMY_FILE`). One responsibility: the flag.
- `plugins/hscc-roles/hscc.py` — add an `autonomy` CLI subcommand (show / on / off).
- `plugins/hscc-roles/tests/test_autonomy.py` — flag round-trip + default-off tests.
- `~/.hermes/SOUL.md` — orchestrator guidance: phrase trigger, autonomy-gated approval, auto-role-creation. (Not in a repo; back up before editing.)

---

## Task 1: The autonomy flag module

**Files:**
- Create: `plugins/hscc-roles/autonomy.py`
- Test: `plugins/hscc-roles/tests/test_autonomy.py`

- [ ] **Step 1: Write the failing test**

`plugins/hscc-roles/tests/test_autonomy.py`:
```python
import os
import autonomy


def test_default_off(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    assert autonomy.is_on() is False          # absent file = off


def test_set_on_then_off(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    autonomy.set_state("on")
    assert autonomy.is_on() is True
    autonomy.set_state("off")
    assert autonomy.is_on() is False


def test_truthy_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    for v in ("on", "1", "true", "yes", "ON", "True"):
        autonomy.set_state(v)
        assert autonomy.is_on() is True
    for v in ("off", "0", "false", "no", ""):
        autonomy.set_state(v)
        assert autonomy.is_on() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_autonomy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy'`

- [ ] **Step 3: Write minimal implementation**

`plugins/hscc-roles/autonomy.py`:
```python
"""Master autonomy flag for the fleet.

A single file ``~/.hscc/autonomy`` holds ``on``/``off``. When on, the
orchestrator runs the idea->spec->tasks pipeline hands-off (no human approval
gate at the spec step). Both the human and the orchestrator flip it via the
`hscc-roles autonomy` CLI. Default (absent file) is OFF — the conservative,
ask-first posture. Reviver of the archived coordinator's pattern.
"""
import os

HSCC_DIR = os.path.expanduser("~/.hscc")
AUTONOMY_FILE = os.path.join(HSCC_DIR, "autonomy")
_TRUE = ("on", "1", "true", "yes")


def is_on():
    """True iff the autonomy flag file exists and holds a truthy value."""
    try:
        with open(AUTONOMY_FILE) as f:
            return f.read().strip().lower() in _TRUE
    except (FileNotFoundError, OSError):
        return False


def set_state(value):
    """Write the flag atomically. Any non-truthy string disables autonomy."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    tmp = AUTONOMY_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(value).strip() + "\n")
    os.replace(tmp, AUTONOMY_FILE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_autonomy.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/autonomy.py hscc-roles/tests/test_autonomy.py
git commit -m "feat(hscc-roles): autonomy flag module (~/.hscc/autonomy)"
```

---

## Task 2: The `autonomy` CLI subcommand

**Files:**
- Modify: `plugins/hscc-roles/hscc.py` (add `autonomy` command to the dispatch + docstring)
- Test: `plugins/hscc-roles/tests/test_autonomy.py`

- [ ] **Step 1: Write the failing test**

Append to `plugins/hscc-roles/tests/test_autonomy.py`:
```python
import subprocess
import sys


def test_cli_autonomy_show_on_off(tmp_path):
    plugin_dir = os.path.dirname(os.path.abspath(autonomy.__file__))
    venv_py = os.path.join(plugin_dir, "..", "..", "hermes-agent", "venv", "bin", "python")
    hscc = os.path.join(plugin_dir, "hscc.py")
    env = dict(os.environ, HOME=str(tmp_path))  # isolate ~/.hscc to tmp
    # default off
    r = subprocess.run([venv_py, hscc, "autonomy"], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "off" in r.stdout.lower()
    # turn on
    r = subprocess.run([venv_py, hscc, "autonomy", "on"], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    r = subprocess.run([venv_py, hscc, "autonomy"], capture_output=True, text=True, env=env)
    assert "on" in r.stdout.lower()
```
NOTE: this relies on `autonomy.HSCC_DIR`/`AUTONOMY_FILE` deriving from `~` at import via `os.path.expanduser`, and HOME being overridden in the subprocess env. Confirm `expanduser` honors the overridden HOME (it does on macOS/Linux). If the module caches the path at import in a way HOME can't override, adjust the test to set an `HSCC_HOME`-style env — but prefer the HOME override to keep autonomy.py dependency-free.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_autonomy.py::test_cli_autonomy_show_on_off -v`
Expected: FAIL (hscc.py has no `autonomy` command → nonzero or wrong output)

- [ ] **Step 3: Add the command to hscc.py**

In `plugins/hscc-roles/hscc.py`: import the module (`import autonomy` alongside the existing `import rolelib` etc.), add a `cmd_autonomy(argv)` function, wire it into `main()`'s dispatch, and add it to the docstring.
```python
def cmd_autonomy(argv):
    if argv:
        autonomy.set_state(argv[0])
    print({"autonomy": "on" if autonomy.is_on() else "off"})
    return 0
```
In `main()` add: `if cmd == "autonomy": return cmd_autonomy(sys.argv[2:])`.
In the module docstring Commands list add:
```
  autonomy [on|off]        Show or set the fleet autonomy flag
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/test_autonomy.py -v`
Expected: PASS (4 passed). Also run the full plugin suite: `../../hermes-agent/venv/bin/python -m pytest tests/ -q` → expect all green (18 from P1 + 4 autonomy = 22).

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/plugins
git add hscc-roles/hscc.py hscc-roles/tests/test_autonomy.py
git commit -m "feat(hscc-roles): autonomy CLI subcommand (show/on/off)"
git push origin main
```

---

## Task 3: Orchestrator SOUL guidance — phrase trigger, autonomy gate, auto-role-creation

**Files:**
- Modify: `~/.hermes/SOUL.md` (the orchestrator identity — NOT in a repo; back up first)

**Context:** SOUL.md is the orchestrator's system prompt (loaded every turn via `load_soul_md`). This is where the autonomy BEHAVIOR lives — no core code decides it; the orchestrator does, reading the flag. The flag CLI is `python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy [on|off]` (the orchestrator can call it via its terminal/quick-command; the human via the same).

- [ ] **Step 1: Back up SOUL.md**

```bash
cp ~/.hermes/SOUL.md ~/.hermes/SOUL.md.bak-$(date +%Y%m%d-%H%M%S)
```

- [ ] **Step 2: Read the current SOUL.md to find the insertion point**

Run: `cat ~/.hermes/SOUL.md`
Identify the "You orchestrate" section (added in the refactor). The new autonomy guidance goes as a new `## Autonomy` section after it, before `## Safety`.

- [ ] **Step 3: Add the Autonomy section to ~/.hermes/SOUL.md**

Insert this section (verbatim) before the `## Safety` section:
```markdown
## Autonomy

There is a master autonomy flag at `~/.hscc/autonomy` (on/off, default off).
Read or set it with `python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy [on|off]`.

- **When the user says "do it autonomously"** (or "run it autonomously", "go
  autonomous", "don't wait for me", or clearly delegates end-to-end): set the
  flag on (`autonomy on`), then run the full pipeline hands-off — write a
  best-judgment spec WITHOUT the interactive back-and-forth, decompose it into
  kanban tasks, and let the fleet execute. Tell the user you've gone autonomous
  and will report at milestones/blockers.
- **When autonomy is ON:** do not pause for approval at the spec step. Create
  the kanban work and let it run. You still surface blockers and escalations.
- **When autonomy is OFF (default):** brainstorm interactively, present the
  spec/plan, and WAIT for the user's go before creating the kanban work.
- You may flip the flag yourself when the user delegates, and flip it off when
  they ask to take back control ("stop", "let me review", "pause autonomy").

Autonomy changes only whether you pause for approval. It NEVER bypasses the
reviewer gate or merges to main — reviewed work lands on the integration branch;
promoting integration→main is always the human's call.

## Auto-create roles on demand

When decomposing work, route each task to the best-fit role profile. If no
existing role fits a task, CREATE one:
`python3 ~/.hermes/plugins/hscc-roles/hscc.py create <name> "<what this role does>"`
then `... hscc-roles ... generate` to build its profile, then assign the task to
it. Prefer reusing an existing role if one reasonably fits — only mint a new
role when nothing does (avoid near-duplicates like coder/code-writer). When you
auto-create a role, note it so the human can review the new role's spec later.
```

- [ ] **Step 4: Verify SOUL.md still loads + contains the guidance**

Run:
```bash
cd ~/.hermes/hermes-agent && venv/bin/python -c "
from agent.prompt_builder import load_soul_md
soul = load_soul_md()
assert 'Autonomy' in soul and 'do it autonomously' in soul
assert 'Auto-create roles' in soul or 'auto-create' in soul.lower()
assert 'integration branch' in soul.lower() or 'integration' in soul.lower()
print('SOUL.md autonomy + auto-role guidance present')
"
```
Expected: prints the confirmation. (If `load_soul_md` needs HERMES_HOME, it defaults to ~/.hermes — fine.)

- [ ] **Step 5: Note the change (SOUL.md is not in a repo)**

No git commit (SOUL.md lives in ~/.hermes). The backup from Step 1 is the rollback. Record in session/memory that the orchestrator SOUL now carries autonomy + auto-role guidance, effective on next gateway start.

---

## Task 4: End-to-end verification

**Files:** none (verification).

- [ ] **Step 1: Flag round-trips via the real CLI**

```bash
cd ~/.hermes/plugins/hscc-roles
../../hermes-agent/venv/bin/python hscc.py autonomy          # -> {"autonomy": "off"} (or current)
../../hermes-agent/venv/bin/python hscc.py autonomy on       # -> {"autonomy": "on"}
../../hermes-agent/venv/bin/python hscc.py autonomy          # -> {"autonomy": "on"}
../../hermes-agent/venv/bin/python hscc.py autonomy off      # -> {"autonomy": "off"}
```
Confirm `~/.hscc/autonomy` reflects the last state: `cat ~/.hscc/autonomy`. Leave it OFF at the end (conservative default).

- [ ] **Step 2: The orchestrator can read the flag (quick-command path)**

Confirm the orchestrator has a terminal/exec path to the CLI. It already runs `python3 ~/.hermes/plugins/...` style commands. Verify the command is callable as the orchestrator would: `python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy` returns valid JSON. (No gateway start needed.)

- [ ] **Step 3: SOUL guidance present (from Task 3 Step 4)** — already verified.

- [ ] **Step 4: Full plugin suite green**

Run: `cd ~/.hermes/plugins/hscc-roles && ../../hermes-agent/venv/bin/python -m pytest tests/ -q`
Expected: all green (P1 18 + autonomy 4 = 22).

- [ ] **Step 5: Document the manual e2e for the user**

After the next gateway start, the user can verify hands-off operation:
1. In chat: "build <small thing> autonomously" → orchestrator flips `autonomy on`, writes a spec without back-and-forth, creates kanban tasks.
2. Coders build → submit to review → sdlc-review merges to integration.
3. User: "pause autonomy" → orchestrator flips `autonomy off`.
4. `cat ~/.hscc/autonomy` reflects state throughout.

---

## Self-Review

**Spec coverage (Phase 4 as scoped):**
- Master autonomy switch (`~/.hscc/autonomy`) → Task 1 module + Task 2 CLI ✓
- "Do it autonomously" phrase trigger → Task 3 SOUL guidance ✓
- Autonomy-gated approval (off=ask, on=hands-off) → Task 3 ✓
- Safety: never bypass reviewer / never merge to main → Task 3 explicit + Phase 3 mechanics ✓
- Auto-create roles on demand → Task 3 SOUL guidance + Phase 1 `create`/`generate` ✓
- Human + orchestrator can both flip → Task 2 CLI (callable by both) ✓

Explicitly OUT (per user): spawn base_url injection, review→running auto-retry. NOT in this plan.

**Placeholder scan:** none — flag module + CLI + SOUL text are concrete.

**Type/consistency:** `autonomy.is_on()`/`set_state()`/`AUTONOMY_FILE` used consistently in tests, CLI, and referenced by SOUL via the CLI command. CLI command name `autonomy` matches across hscc.py dispatch + docstring + SOUL references + tests. The `hscc-roles create`/`generate` commands referenced in SOUL exist (Phase 1).

**Risk:** Lowest of all phases — one new stdlib module + one CLI command + a prompt edit. No hermes-core changes. The flag defaults OFF (conservative), and even ON cannot bypass the reviewer or merge to main (enforced by Phase 3 + the SOUL safety clause). SOUL.md change is backed up; flag is a file. Nothing activates until the next gateway start.
