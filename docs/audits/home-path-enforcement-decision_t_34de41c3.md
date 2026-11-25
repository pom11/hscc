# Home-path enforcement: the decision (t_34de41c3)

Status: **IN PROGRESS** — measurement phase. This file is the card's real
deliverable (the posture decision) plus the evidence behind it.

## Context

`scripts/address_guard.py` @ `origin/main` 21da34a8 detects home paths inside the
ONE `FORBIDDEN` pattern but reports them ADVISORY (`HOME_PATHS_ENFORCE = False`).
This card decides how to get to `HOME_PATHS_ENFORCE = True` without reddening the
merge queue, and lands it.

Baseline facts to re-measure (numbers in the card body are from the parent card):

* home offenders across tracked files: TBD
* files affected: TBD
* operator-path offenders (genuine PII): TBD

## Measurement

TBD — per-user and per-area breakdown, produced by the shipping guard over
`git ls-files` (not `git grep`), with `HSCC_ADDRESS_GUARD_HOME_ENFORCE=1`.

## Classification

Each offender is one of:

* **FP** — not a home path at all (relative-path substring, prose ellipsis,
  dot-prefixed junk). Fix: the pattern (option 3), not the file.
* **FIXTURE** — a genuine-shaped home path used as test/doc fiction. Fix: scrub
  to the sanctioned fixture user (option 1).
* **PII** — names the operator's real machine/account. Fix: scrub to
  `/Users/<operator>` and it must never have been advisory.

## The decision

TBD

## Evidence that the flip bites

TBD
