# Bootstrap audit 2/3 — detect / doctor / enable_plugins / ensure_review_feature (correctness + silent-failure)

Task: t_ef341cfe · branch wt/t_ef341cfe · scope: audit ONLY these 4 files.

## GOAL (from card)
Correctness + silent-failure, specifically:
- swallows an exception and reports success (anti-pattern `except Exception: return None` — a failure to look is never a data fact)
- writes config without saying so
- reverts an operator's choice
- an ENVIRONMENT fault rendered as a fact about DATA

## Findings

(findings in progress)

## Reviewed and accepted (no change)

(under review)

## Fixes

(under review)

## Verification

- [ ] hscc-bootstrap suite green (host interpreter)
- [ ] hscc-bootstrap suite green (p313 interpreter)
- [ ] full `scripts/run_tests.sh` suite (host interpreter default)
- [ ] full `scripts/run_tests.sh` suite (p313 via HSCC_TEST_PY)
- [ ] merge to main + push
- [ ] deploy (install_payload.py)
