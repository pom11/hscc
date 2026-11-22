kind: Fixed
task: t_fc2c09cb

* paneless-skip: the pane-expectation verdict is now cached per (board, profile) instead of per board, so an orchestrator lane that is NOT the retired cluster identity can no longer borrow another profile's "no pane is expected" answer and go silent; it is nudged (and audited) as before.
---
kind: Verified
task: t_fc2c09cb

* orch-waker: the `general -> general-orch` FAILED cycle is CLOSED, measured rather than assumed: that lane target carries the retirement marker hscc writes in its SOUL and is also the legacy cluster identity of the board the main orchestrator owns, so the paneless-skip names it, the batch drains and no audit line is written; the last failed attempt for the lane in the waker audit trail predates the daemon process carrying the #587 gate by 17 seconds, and that process recorded no attempt for the lane in the following 4h12m. No board was added to skip_boards.
* orch-waker: the general lane is now pinned in the shape the live install has it — a busy tmux server with panes for other orchestrators and none for the target, each half of the pane-expectation record carrying the lane alone, one pane for the target still nudging, and a sweep that refuses never skipping — so a future relaxation cannot put the lane back on the 30 s metronome without a test going red.
