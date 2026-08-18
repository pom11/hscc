# Per-project Hermes profiles ("bots") for the linked EcoFire family — worth it, or is project-linking alone enough?

**Design exploration — task t_1764e5fb**
**Date:** 2026-08-18
**Assignee:** architect

This is an honest evaluation, not a feature pitch. The question: should the
EcoFire family (ecofire-app, efsdriver, ecofire-bc) get its own dedicated
Hermes profiles — "its own bot, with its memory, session, profile" — instead
of dispatching to generic role profiles reused across every project?

The conclusion up front: **per-project profile *memory* is the wrong mechanism
for the friction it targets, and adds real cost for little likely benefit.
The dependency-link work in the flightdeck registry (the sibling card) is the
actual fix for the stated friction.** There is a narrow, cheap, worth-a-pilot
version of the idea (ONE shared *family* profile), but only on the condition
that its success is measured by whether memory actually gets populated — which
the existing evidence suggests it will not under current dispatch habits.

---

## TL;DR recommendation

| Question | Verdict |
|----------|---------|
| Per-repo profiles (ecofire-app-dev, efsdriver-dev, ecofire-bc-dev, ...)? | **No.** Wrong granularity — per-repo memory does NOT carry cross-repo context, which is precisely the friction being solved. 3–4× the cost, near-zero benefit against the stated goal. |
| Per-project profile memory accumulates useful continuity? | **Not realized in practice.** The existing `ecofire-bc-engineer` profile proves it: exists, has 6 sessions, **zero memories**. Workers don't reliably write memory. |
| Project-linking in the registry (sibling `depends_on` card)? | **Yes — this is the real fix.** It makes cross-repo *discoverability* deterministic, and the worker then reads the actual sibling repos. Strictly more reliable than probabilistic memory. |
| One SHARED EcoFire-family profile as a pilot? | **Optional, cheap, worth one pilot** — the only version of the idea that targets the actual friction. Success criterion = "does memory get populated over N cards?" If not (as `ecofire-bc-engineer` shows), drop it. |

---

## 0. Context already established (read, don't re-derive)

- **Yesterday's bot-mode investigation** (`docs/INVESTIGATION-bot-mode-relevance.md`,
  task t_ba307d02, committed on branch `wt/t_ba307d02`; **not merged to main**)
  concluded the Bot Mode **UI plugin does not apply** (headless fleet, no desktop
  app). That conclusion stands; this card is unrelated to the UI plugin. It also
  found that **profile creation is mostly solved** by HSCC's own `hscc-roles`
  role framework — `create`/`generate`/`list`/`validate`, 22+ shipped role
  profiles, building via the native profile API. That finding is confirmed by
  inspection here (see §2).
- **The underlying mechanism** (confirmed): a bot IS a Hermes profile —
  isolated config, memory, skills, credentials, chat history under
  `~/.hermes/profiles/<name>/`. Scriptable via plain `hermes -p <profile>`.
- **The motivating friction (today):** EcoFire-family work repeatedly needs
  cross-project context — tracing whether a BC API page change affects
  ecofire-app, whether efsdriver's Firestore writes reach BC correctly. Today
  every dispatched card starts with zero memory of prior work on related-but-
  different repos.

---

## 1. Would per-project profiles genuinely help, or is the real gap something else?

### The granularity mismatch is the core problem

The stated friction is **cross-project** context (app ↔ driver ↔ BC). A
one-profile-per-repo scheme gives each repo its own memory, but that memory is
scoped to *that repo's* work only. When an `ecofire-app` worker needs to know
whether a BC API page change affects the app, they need **BC context** — not
more app context. A per-repo `ecofire-app-dev` profile would hold only app
knowledge. It cannot help the cross-repo trace.

Strikingly, even the task's own motivating example is a *shared-family* argument,
not a per-project one:

> "a memory of 'get_bc_items already had the 501-on-OR bug fixed once, don't
> reintroduce it'"

