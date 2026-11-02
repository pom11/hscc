kind: Fixed
task: kmon-profile-validate

- **`hscc kanban monitor` only opens real profiles' state.db.** The
  orchestrator list takes profile names from the process list, which any local
  process can shape; a name now has to be a plain token naming an existing
  `~/.hermes/profiles/<name>` directory before it becomes a path, and control
  characters (ANSI escapes) are stripped from profile names and session titles
  before rendering.
