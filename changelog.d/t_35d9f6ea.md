kind: Fixed
task: t_35d9f6ea

* the `hscc_daemon` test suite no longer has a path from which a test can delete the operator's real launchd jobs: `conftest.py::_isolate_hscc` now redirects `event_driven.PLIST_DIR` (and that module's `HSCC_DIR` / `STATE_DIR`) to the per-test tmp dir, because `~/Library/LaunchAgents` sits outside the `~/.hscc` tree its `expanduser` rewrite covers and the module's constants were in neither list. `LaunchdJobGenerator.uninstall_job` does a bare `os.remove(<PLIST_DIR>/<label>.plist)` for every periodic stream, so a single case that forgot to patch would have de-Registered the operator's `com.nousresearch.hscc-periodic.*.plist` jobs with no subprocess for any fence to see.
* the launchd directory is now a measured arm of the no-live-leak guard: `test_no_live_hscc_leak.py` plants the plist set the code would touch in a sandboxed `HOME`, runs the event-driven CLI cases there, and byte-diffs that directory too — the seal is load-bearing (those cases no longer patch any path of their own) instead of assumed.
---
kind: Verified
task: t_35d9f6ea

* the autouse seal is pinned from inside the fixture, reading the production attributes back: the fixture guard-asserts `event_driven.PLIST_DIR` / `HSCC_DIR` / `STATE_DIR` on every case, because a case that patches its own path stays green forever once the seal is deleted and `monkeypatch.setattr(..., raising=False)` would create a misspelled attribute rather than fail. Nothing here touches the real `~/Library/LaunchAgents`; the real path is compared as a string only.
