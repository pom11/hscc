# Pipeline (Phase 2) — Auto-Paired Review Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the kanban decomposer automatically pair a `review:` task (assigned to the `reviewer` role) onto every code-producing child task, so review is structural — every implementation task gets a reviewer gate without the decomposer LLM having to remember.

**Architecture:** A small, self-contained transform inserted into `kanban_decompose.decompose_task` after the children list is built (line ~439) and before it's passed to `kb.decompose_triage_task`. The transform reads a config policy (`kanban.auto_review`) listing which assignee roles produce work that needs review; for each matching child it appends a review child whose `parents=[<impl child index>]`. Pure list manipulation on the existing `{title, body, assignee, parents}` child dicts — no change to `decompose_triage_task` or the DB layer. Indices stay valid because review children are appended after all impl children, and their parent index points back at the impl child.

**Tech Stack:** Python 3 (Hermes core, `hermes-agent/hermes_cli/kanban_decompose.py`), pytest. Config in `~/.hermes/config.yaml` under `kanban.auto_review`.

**Branch:** `local-custom` in the `hermes-agent` repo (the user's local-core-mods branch — same place heartbeat/TTS/kanban-assignee fixes live). NEVER pushed to NousResearch upstream. Confirm you are on `local-custom` before committing (`git -C /Users/desac/.hermes/hermes-agent branch --show-current`).

**Scope:** Phase 2 ONLY — auto-pair review tasks during decomposition. The reviewer LOOP (reviewer actually reading diffs, approving→integration-merge, reject→retry) is Phase 3. The autonomy governor + phrase trigger is Phase 4. This plan just guarantees a reviewer task EXISTS, gated behind each impl task. The orchestrator already runs brainstorm→decompose inline (existing capability); we are sharpening the decompose terminal step.

**Out of scope (later phases):** reviewer execution/verdict logic, integration-branch merge, tiered retry, autonomy switch, spawn base_url injection, role preload-skills runtime wiring.

---

## File Structure

- `hermes-agent/hermes_cli/kanban_decompose.py` — add `_review_policy()` (reads config) + `_pair_review_tasks(children, policy)` (the transform) + one call site in `decompose_task`. The pairing logic is its own pure function so it is unit-testable without the DB or LLM.
- `hermes-agent/tests/test_kanban_review_pairing.py` — new test module for the pure transform + a config-driven integration check.
- `~/.hermes/config.yaml` — add the `kanban.auto_review` policy block (config edit, not in the hermes-agent repo).

Keeping `_pair_review_tasks` a pure function (list in → list out) is the key boundary: it has no DB, no LLM, no IO, so it's fully testable and the risky `decompose_task` integration is a one-line call.

---

## Task 1: The review-pairing pure transform

**Files:**
- Modify: `hermes-agent/hermes_cli/kanban_decompose.py` (add two functions near the other `_helpers`, around line 268 after `_normalize_assignee_choice`)
- Test: `hermes-agent/tests/test_kanban_review_pairing.py`

- [ ] **Step 1: Write the failing test**

`hermes-agent/tests/test_kanban_review_pairing.py`:
```python
from hermes_cli import kanban_decompose as kd


def test_pair_adds_review_for_code_role():
    children = [
        {"title": "Build API", "body": "spec", "assignee": "backend-engineer", "parents": []},
    ]
    out = kd._pair_review_tasks(children, policy={"review_roles": ["backend-engineer"],
                                                 "reviewer": "reviewer"})
    assert len(out) == 2
    impl, review = out[0], out[1]
    assert impl["assignee"] == "backend-engineer"
    assert review["assignee"] == "reviewer"
    assert review["parents"] == [0]            # review gated behind impl index 0
    assert "review" in review["title"].lower()


def test_pair_skips_non_code_role():
    children = [
        {"title": "Write spec", "body": "", "assignee": "product-manager", "parents": []},
    ]
    out = kd._pair_review_tasks(children, policy={"review_roles": ["backend-engineer"],
                                                 "reviewer": "reviewer"})
    assert len(out) == 1                        # PM task gets no review


def test_pair_never_reviews_a_reviewer_task():
    children = [
        {"title": "Review X", "body": "", "assignee": "reviewer", "parents": []},
    ]
    out = kd._pair_review_tasks(children, policy={"review_roles": ["reviewer"],
                                                 "reviewer": "reviewer"})
    assert len(out) == 1                        # never pair a review onto a review


def test_pair_preserves_impl_indices_for_existing_parents():
    # Two impl tasks; second depends on first. Review children appended AFTER
    # all impls, so the existing parent index (1 depends on 0) stays valid.
    children = [
        {"title": "A", "body": "", "assignee": "coder", "parents": []},
        {"title": "B", "body": "", "assignee": "coder", "parents": [0]},
    ]
    out = kd._pair_review_tasks(children, policy={"review_roles": ["coder"],
                                                 "reviewer": "reviewer"})
    assert len(out) == 4
    assert out[1]["parents"] == [0]            # B still depends on A
    # review of A parents=[0], review of B parents=[1]
    reviews = [c for c in out if c["assignee"] == "reviewer"]
    assert {tuple(r["parents"]) for r in reviews} == {(0,), (1,)}


def test_pair_empty_policy_noop():
    children = [{"title": "X", "body": "", "assignee": "coder", "parents": []}]
    assert kd._pair_review_tasks(children, policy={}) == children
    assert kd._pair_review_tasks(children, policy=None) == children
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: FAIL with `AttributeError: module 'hermes_cli.kanban_decompose' has no attribute '_pair_review_tasks'`

- [ ] **Step 3: Write minimal implementation**

Add to `hermes-agent/hermes_cli/kanban_decompose.py` after `_normalize_assignee_choice` (around line 268):
```python
def _pair_review_tasks(children, policy):
    """Append a reviewer task for each impl child whose role is in review_roles.

    Pure transform: takes the built children list (each a dict with title,
    body, assignee, parents=indices) and returns a NEW list with review
    children appended. Each review child is gated behind its impl child via
    ``parents=[impl_index]``. Review children are appended AFTER all impl
    children so every pre-existing parent index stays valid.

    ``policy`` is ``kanban.auto_review`` config:
      {"review_roles": [<assignee names that produce reviewable work>],
       "reviewer": "<reviewer profile name>"}
    Empty/None policy or missing reviewer → returns ``children`` unchanged.
    A child already assigned to the reviewer is never paired (no review of a
    review).
    """
    if not policy:
        return children
    review_roles = set(policy.get("review_roles") or [])
    reviewer = (policy.get("reviewer") or "").strip()
    if not review_roles or not reviewer:
        return children
    out = list(children)
    for idx, child in enumerate(children):
        assignee = child.get("assignee")
        if assignee == reviewer:
            continue
        if assignee not in review_roles:
            continue
        out.append({
            "title": f"review: {child.get('title', '')}".strip()[:200],
            "body": (
                "Review the work produced by the parent task. Read the diff for "
                "correctness, run its tests and confirm they pass, and verify the "
                "work matches the task spec. Approve, or send back with precise "
                "change requests."
            ),
            "assignee": reviewer,
            "parents": [idx],
        })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/hermes-agent
git branch --show-current   # MUST print: local-custom
git add hermes_cli/kanban_decompose.py tests/test_kanban_review_pairing.py
git commit -m "feat(kanban): pure review-pairing transform for decompose"
```

---

## Task 2: Config policy reader

**Files:**
- Modify: `hermes-agent/hermes_cli/kanban_decompose.py` (add `_review_policy(cfg)` near `_resolve_default_assignee`, ~line 215)
- Test: `hermes-agent/tests/test_kanban_review_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `hermes-agent/tests/test_kanban_review_pairing.py`:
```python
def test_review_policy_reads_config():
    cfg = {"kanban": {"auto_review": {"review_roles": ["coder", "backend-engineer"],
                                      "reviewer": "reviewer"}}}
    pol = kd._review_policy(cfg)
    assert pol["reviewer"] == "reviewer"
    assert "coder" in pol["review_roles"]


def test_review_policy_absent_returns_empty():
    assert kd._review_policy({}) == {}
    assert kd._review_policy({"kanban": {}}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: FAIL with `AttributeError: ... has no attribute '_review_policy'`

- [ ] **Step 3: Write minimal implementation**

Add to `hermes-agent/hermes_cli/kanban_decompose.py` (near `_resolve_default_assignee`, ~line 215):
```python
def _review_policy(cfg):
    """Return the kanban.auto_review policy dict, or {} when not configured.

    Shape: {"review_roles": [...], "reviewer": "<profile>"}. Missing or
    malformed config returns {} so callers treat review-pairing as disabled.
    """
    kanban_cfg = (cfg or {}).get("kanban", {}) if isinstance(cfg, dict) else {}
    policy = kanban_cfg.get("auto_review")
    if not isinstance(policy, dict):
        return {}
    roles = policy.get("review_roles")
    reviewer = policy.get("reviewer")
    if not isinstance(roles, list) or not isinstance(reviewer, str):
        return {}
    return {"review_roles": [str(r).strip() for r in roles if str(r).strip()],
            "reviewer": reviewer.strip()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/hermes-agent
git add hermes_cli/kanban_decompose.py tests/test_kanban_review_pairing.py
git commit -m "feat(kanban): auto_review config policy reader"
```

---

## Task 3: Wire the transform into decompose_task

**Files:**
- Modify: `hermes-agent/hermes_cli/kanban_decompose.py` (call site inside `decompose_task`, after the children list is built ~line 439, before the `kb.decompose_triage_task` call ~line 441)
- Test: `hermes-agent/tests/test_kanban_review_pairing.py`

- [ ] **Step 1: Write the failing test**

The real model call inside `decompose_task` is `client.chat.completions.create` (an auxiliary LLM client), and the function needs a real triage DB row — driving the whole function in a unit test is heavy and brittle. Instead, this task's correctness is verified two ways: (a) a **seam test** asserting the two helpers compose over real config (below), and (b) the **manual integration check** in Step 4 (real config + the live decompose code path). Tasks 1-2 already exhaustively unit-test the transform + policy, so the remaining risk is purely the one-line wiring, which Step 4 exercises directly.

Append to `hermes-agent/tests/test_kanban_review_pairing.py`:
```python
def test_policy_and_transform_compose():
    """Contract: a configured policy + the transform yield a reviewer task.

    This is the composition the Task-3 wiring performs inside decompose_task
    (children = _pair_review_tasks(children, _review_policy(cfg))).
    """
    from hermes_cli import kanban_decompose as kd
    cfg = {"kanban": {"auto_review": {"review_roles": ["backend-engineer"],
                                      "reviewer": "reviewer"}}}
    children = [{"title": "Build API", "body": "spec",
                 "assignee": "backend-engineer", "parents": []}]
    out = kd._pair_review_tasks(children, kd._review_policy(cfg))
    assert len(out) == 2
    assert out[1]["assignee"] == "reviewer"
    assert out[1]["parents"] == [0]
```

- [ ] **Step 2: Run test to verify it passes (seam test — green once Tasks 1-2 exist)**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py::test_policy_and_transform_compose -v`
Expected: PASS (the helpers from Tasks 1-2 compose correctly). The actual `decompose_task` wiring is verified by Step 4's integration check.

- [ ] **Step 3: Write the wiring (the actual Phase-2 change)**

In `hermes-agent/hermes_cli/kanban_decompose.py`, inside `decompose_task`, find where `children` is fully built (right after the `for idx, entry in enumerate(raw_tasks)` loop that appends to `children`, immediately before `try:` / `with kb.connect_closing()`), insert:
```python
    # Auto-pair a reviewer task onto each code-producing child (Phase 2).
    # Policy-driven: kanban.auto_review.{review_roles, reviewer}. No-op when
    # unconfigured. Review children are appended after all impl children so the
    # impl parent indices above stay valid; each review is gated behind its impl.
    children = _pair_review_tasks(children, _review_policy(cfg))
```
NOTE: `cfg` must be in scope at that point. `decompose_task` already loads config (it resolves orchestrator + default_assignee). If the local variable holding the parsed config dict is named differently (e.g. `config`), use that name. If config isn't already loaded in that function, add `cfg = _load_config()` once near the top of `decompose_task` and reuse it for the existing resolver calls too — but PREFER reusing the existing load; do not double-load.

- [ ] **Step 4: Run tests + a real integration check**

Run unit tests: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: PASS.

Run the FULL decompose test suite to ensure no regression:
`cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/ -k "decompose or kanban" -q`
Expected: all green (no existing decompose test broken by the new call).

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/hermes-agent
git add hermes_cli/kanban_decompose.py tests/test_kanban_review_pairing.py
git commit -m "feat(kanban): wire review-pairing into decompose_task (policy-gated)"
```

---

## Task 4: Configure the policy + end-to-end verification

**Files:**
- Modify: `~/.hermes/config.yaml` (add `kanban.auto_review`) — NOT in the hermes-agent repo; this is live config.

- [ ] **Step 1: Back up config + add the policy**

```bash
cd /Users/desac/.hermes
cp config.yaml config.yaml.bak-$(date +%Y%m%d-%H%M%S)
```
Add under the existing `kanban:` block in `~/.hermes/config.yaml`:
```yaml
  auto_review:
    reviewer: reviewer
    review_roles:
      - coder
      - backend-engineer
      - frontend-engineer
      - devops-engineer
      - data-engineer
      - ml-engineer
```
(These are the roles whose output is code/infra that warrants a reviewer gate. Non-code roles — PM, writer, analyst, designer, researcher — are intentionally excluded; their output isn't diff+test reviewable the same way. Tune later as the roster grows.)

- [ ] **Step 2: Validate config loads**

Run: `cd /Users/desac/.hermes && hermes-agent/venv/bin/python -c "import yaml; k=yaml.safe_load(open('config.yaml'))['kanban']['auto_review']; print('reviewer:', k['reviewer']); print('roles:', k['review_roles'])"`
Expected: prints reviewer + the 6 roles.

- [ ] **Step 3: End-to-end dry check of the transform against real config**

Run:
```bash
cd /Users/desac/.hermes/hermes-agent && venv/bin/python -c "
from hermes_cli import kanban_decompose as kd
from hermes_cli.config import load_config
pol = kd._review_policy(load_config())
print('policy:', pol)
demo = [
  {'title': 'Build the API', 'body': '', 'assignee': 'backend-engineer', 'parents': []},
  {'title': 'Write the spec', 'body': '', 'assignee': 'product-manager', 'parents': []},
]
out = kd._pair_review_tasks(demo, pol)
for c in out:
    print(' ', c['assignee'], '|', c['title'], '| parents', c['parents'])
"
```
Expected: backend-engineer task gets a paired `review:` task (parents=[0], assignee=reviewer); the product-manager task does NOT.

- [ ] **Step 4: Commit note (config is not in a repo)**

Config lives in `~/.hermes` (not version-controlled). No commit needed; the backup from Step 1 is the rollback. Record in the session/memory that `kanban.auto_review` is now set.

NOTE: This change takes effect on the next gateway start (the dispatcher/decomposer reads config at run time). Per the user's standing instruction, DO NOT start the gateway as part of this plan — the config + code are staged and will activate when the user next starts Hermes.

---

## Self-Review

**Spec coverage (Phase 2 portion of the design):**
- Auto-paired review task per code task → Task 1 (`_pair_review_tasks`) ✓
- Review gated behind impl (dependency) → Task 1 (`parents=[idx]`) ✓
- Policy-driven which roles need review → Task 2 (`_review_policy`) + Task 4 (config) ✓
- Never review a review → Task 1 (`assignee == reviewer` skip) ✓
- Wired into the inline decompose pipeline → Task 3 ✓
- Reviewer role exists → already shipped in Phase 1 ✓

Deferred correctly (NOT in this plan): reviewer execution/verdict, integration-branch merge, tiered retry, autonomy governor, spawn injection.

**Placeholder scan:** none — all steps have real code. Task 3's test note is an explicit, justified seam-vs-e2e decision, not a placeholder.

**Type consistency:** `_review_policy` returns `{"review_roles": [...], "reviewer": str}` or `{}`; `_pair_review_tasks(children, policy)` consumes exactly that shape and the existing child dict shape `{title, body, assignee, parents}`. The call site passes `cfg` (the already-loaded config dict in `decompose_task`). Review children use `parents=[idx]` (indices), matching `decompose_triage_task`'s documented contract (parents = indices into the children list). Append-after-all-impls keeps existing indices valid. ✓

**Risk note:** This is a Hermes-core change on `local-custom`. It is additive and policy-gated (no-op when `kanban.auto_review` is absent), so a config rollback fully disables it without reverting code. The transform is a pure function with thorough unit tests; the only integration surface is one line in `decompose_task`.
