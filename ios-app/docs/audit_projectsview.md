# Screen audit: ProjectsView — prove every element works

Task: t_bee9db8a (ios-engineer). Full audit of
`ios-app/Sources/HSCC/Views/ProjectsView.swift` — the app's home surface and
entry to every project.

Methods: compile (build_check.sh), live read-only API fetches with real values,
route cross-check vs scripts/api_route_sweep.py, code reasoning. NO iOS runtime
here — every finding below is marked (executed) or (reasoning) per the rule.

## Findings so far
(work in progress — appended as verified)
