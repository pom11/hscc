Bug on {{project}}.

Context (derived — already filled):
- Project: {{project}}
- Repo: {{repo}}
- Current branch: {{branch}}
- Verify command: {{verify}}

SYMPTOM: {{symptom}}
REPRO: {{repro}}
EXPECTED: {{expected}}

Fix the root cause, not the symptom. Add a regression test that fails before
and passes after. Apply the same card-quality rules: one concern, a VERIFY:
line, concrete file/function references, and acceptance criteria a test would
fail without.
