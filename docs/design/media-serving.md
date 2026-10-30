# Media serving on the Sparks — design for operator review (M0)

**Date:** 2026-10-30
**Status:** Proposed — design only. No recipes, no code, no fleet action in this card.
**Card:** t_13c64d9b (media workstream M0).
**Depends on:** `docs/design/media-serving-support-matrix.md` (the merged support
matrix, PR #117). This doc does not restate its evidence table; it consumes its
verdicts.
**Supersedes nothing.** ComfyUI does not run on the nodes — operator decision
2026-10-30, restated in §7 because every other section depends on it.

Scope: how an image/video model becomes a **media unit** in this fleet — a pure
model server on a Spark, addressed by alias through the LiteLLM proxy on `:4000`,
declared by a cluster template, staged from the NAS, co-tenanted with LLM units.
Eight sections, then the open questions the operator has to rule on.

Sections: 1 runtime · 2 facade API contract · 3 LiteLLM registration ·
4 template schema · 5 VRAM budget · 6 NAS/staging · 7 security boundary ·
8 open questions.

---

## 1. Runtime per model

<!-- FILL -->

## 2. Facade API contract per unit

<!-- FILL -->

## 3. LiteLLM registration

<!-- FILL -->

## 4. hscc template schema extension

<!-- FILL -->

## 5. GPU memory budget vs LLM units on the same node

<!-- FILL -->

## 6. NAS / staging paths and the local-NVMe rule

<!-- FILL -->

## 7. Security boundary

<!-- FILL -->

## 8. Open questions for the operator

<!-- FILL -->
