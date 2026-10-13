kind: Added
task: t_tmux_chat_20261013

- **Every interactive chat the hscc CLI opens is now tmux-aware.** The iOS
  app delivers chat by typing into the project's REPL *pane*
  (`hscc-api/repl_inject`), so a chat started outside tmux was invisible to
  the phone — messages silently went to a hidden serve-hosted session.
  `hscc project chat` (and its `--resume` door) now goes through one shared
  helper, `flightdeck/core/tmux_chat.py`: inside tmux the chat opens in
  place as before; outside, it opens in (or attaches to) the named session
  `hscc-<project>`; when tmux is unusable (not installed, broken server, the
  named session runs another profile's REPL) it **stops and asks an explicit
  Y/N naming what is missing and the consequence — a silent non-tmux
  fallback never happens**, and non-interactive callers are refused, not
  surprised. The real tmux/ps/stdin seams carry the `repl_inject`-style
  identity guard (they refuse under pytest). `hscc-project/tests/test_tmux_chat.py`:
  15 cases (branches A/B/C incl. reuse/chatless-window/foreign-profile/no-TTY).
- **`hscc doctor` gained an iOS chat-reachability section.** Per registry
  project: is its `-orch` REPL live, and does it own a tty a tmux pane
  claims? `problem` (outside tmux — the exact consequence spelled out) or
  `unverified` (tmux unconsultable) now make the run NOT ALL CLEAR; idle
  projects stay `ok` (no wolf-crying). Same opt-in sentinel + injectable-seam
  pattern as the learning pipeline; production `run()` always enables it.
  `hscc-project/tests/test_doctor_tmux_repl.py`: 10 cases.
- **bootstrap reports the tmux/REPL split at install time**: tmux presence
  (warn + `brew install tmux` when missing) and one line per running REPL
  chat — `IN_TMUX` (iOS can reach it) or `OUTSIDE_TMUX` (messages would MISS
  it; reopen with `hscc project chat <project>`). The report helper loads the
  installed `tmux_chat` matcher by path, so launch door, doctor, and
  bootstrap can never drift in what "a REPL chat" means.
- **The serve-fallback WS frame now carries a human `notice`** ("Your
  terminal REPL isn't in tmux, so this went to a background session…"), and
  the iOS `SystemRow` renders `details.notice` as its own full-width line —
  the operator sees where the message actually went instead of watching a
  dark terminal.

kind: Verified
order: 1

- Branch A/B/C each reproduced against a REAL tmux server with a throwaway
  `pom` project (`pom-orch` profile; `hscc-pom` session, window reuse,
  Y/N prompt answered `n` in a live pane): outside-tmux creates
  `hscc-pom` running `hermes -p pom-orch chat --continue pom`; a second call
  attaches without opening a second chat (window count unchanged); inside
  tmux the chat execs in that pane; tmux hidden from PATH gives the named
  reason + consequence and refuses on `n` (rc 1, nothing exec'd); no-TTY
  refuses with the same message.
- `doctor` tmux/REPL section run LIVE against the real registry: this
  pane's `hscc-orch` reads "is in a tmux pane — iOS delivery targets it";
  idle projects read the hint; triangle stays 12/12 green (read-only).
- The live bootstrap-helper run caught headless kanban workers (`chat -q`,
  no tty) being miscounted as operator REPLs — now excluded (an interactive
  REPL always owns a tty), pinned by test.
- `xcrun swiftc -parse` on the edited view; iOS build leg recorded in the PR.
- Suite green under BOTH interpreters at the pushed tip (see PR).
