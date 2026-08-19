# Flightdeck roadmap

# Subproject: trustworthy-signal

## Milestone: honest-digest <!-- id: honest-digest -->
status: now
- [x] standup FAILING / DRIFT / starved-vs-working
- [x] attribute cards by repo path, not board slug
- [x] coverage footer — never print a digest the tool could not read
- [x] reconcile close-safety (ancestor of main is not proof work landed)
- [x] reconcile + hygiene use repo-path attribution too

## Milestone: release-flow <!-- id: release-flow -->
status: next
- [x] release preconditions + dry-run plan
- [x] release execution — bump, commit, tag, push, gh release
- [x] install + post-install verification (merged is not live)

# Subproject: work-loop

## Milestone: milestone-tracking <!-- id: milestone-tracking -->
status: now
- [x] ROADMAP.md subprojects + milestones with stable ids
- [x] decompose --milestone stamps MILESTONE: into every card
- [x] roadmap progress links cards and renders counts
- [x] start — concurrency-aware dispatch of a milestone
- [x] qa --watch and --notify so a QA request reaches the operator

## Milestone: review-loop <!-- id: review-loop -->
status: later
- [x] why <card> — one card's full story across kanban and git
- [x] metrics: first-time-pass rate, stall rate, review latency
- [x] incident log so lessons live in the repo