`get_bc_items` lives in **ecofire-app** but the bug is **on the BC side** (501-on-OR
is a BC OData endpoint issue). To remember that, the *app* worker needs
shared-family knowledge — which a per-repo app profile does not carry. The most
compelling example in the brief is only satisfiable by a profile whose memory
spans the whole family, not by per-repo profiles.

**So: "one profile per repo" is the wrong granularity for the very friction it
purports to solve.** The only profile shape that targets the friction at all is a
single profile shared across the family.

### Is accumulated memory even the right carrier?

Two reasons it is not the primary fix:

1. **Memory is not reliably populated under kanban dispatch.** The existing
   `ecofire-bc-engineer` profile is living proof (§3 below): it exists, is a real
   project-rooted profile, has been used (6 sessions), and has **zero memories**.
   The fleet does not currently nurture per-profile memory — workers on role
   profiles don't reliably call the `memory` tool. Creating more empty profile
   stores does not create the memory; memory only helps if something writes it.

2. **Memory is a poor carrier for architectural tracing.** The facts the friction
   describes ("does a BC page change affect the app?") are *derived, situational,
   traced live* — task-execution reasoning, not stable durable facts. Memory
   (`MEMORY.md`, 2200-char cap, §-delimited, manually curated) is designed for
   high-signal durable facts ("ecofire-app's test command is X", "this repo uses
   pytest"), not for on-the-fly architecture tracing. Tracing requires *reading
   the related repo's code*, which any worker is already free to do with its
   terminal/file toolset. What the worker actually lacks is not memory — it is
   **knowing which repos are related**. That is precisely the `depends_on` gap the
   sibling card closes.

### The real gap is discoverability, and the dependency link fixes it directly

When (a) a card is anchored to a project's repo via its worktree, and (b) the
registry records `ecofire-app depends_on ecofire-bc`, the dispatcher/card body
can deterministically tell the worker "the related repos are A, B, C and here's
how they're wired." The worker then *reads* the actual sibling repos as needed.
This is deterministic, complete, and fresh. Memory is probabilistic, partial,
and stale-able. For cross-repo tracing, the link + direct reads strictly win.

---

## 2. How dispatch resolves the assignee (design question 2)

### The assignee IS a Hermes profile name

The full chain, verified by reading the code:

1. `flightdeck message dispatch <project> "task" --assignee X`
   (`flightdeck/commands/message.py:cmd_dispatch`) → passes `assignee=X`
   straight to `kanban.create_task(...)`.
2. `create_task` (`flightdeck/core/kanban.py:966`) sets the card's `assignee`
   field to `X` via `kdb.create_task(conn, assignee=X)`.
3. The Hermes dispatcher (`hermes_cli/kanban_db.py:dispatch_once`) decides
   spawnability via **`profile_exists(assignee)`** (line ~8399):
   - if the assignee is NOT a real profile on disk → `skipped_nonspawnable`
     (the card is never worked);
   - if it IS a real profile → spawn a worker under `-p <assignee>`.

So **`--assignee` on dispatch must be the exact name of a Hermes profile**.
Any project-scoped profile we create becomes a first-class, valid dispatch
target with **zero changes to flightdeck or the dispatcher**. The mechanism
already supports per-project routing; nothing new needs building on the
dispatch side.

### Role vs project: two orthogonal axes, but one single profile name

The task asks whether role-based assignment (skills/toolset/model for the WORK
TYPE) and project-based assignment (memory/context for the PROJECT) can
coexist. They are two orthogonal axes, but the kanban card has **one** assignee
field, and `profile_exists` maps that one name to one profile. So the axes must
be *composed into a single profile*, not named separately.

Hermes provides the composition primitive directly: `create_profile(...,
clone_from=<role>)` with `clone_config=True` copies the role's `config.yaml`,
`.env`, `SOUL.md`, installed skills, **and** `memories/MEMORY.md` + `USER.md`.
So `ecofire-app-backend-engineer = clone backend-engineer + own name` is
technically one API call.

**But there is a critical footgun:** `clone_config` also copies the source
profile's `memories/MEMORY.md` and `USER.md`. Cloning a fleet-wide role's
memory into a project profile would **inherit the generic role's shared memory
that spans all projects** — exactly the cross-project pollution the user wants
to escape. So the correct composition for a project profile must **NOT** clone
memory; it needs a fresh `<project>/memories/` directory.

This is precisely what `hscc-roles generate` already does right: it calls
`create_profile(name, no_skills=True)` and writes `config.yaml`/`SOUL.md`
manually, giving each profile a **clean, empty memory store** (§3). So the
correct primitive for a per-project profile already exists and is proven: the
`hscc-roles` generator with a project-scoped spec. Don't invent a new mechanism.

### Composition is possible but overkill for 3 related repos

We *could* mint `ecofire-app-backend-engineer`, `efsdriver-backend-engineer`,
`ecofire-bc-engineer` (exists), and 3× the maintenance for a marginal benefit.
The fleet already has 24 role profiles; the marginal value of cloning each into
a project-bound variant is low for a tightly-coupled trio. Composition is
*possible*; it is not *proportionate* given the granularity mismatch in §1.

---

## 3. What the existing `ecofire-bc-engineer` profile proves (evidence)

On disk (verified today):

- **Profile exists**: `~/.hermes/profiles/ecofire-bc-engineer/` — a real
  project-rooted role, generated by `hscc-roles`, with SOUL, config, skills,
  sessions. **6 session dumps** under `sessions/`.
- **Zero memories**: `memories/` is empty — **no `MEMORY.md`, no `USER.md`**.
- **SOUL is single-project** (BC only): its identity names
  `~/dev/EcoFire_customizations_bc` and nothing else — **no mention of
  ecofire-app, efsdriver, or Firestore**. So even its (deterministic) identity
  carries zero cross-repo context.
- **state.db is 78 MB** — the per-profile session-state cost.

Three conclusions follow:

1. **Creating a project-rooted profile is already fully supported end-to-end**
   (author spec → generate → dispatcher-ready). The mechanism is not the gap.
2. **A dedicated profile does not, by itself, produce continuity.** Despite
   existing and being used, it accumulated nothing. Memory only accumulates if
   workers actively write it — and they don't today. This is the strongest
   evidence against the optimistic "it would accumulate memory across every
   dispatched card" premise.
3. **Even a project-rooted *role* only fixes within-project capability, not
   cross-project context.** Its identity is deterministic (rebuilt from spec on
   every `generate`) and BC-only. To get cross-repo context into it you'd have to
   either (a) manually enlarge its SOUL to name the whole family — which the
   `depends_on` link captures more cheaply and more accurately — or (b) rely on
   memory, which isn't being written.

---

## 4. Cost / complexity (design question 4)

- **Profile count.** 24 roles + `default` = 25 profiles already on this host.
  Per-repo profiles for the family (app, bc, driver, + powerbi/ecofire-powerbi
  which is also registered) = **4 more**, each needing a role spec to author and
  regenerate. A shared family profile = **1 more** spec.
- **State growth (the real cost).** Each profile carries a `state.db` of
  **~78 MB** (session history) plus skills/sessions/cache. 25 profiles ≈ ~2 GB
  of per-profile session state already. 4 more ≈ +300 MB; 1 more ≈ +80 MB, both
  growing over time. Memory itself is negligible (2200 chars max) — the storage
  cost is session state, which per-project profiles multiply.
- **Maintenance.** Each profile spec is data (`roles/*.yaml`). Authoring +
  keeping routing descriptions / model tiers current is recurring overhead. The
  existing 22-role roster is already a large surface; project layers multiply it
  linearly with no shared cost.
- **Decomposer routing.** The kanban decomposer matches task →
  `routing_description` to pick an assignee. More project-scoped profiles means
  more routing_description overlap/confusion for the LLM assigner, not less.

---

## 5. The one version worth a pilot: ONE shared EcoFire-family profile

If the user wants to test whether per-project(ish) memory genuinely pays off,
the only shape that targets the stated friction is a **single profile spanning
the whole family** — `ecofire-family` — because the friction is cross-repo and
only a shared store can hold cross-repo integration facts.

### What it would hold (real, durable, cross-repo facts)

The kind of stable knowledge that genuinely reduces re-derivation, distinct
from on-the-fly tracing:

- `ecofire-app get_bc_items` fetches from BC OData v2.0 endpoint `.../API/v2.0/...`;
  a **501-on-OR** response = a server-side BC app-code error (not a client bug).
- efsdriver writes Firestore docs in collection `X` which `nightly` syncs into BC
  table `Y`; the sync path is `...`.
- The family's coupling map: app → BC via OData; driver → BC via Firestore sync;
  Power BI reads BC + Firestore.

These are bounded, high-signal, durable coupling facts — exactly what a 2200-char
curated MEMORY.md is for. This is a real (if narrow) kernel of value.

### The mechanism (reuse `hscc-roles`, don't invent)

1. Author one spec — `hscc-roles create ecofire-family "<identity spanning the
   three repos and their coupling>"` → writes `roles/ecofire-family.yaml`.
2. `hscc-roles generate` → builds `~/.hermes/profiles/ecofire-family/` with a
   fresh, empty memory store and the worker toolset (no cluster control).
3. Dispatch family-crossing cards with `--assignee ecofire-family` (or have the
   decomposer route to it for integrated tasks). The dispatcher already resolves
   it via `profile_exists` — no flightdeck/kanban change needed.
4. **Seed** the memory store once with the coupling map above
   (`memory add` a handful of durable facts).

### The honest success criterion

The pilot only means anything if **memory actually gets populated** as cards
run. Given `ecofire-bc-engineer`'s empty memory, the null hypothesis is that
workers won't write it. So define the pilot as: run N (say 10) family cards
against `ecofire-family`, then check `memories/MEMORY.md`. If it has grown to
real content, the idea works — consider seeding family-wide. If it is still
empty (the likely outcome per §3's evidence), **drop the profile and rely on
`depends_on` linking + direct repo reads**, which is the deterministic fix.

---

## 6. Recommendation (summary)

1. **Do NOT build per-repo profiles.** Wrong granularity (doesn't carry
   cross-repo context), 4× the cost, near-zero benefit against the stated goal.
   The most compelling motivating example is inherently a shared-family fact.
2. **Per-project profile memory is not the fix — the dependency link is.**
   The real gap is cross-repo *discoverability*. The sibling card's `depends_on`
   on the flightdeck registry makes the family relationships deterministic, and
   workers then read the actual sibling repos directly. This is more reliable
   than accumulated memory, which current dispatch habits don't populate anyway
   (`ecofire-bc-engineer`: 6 sessions, zero memories).
3. **Composition (role × project) is technically possible** via
   `create_profile(clone_from=<role>)`, but clone_config copies memory (a
   pollution footgun) and it is overkill for a tightly-coupled trio. If ever
   needed, the correct primitive is `hscc-roles generate` (`no_skills=True`,
   manual config → fresh memory store), not cloning.
4. **Optional, single cheap pilot:** one shared `ecofire-family` profile seeded
   with the durable cross-repo coupling map, success = memory gets populated
   over ~10 cards. If it doesn't, drop it. This is the only shape of the idea
   that addresses the real friction, and it's one spec + one generator run.

**Bottom line:** project-linking alone (the sibling `depends_on` card) is the
real answer to the stated friction. Per-project profiles are a solution looking
for a problem that the granularity mismatch and the empty-memory evidence both
rule out; at most, a single shared-family pilot is worth a cheap experiment, not
a committed investment and never a blanket all-projects-get-a-profile policy.
