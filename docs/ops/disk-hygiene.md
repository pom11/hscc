# Disk hygiene — `hscc clean` (kanban workspace archive + removal)

**Cards:** engine `t_274530ae` (`hscc_daemon/workspace_clean.py`) ·
CLI verb `t_fc9296a7` (this doc) · rebuildable-cache tiers `t_a4d6b78d` (not
shipped yet — there is **no `--deep` flag**).
**Code:** `hscc_daemon/workspace_clean.py` (plan + executor),
`hscc_daemon/hscc.py::_handle_clean` (the verb: argv, human view, `--json`).
**Reference behaviour:** `~/bin/clean-mac.sh` § *1. finished kanban workspaces*
— the hand-maintained script this verb productises.

What it does, in one line: finds every **finished** card (status `done` or
`archived`) whose workspace has been untouched for longer than `--days`
(default 7), keeps any of them that looks even slightly live, and — only with
`--apply` — archives each remaining workspace and then removes it.

Why it exists: this hygiene lived only in a shell script on the gateway,
invisible to bootstrap and to review, reading the board DBs with its own
`sqlite3` calls and ending with a `git worktree prune` sweep across `~/dev`.
Productising it meant two hard changes: hermes is the only board authority
(every root and every row comes from hermes' own accessors, never from a
hand-written query), and `~/dev` is never touched.

---

## The verb

```
hscc clean                      # DRY RUN (default) — prints the plan, changes nothing
hscc clean --apply              # perform the safe set
hscc clean --days N             # age cutoff for finished workspaces (default 7)
hscc clean --board <slug>       # one board instead of every enumerated board
hscc clean --json               # machine output
```

`--deep` does not exist. An unknown option — `--deep` included — exits `2` with
the usage line rather than being quietly ignored; card `t_a4d6b78d` adds the
cache tiers as its own surface. `hscc clean --help`, `hscc clean -h` and
`hscc help clean` print the same text and execute nothing: no board read, no
`git`, no engine import (audit invariant D).

**The dry run is the default and it is read-only by construction.**
`plan_clean()` performs no create, move, delete or write-open; its git probes
run under `--no-optional-locks` precisely because a plain `git status`
refreshes `.git/index`, which a "read-only" pass has no business doing. The
human view makes the mode unmistakable: the header carries
`MODE: DRY RUN (add --apply to clean)` and every mutating line starts with
`would`. `--apply` prints `MODE: APPLY` and ends with the freed-bytes total.

`--json` is the engine's own dict, verbatim: the plan
(`days`, `cutoff`, `actions`, `skipped`, `errors`) for a dry run, the result
(`apply`, `archived`, `removed`, `skipped`, `errors`, `freed_bytes`) for
`--apply`. No Rich, no ANSI, no added timestamps, byte-stable.

Exit status: `0` when the pass completed, `1` when it reported an error (an
unreadable board, a workspace that vanished mid-pass, a failed archive) —
skips alone are not an error.

## What the verb covers

Only one thing: a **finished card's own workspace directory**, derived as
`<workspaces root for that board>/<task id>`. `hscc clean` reads each board
through hermes (`list_boards`, `list_tasks`, `workspaces_root`), derives that
path itself, and re-checks containment against the derived root in the plan and
again in the executor. `tasks.workspace_path` is treated as untrusted input and
is never used as a removal target — a row pointing at a real repository would
otherwise turn a cleanup into a repo delete.

## What it never touches

* anything under `~/dev` — no repo, no worktree registration is pruned or
  swept. A workspace this verb removes leaves through
  `git worktree remove`, which keeps the parent registry consistent for
  exactly what was removed and is all the pruning this engine is entitled to
  do;
* `~/.hscc` (daemon state, streams, overlays, the merge queue);
* hermes config, profiles, sessions and memory;
* the board databases, and everything card history lives in: comments,
  attachments and events all sit outside `workspaces/`, so card and board
  history survive by construction;
* unfinished cards — `ready`, `running`, `blocked`, `todo`, `triage`,
  `review` and `scheduled` are not candidates at all, and a card inside the
  `--days` window is not a candidate either;
* a workspace with an unexpired claim lock (a worker still holds it), one with
  uncommitted changes, unpushed commits, a stash, or a git probe that could not
  be read. Each of those is **kept and named** in `skipped` with its reason
  (`dirty`, `unpushed`, `stashed`, `unreadable`, `claim`, `archive-failed`) —
  unreadable counts as dirty, because guessing that files are disposable is
  how data gets lost.

## The archive, and how to restore one

Nothing is deleted that has not first been archived. The archive is written to
`<archive>.part`, verified with `tar -tzf`, and only then renamed into place;
any failure in that sequence deletes the `.part` and **keeps the workspace**.
Archives land under the hermes home hermes resolved:

```
<hermes-home>/kanban/workspace-archive/<board>/<task-id>.tar.gz
```

Rebuildable noise (`.git`, `__pycache__`, `node_modules`, venvs, `.dart_tool`,
`.pytest_cache`, `DerivedData`, `.build`) is excluded; everything else is kept —
decision docs, reports, probe output, git-ignored results.

To put a workspace back:

```
mkdir -p ~/.hermes/kanban/boards/<board>/workspaces/<task-id>
tar -xzf ~/.hermes/kanban/workspace-archive/<board>/<task-id>.tar.gz \
    -C ~/.hermes/kanban/boards/<board>/workspaces/<task-id>
```

The archive holds the workspace contents (it is created with `tar -C <ws> .`),
so extracting into the directory named above restores the layout the card had.
The `.git` of any clone inside it is not in the archive — re-clone or
`git worktree add` again; the workspace's non-git work is all there.

## Standing note — the gateway crontab line

The operator's weekly script is still installed on the gateway and still runs
with `--apply`:

```
30 4 * * 0 PATH=… ~/bin/clean-mac.sh --apply > ~/fleet-check/backups/clean-mac-weekly.log 2>&1
```

**That line stays until the operator moves it.** It is not retired by this
verb: `clean-mac.sh` also does six sections `hscc clean` does not cover
(hermes worker logs, Xcode device support, caches, Trash, and the
`FREE_BEFORE`/`FREE_AFTER` report), and it is the only thing today that applies
this cleanup on a schedule. Retiring or re-pointing that crontab entry is its
own card, with its own diff — until it lands, the weekly script is the live
hygiene path and `hscc clean` is the reviewed, dry-run-first one.
