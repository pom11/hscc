#!/usr/bin/env python3
"""R7 README lint — badge==VERSION + quoted-command existence (audit §3).

Rules (``docs/audits/README_REVIEW_2026-10-15.md`` §3, "R7 lint rules"):

  (1) badge == VERSION   every shields.io badge whose label asserts a
      version (``version``/``v``/``release``) must carry the repo's own
      version — root ``pyproject.toml`` ``[project] version`` when present,
      else the root ``VERSION`` file (what ``hscc --help`` prints via
      ``hscc_daemon/hscc.py`` ``_get_version``; this repo has no root
      pyproject, so the VERSION file IS the shipped truth — the fallback is
      the repo's behaviour, not a shortcut around the audit's wording).
      Motivating bug (audit §1.1): the root README badge drifted to a stale
      version while ``VERSION`` moved on.

  (2) quoted commands    per audit §3 the inventory is the backticked
      ``hscc*``/``hermes*``/``sparkrun*`` commands quoted in a README. An
      ``hscc …`` token must resolve against the real CLI surface — checked
      through the SAME code-derived oracle the CLI-help audit uses
      (``hscc_cli_help_audit.ExampleParser``: flightdeck's real argparse
      parser + the daemon's dispatch tables; parse-only, NEVER executed).
      Verb EXISTENCE is the contract here — READMEs quote verbs with their
      required args elided (``hscc project review`` IS a real command even
      though argparse rejects it bare), so an argparse rejection is demoted
      to a verb-existence question against the code-derived tables. The (C)
      pass of the CLI-help audit still validates full argument shapes on
      ``example:`` lines; this lint does not re-validate flags. External
      CLIs (``sparkrun``, ``hermes``, ``git``, ``brew``, ``xcodebuild`` …)
      are allow-listed strings, not parsed by the hscc oracle — per audit
      §3. Repo-local scripts (``scripts/foo.py``, ``hscc.py``,
      ``python3 scripts/x.sh``) must exist in the tree.

Documented boundaries — never silently ignored; every skip is counted in
``report["skipped"]`` so a shrinking check surface stays visible:

  * fenced code blocks are OUT of scope (the rule says "commands quoted";
    runnable transcripts are covered by the CLI-help (C) pass and the
    R-cards' live ``--help`` verification);
  * ``hscc`` heads whose args are ALL-CAPS skeletons (``hscc project X``),
    ``<placeholders>``, ellipses, ``a | b`` / ``a/b`` alternative lists
    (``cluster up/down``, ``list/status/validate``) — documentation forms
    no parser can resolve: ``skeleton`` / ``alternatives``;
  * non-command tokens are NOT invented findings: single words and word
    lists (``deps not met``), branch names (``feat/hscc-api``,
    ``origin/main``), protocols/coordinates (``HTTP/1.1``, ``hscc://``,
    ``@reg/name``) skip as ``prose`` / ``bare-name`` / ``ref-name``;
  * file references whose leaf is not a ``*.py``/``*.sh`` invocation
    (``Views/AppShell.swift``, ``docs/assets/banner.png``, ``tests/``) are
    NOT command references — this lint checks COMMAND existence, not doc
    links (that was the R-cards' job): ``path-ref`` / ``dir-ref``. A
    ``*.py``/``*.sh`` reference is checked against the tree, resolved from
    the README's own directory upward (a doc's paths anchor where a reader
    standing in that directory would resolve them); a first segment that
    exists nowhere along that chain belongs to another repo's layout
    (``src/sparkrun/...``) and skips as ``foreign-path``;
  * absolute / ``~`` / ``$VAR`` paths describe machines that are not this
    one: ``external-path`` (HTTP routes ``/v1/…`` and Hermes chat
    slash-commands ``/cluster`` live under the same rule);
  * ``flightdeck*`` tokens are left to audit rule (3) + the §3.1 survival
    ledger (naming, not existence): ``flightdeck-rule3``;
  * data names (``autodown.json``, ``.stale``): ``data-name``.

Safety: this module NEVER executes a command quoted in a README. A quoted
mutating verb (``hscc cluster down --dry-run``) is linted as a STRING; the
audit §6 "run for real" smoke exceptions do NOT transfer to this gate.

Usage:
  python3 scripts/readme_lint.py                    # lint the repo's README set
  python3 scripts/readme_lint.py --json out.json    # machine report
  python3 scripts/readme_lint.py --strict           # exit 1 on findings
  python3 scripts/readme_lint.py --files a.md b.md  # explicit file set

Exit codes (same convention as scripts/address_guard.py): 0 clean, 1 a
finding, 2 the lint could not run (never a silent pass).

The pytest gate that executes in scripts/run_tests.sh lives in
scripts/tests/test_readme_lint.py; positive/negative fixture repos under
scripts/tests/fixtures/readme_lint/ pin both directions (anti-vacuity).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# ── (1) badge == VERSION ────────────────────────────────────────────────

_BADGE_URL = re.compile(r"img\.shields\.io/badge/([^/\s)\]]+)")
#: shield labels that ASSERT this repo's version. ``python``/``tests``/
#: ``license`` badges assert other things and are not version claims.
VERSION_BADGE_LABELS = {"version", "v", "release"}


def _split_shields(path: str) -> list:
    """``version-2.11.0-blue.svg`` -> ``['version','2.11.0','blue']``.

    Greedy URL capture keeps the ``.svg``/query tail in; shields.io escapes
    a literal ``-`` inside label/message as ``--``, so split on single
    hyphens only (a lazy regex capture here once matched just ``v`` of
    ``version-…`` and silently checked zero badges — caught by the
    badge-count floor test, not by a red run).
    """
    path = re.sub(r"\.svg(?:[?\#].*)?$", "", path)
    parts = path.replace("--", "\x00").split("-")
    return [p.replace("\x00", "-") for p in parts]


def repo_version(repo: Path):
    """The repo's own version, normalised (no leading ``v``); None if absent.

    Root ``pyproject.toml`` ``[project] version`` wins (the audit's wording);
    otherwise the root ``VERSION`` file — the value ``hscc --help`` prints.
    Sub-package pyprojects (``hscc-project`` 0.6.0, ``hscc-cli`` date-stamp)
    are NOT repo version and are deliberately not consulted.
    """
    py = repo / "pyproject.toml"
    if py.is_file():
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']',
                      py.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip().lstrip("v")
    vf = repo / "VERSION"
    if vf.is_file():
        v = vf.read_text(encoding="utf-8").strip()
        if v:
            return v.lstrip("v")
    return None


def _finding(kind: str, path, line: int, quote: str, repo: Path,
             detail: str, fix: str) -> dict:
    try:
        rel = str(Path(path).relative_to(repo))
    except ValueError:
        rel = str(path)
    return {"kind": kind, "file": rel, "line": line, "quote": quote[:160],
            "detail": detail, "fix": fix}


def check_version_badges(files, expected, repo: Path):
    """(findings, checked) over every version-asserting badge in ``files``."""
    findings, checked = [], 0
    for f in files:
        text = Path(f).read_text(encoding="utf-8", errors="replace")
        for n, ln in enumerate(text.splitlines(), 1):
            if "img.shields.io/badge/" not in ln:
                continue
            for m in _BADGE_URL.finditer(ln):
                parts = _split_shields(m.group(1))
                if len(parts) < 2:
                    continue
                label, message = parts[0].lower(), parts[1].strip()
                if label not in VERSION_BADGE_LABELS:
                    continue
                checked += 1
                claim = message.lstrip("v")
                if expected is None:
                    findings.append(_finding(
                        "BADGE-NOVERSION", f, n, m.group(0), repo,
                        "badge asserts a version but the repo version "
                        "(root pyproject [project] version / VERSION file) "
                        "cannot be read",
                        "restore the root VERSION file (or root pyproject)"))
                elif claim != expected:
                    findings.append(_finding(
                        "BADGE-VERSION", f, n, m.group(0), repo,
                        f"badge claims version {claim!r}, repo version is "
                        f"{expected!r}",
                        f"re-cut the badge to {expected} (or release so they "
                        "match)"))
    return findings, checked


# ── (2) quoted commands ─────────────────────────────────────────────────

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_ALLCAPS = re.compile(r"^[A-Z][A-Z0-9]{0,3}$")
_PLACEHOLDER = re.compile(r"^<[^>]*>$")
_ENVASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CONFIG_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*=")
_LINE_REF = re.compile(r"(?::\d+|::[\w.]+)$")
# shell-pipe/substitution metacharacters mark a PROSE expression. NOT `<`
# or `>`: those appear inside legitimate quoted commands as <placeholder>
# and comparisons (`tp > 1` classifies as prose downstream anyway — a
# blanket `<`/`>` kill here once skipped every placeholder-bearing command).
_META = re.compile(r"[;&\$`]")

#: external CLIs per audit §3 ("allow-listed strings, not parsed by the hscc
#: oracle") plus the plain Unix verbs READMEs quote inline. Entries were
#: added ONLY for commands actually quoted in this repo's README set (or
#: unavoidable shell verbs next to them) — an allow-list nobody quoted is
#: dead weight that hides future typos.
EXTERNAL = frozenset({
    # cluster/Hermes ecosystem
    "sparkrun", "hermes", "hf",
    # VCS / forge / packaging
    "git", "gh", "pip", "pip3", "uv", "pipx", "brew",
    # python test/doc tooling usable as a bare verb
    "pytest",
    # iOS / macOS
    "xcodebuild", "xcrun", "xcodegen", "swift", "swiftc", "simctl", "act",
    "security", "defaults", "osascript", "codesign", "plutil", "sysctl",
    "sw_vers", "launchctl", "systemctl", "open",
    # node
    "node", "npm", "npx", "yarn",
    # containers / net / observability
    "docker", "tailscale", "ssh", "scp", "rsync", "curl", "wget", "ping",
    "jq", "sqlite3", "nvidia-smi",
    # coreutils / shell verbs
    "bash", "sh", "zsh", "cat", "ls", "cp", "mv", "rm", "mkdir", "rmdir",
    "chmod", "chown", "ln", "touch", "echo", "printf", "grep", "rg", "sed",
    "awk", "head", "tail", "sort", "uniq", "wc", "find", "xargs", "tar",
    "gzip", "gunzip", "zip", "unzip", "du", "df", "ps", "pgrep", "pkill",
    "kill", "sleep", "tee", "date", "which", "whoami", "diff", "patch",
    "basename", "dirname", "realpath", "stat", "install", "timeout",
})

#: interpreters get their FIRST script/module argument checked (a README
#: that points ``python3`` at a script that no longer exists is exactly as
#: dead as a bad ``hscc`` verb), but their script's own args are not parsed.
INTERPRETERS = re.compile(r"^(python|python3|python3\.\d+|bash|sh|zsh|source)$")
#: modules ``python -m`` may legitimately name without being repo paths
EXTERNAL_MODULES = frozenset({"pytest", "pip", "http.server", "venv",
                              "json.tool", "unittest", "time"})

_WRAPPERS = frozenset({"env", "sudo", "nohup", "command", "time"})

#: file extensions that mark a token as a FILE reference, not a command
_SCRIPT_EXT = (".py", ".sh")          # checked (audit §3 script forms)
_ANY_EXT = _SCRIPT_EXT + (".md", ".json", ".yaml", ".yml", ".toml", ".txt",
                          ".cfg", ".db", ".csv", ".ini", ".plist", ".xml",
                          ".html", ".swift", ".png", ".svg", ".jpg", ".gif",
                          ".pdf", ".ipynb", ".lock", ".log")


def iter_inline_code(text: str):
    """(line_no, token) for every inline code span OUTSIDE fenced blocks."""
    fence = None
    for n, ln in enumerate(text.splitlines(), 1):
        m = _FENCE.match(ln)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            continue
        if fence:
            continue
        for mm in _INLINE_CODE.finditer(ln):
            t = mm.group(1).strip()
            if t:
                yield n, t


def tracked_basenames(repo: Path) -> set:
    """Basenames of every tracked file — the existence set for bare
    ``hscc.py``/``bootstrap.sh``-style quotes. Falls back to a plain walk
    when git yields nothing (fixture/tmp repos)."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return {Path(p).name for p in out.stdout.split() if p}
    except Exception:
        pass
    names = set()
    for p in repo.rglob("*"):
        if ".git" not in p.parts:
            names.add(p.name)
    return names


def default_readme_set(repo: Path) -> list:
    """The README set: root README + every tracked ``*README.md``.

    git-backed when possible (exact tracked set); fixture/tmp repos fall
    back to a glob. Fixture inputs (``*/fixtures/*README.md``) are lint
    INPUTS, not repo docs — they are excluded from the default set so the
    repo-wide gate stays meaningful; tests pass them explicitly.
    """
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "*README.md"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            files = [repo / p for p in out.stdout.split() if p]
        else:
            raise RuntimeError(out.stderr)
    except Exception:
        files = sorted(repo.rglob("README.md"))
    files = [f for f in files
             if "/fixtures/" not in f.relative_to(repo).as_posix()
             and f.is_file()]
    return sorted(set(files))


# ── oracle wrapper (CLI-audit reuse, never a second enumeration) ────────

_VERB_WORDS: set = set()  # filled by Oracle.__init__ from the real tables


class Oracle:
    """The CLI-help audit's parse-only oracle + verb-existence tables.

    ``parse`` is the untouched ``ExampleParser.parse`` (NEVER executes).
    ``exists_chain`` answers verb EXISTENCE on the flightdeck side, which
    the raw parse cannot: it models complete invocations, so a bare verb
    that argparse documents WITH required args (``hscc project review``)
    would "fail" the parse while being a perfectly real command. The verb
    tables are code-derived from the same objects the CLI-help audit walks.
    """

    def __init__(self, parser, fd_cmds):
        self._p = parser
        self.fd_cmds = fd_cmds
        self.fd_top = set(fd_cmds)
        self.daemon_heads = (set(parser.subs) | set(parser.daemon_cmds)
                             | {"help", "project"})
        self.heads = self.daemon_heads | self.fd_top
        verbs = set(self.heads) | {str(v) for v in parser.daemon_cmds}
        for table in parser.subs.values():
            verbs |= {str(v) for v in table}
        for subs in fd_cmds.values():
            verbs |= {str(v) for v in subs}
        globals()["_VERB_WORDS"] = {v.lower() for v in verbs}

    def parse(self, cmd: str):
        return self._p.parse(cmd)

    def exists_chain(self, tokens: list) -> bool:
        """True when the leading tokens form a registered command chain
        (``['review']`` from ``hscc project review <card>``, or
        ``['project', 'chat']`` for a subgroup verb). The first token that
        is neither the next registered verb nor a stripped placeholder is
        an ARGUMENT, so existence stops at the deepest registered verb.
        """
        cur = self.fd_top
        seen = False
        for t in tokens:
            if t in cur:
                seen = True
                cur = self.fd_cmds.get(t, {})
                continue
            break
        return seen


def build_oracle() -> Oracle:
    """Build the oracle from the CLI-help audit's own enumeration."""
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import hscc_cli_help_audit as cli_audit  # heavy import — audit lane
    fd_cmds = cli_audit.flightdeck_commands()
    return Oracle(cli_audit.ExampleParser(fd_cmds), fd_cmds)


# ── token classification ────────────────────────────────────────────────

def _strip_q(tok: str) -> str:
    return tok.strip().strip("\"'").strip()


def _is_skeleton_body(rest: list) -> bool:
    """Args are ALL-CAPS / <placeholder> / ellipsis only → verb + skeleton."""
    body = [_strip_q(t) for t in rest]
    return bool(body) and all(
        b == "" or _ALLCAPS.match(b) or _PLACEHOLDER.match(b)
        or b in ("…", "...", "/") for b in body)


def _wildcard(tok: str) -> bool:
    """A glob / placeholder / bracket reference — nothing to resolve."""
    return any(c in tok for c in "*?<>[") or "…" in tok or "..." in tok


def _verb_slash_list(tok: str) -> bool:
    """``list/status/validate/preview`` — a slash-separated VERB list.

    True only when EVERY segment is a verb the CLI actually has
    (``_VERB_WORDS`` comes from the code-derived tables) and there are 2+
    segments. ``scripts/run_tests.sh`` keeps falling through to the path
    check because ``run_tests.sh`` is not a verb.
    """
    segs = [s for s in re.split(r"[/\s]+", tok) if s]
    if len(segs) < 2:
        return False
    return all(s.lower().strip("<>()") in _VERB_WORDS for s in segs)


def _clean_arg(t: str) -> str:
    """Drop ``path.py:450`` / ``file.py::test_x`` line refs."""
    return _LINE_REF.sub("", t)


def _has_ext(tok: str, exts=_ANY_EXT) -> bool:
    leaf = tok.rstrip("/").split("/")[-1]
    return leaf.lower().endswith(exts)


def _anchor_dirs(readme: Path, repo: Path) -> list:
    """Directories a relative path in ``readme`` may anchor at: the README's
    own dir and its ancestors up to (and including) the repo root."""
    out, cur = [], readme.parent
    while True:
        out.append(cur)
        if cur == repo or cur.parent == cur:
            break
        cur = cur.parent
    if repo not in out:
        out.append(repo)
    return out


def _check_script_path(tok: str, readme: Path, repo: Path,
                       names: set) -> tuple:
    """A ``*.py``/``*.sh`` reference — the audit §3 script-command forms.

    Slash form: resolve from the README's dir upward; a first segment that
    exists as a directory THERE but whose leaf is missing is a genuine
    broken reference (fail); an unanchorable first segment belongs to
    another repo's layout (skip ``foreign-path``). Bare form: the basename
    must exist somewhere in the tree.
    """
    tok = _clean_arg(tok)
    if _wildcard(tok):
        return "skip", "wildcard-path"
    if "/" in tok:
        p = tok.rstrip("/")
        anchored = False
        for base in _anchor_dirs(readme, repo):
            first = Path(p).parts[0] if len(Path(p).parts) > 1 else ""
            if first and (base / first).is_dir():
                anchored = True
                if (base / p).exists():
                    return "pass", None
            elif (base / p).exists():
                return "pass", None
        if not anchored:
            return "skip", "foreign-path"
        return "fail", ("PATH-MISSING",
                        f"`{tok}` is anchored in this repo but exists "
                        f"nowhere from {readme.name}'s directory up to the "
                        "repo root")
    if tok in names:
        return "pass", None
    return "fail", ("PATH-MISSING",
                    f"`{tok}` names a repo-local script that exists nowhere "
                    "in the tracked tree")


def _check_interpreter(rest: list, readme: Path, repo: Path,
                       names: set) -> tuple:
    body = [_strip_q(t) for t in rest if not t.startswith("-")]
    if not body:
        return "pass", None
    first = _clean_arg(body[0])
    if first.startswith(("~", "/", "$")):
        return "skip", "external-path"
    if "/" in first or first.endswith(_SCRIPT_EXT):
        return _check_script_path(first, readme, repo, names)
    if first in EXTERNAL_MODULES:
        return "pass", None
    if re.fullmatch(r"[A-Za-z_][\w.]*", first):
        dotted = first.replace(".", "/")
        if (repo / f"{dotted}.py").exists() or \
                (repo / dotted / "__init__.py").exists():
            return "pass", None
    return "fail", ("CMD-BINARY",
                    f"`{first}` is passed to an interpreter but is neither "
                    "an existing repo script/module nor an allow-listed "
                    "module")


def _hscc_args(toks: list) -> list:
    """Args of an ``hscc …`` token, with --help/-h, <placeholders> and
    ALL-CAPS skeleton args stripped (``hscc project X`` documents a VERB,
    the skeleton stands for its operand)."""
    out = []
    for t in toks:
        t = _strip_q(t)
        if (t in ("-h", "--help") or _PLACEHOLDER.match(t)
                or _ALLCAPS.match(t) or t in ("…", "...")):
            continue
        out.append(t)
    return out


def _sub_table(head: str, oracle: Oracle):
    """The registered subcommand names for a group head, or None.

    Same code-derived tables the oracle itself consults — daemon groups via
    ExampleParser.subs, flightdeck groups via the fd command tree.
    """
    parser = oracle._p
    if head in parser.subs:
        return {str(v) for v in parser.subs[head]}
    if head in oracle.fd_cmds:
        return {str(v) for v in oracle.fd_cmds[head]}
    return None


def _slash_alt_ok(args: list, oracle: Oracle) -> bool:
    """True when ``args[1]`` is an ``a/b/c`` list and EVERY alternative is a
    registered subcommand of ``args[0]`` (``cluster up/down``). A half that
    does not exist is NOT excused — it falls through to the parse error."""
    if len(args) < 2 or "/" not in args[1] or args[1].startswith("-"):
        return False
    subs = _sub_table(args[0], oracle)
    if subs is None:
        return False
    segs = [s for s in args[1].split("/") if s]
    return len(segs) > 1 and all(s in subs for s in segs)


def _parse_hscclike(tail: list, oracle: Oracle):
    """None when the tail resolves; an error string otherwise.

    The tail follows ``hscc``. An argparse rejection on the flightdeck side
    is demoted to a verb-EXISTENCE question (READMEs quote verbs with
    required args elided — ``hscc project review`` is real even though
    argparse wants a card); the (C) pass of the CLI-help audit still
    validates argument shapes where they are documented.
    """
    args = [t for t in tail if not t.startswith("-")]
    if args == ["project"]:
        return None                        # bare group header is real
    if _slash_alt_ok(args, oracle):
        return None                        # `cluster up/down` — both real
    cmd = "hscc " + " ".join(shlex.quote(t) for t in tail)
    err = oracle.parse(cmd)
    if err is None:
        return None
    if args and args[0] == "project" and oracle.exists_chain(args[1:]):
        return None
    return err


def classify_token(tok: str, readme: Path, repo: Path, names: set,
                   oracle: Oracle) -> tuple:
    """(verdict, payload) — verdict in {"pass","skip","fail"}.

    fail payload: (kind, detail); skip payload: reason string.

    A `|` whose every occurrence sits inside [...] / <...> belongs to a
    documented ARGUMENT alternative (`hscc check [stream|all]` — the
    audit-sanctioned spelling), so the token proceeds and the verb path is
    validated normally. A bare `a | b` alternative list stays unresolvable:
    skip.
    """
    if "|" in tok and not re.fullmatch(r"[^|]*\[[^|\]]*\|[^|\]]*\][^|]*"
                                       r"([^|]*\[[^|\]]*\|[^|\]]*\][^|]*)*",
                                       tok):
        return "skip", "alternatives"
    if "->" in tok or tok.startswith(("(", "[")):
        return "skip", "expression"             # code signature, not a CLI call
    if _META.search(tok):
        return "skip", "expression"             # `tp > 1`, `$(…)` prose
    if _CONFIG_ASSIGN.match(tok):
        # pyproject/console-script entry: `hscc = "hscc_cli:main"` is TOML,
        # not an invocation
        return "skip", "config"
    try:
        toks = [t for t in shlex.split(tok, posix=False) if t.strip()]
    except ValueError:
        return "skip", "unlexable"
    while toks and (_ENVASSIGN.match(_strip_q(toks[0]))
                    or _strip_q(toks[0]) in _WRAPPERS):
        toks.pop(0)
    if not toks:
        return "skip", "no-command"
    head = _strip_q(toks[0])
    rest = toks[1:]
    if not head:
        return "skip", "no-command"
    if head.startswith("-"):
        return "skip", "flag"                   # a bare flag quote
    if head.startswith(("~", "/", "$")):
        # absolute / home / $VAR paths, HTTP routes (/v1/…), Hermes chat
        # slash commands (/cluster) — not references to THIS repo's surface
        return "skip", "external-path"
    if head.startswith("flightdeck"):
        # audit rule (3) + the §3.1 survival ledger own flightdeck naming;
        # this lint deliberately does not duplicate that rule
        return "skip", "flightdeck-rule3"
    if head.endswith(":"):
        # a YAML field or labelled quote (`build_args: --exp-b12x`,
        # `Nodes: 1 - 1`) — a label, not an invocation
        return "skip", "label"
    if ":" in head and not rest:
        # URL scheme (`hscc://`), protocol coordinate (`npm:ssha`)
        return "skip", "ref-name"
    if head == "hscc":
        if _is_skeleton_body(rest):
            return "skip", "skeleton"
        args = _hscc_args(rest)
        while args and args[0].startswith("-"):
            args.pop(0)
        if not args:
            return "pass", None                 # `hscc` / `hscc --help`
        err = _parse_hscclike(args, oracle)
        return ("fail", ("CMD-UNKNOWN", err)) if err else ("pass", None)
    if head in EXTERNAL:
        return "pass", None
    if INTERPRETERS.match(head):
        return _check_interpreter(rest, readme, repo, names)
    # --- non-hscc heads ------------------------------------------------
    if head in oracle.heads and rest:
        # group/verb shorthand in prose: `api start`, `qa --notify`,
        # `roadmap adopt --apply` — try the daemon surface, then the
        # project (flightdeck) surface
        if "/" in tok and _verb_slash_list(tok):
            return "skip", "alternatives"
        args = _hscc_args(rest)
        err = _parse_hscclike([head, *args], oracle)
        if err is None:
            return "pass", None
        err = _parse_hscclike(["project", head, *args], oracle)
        return ("fail", ("CMD-UNKNOWN", err)) if err else ("pass", None)
    if head in oracle.heads and not rest:
        return "pass", None                     # a bare verb/group mention
    if "/" in tok and _verb_slash_list(tok):
        return "skip", "alternatives"           # `enable/wake/cancel`
    if head.endswith(_SCRIPT_EXT):
        return _check_script_path(head, readme, repo, names)
    if "/" in head or head.startswith("."):
        leaf = head.rstrip("/")
        if "/" in head and leaf.lower().endswith(_SCRIPT_EXT):
            return _check_script_path(head, readme, repo, names)
        return "skip", "path-ref"               # dir / file / branch name
    if _has_ext(tok):
        return "skip", "path-ref"               # file ref, not an invocation
    if not rest:
        return "skip", "bare-name"
    if _is_skeleton_body(rest):
        return "skip", "skeleton"
    body = [_strip_q(t) for t in rest]
    if any(b.endswith(_ANY_EXT) or b.startswith(("~", "/", "$"))
           or _PLACEHOLDER.match(b) for b in body):
        return "skip", "data-name"
    if any(b.startswith("-") for b in body):
        return "fail", ("CMD-BINARY",
                        f"`{tok}` looks like an invocation of {head!r} "
                        "(takes a flag) but is not an hscc command, not a "
                        "repo-local script, and not on the external CLI "
                        "allow-list")
    return "skip", "prose"                      # `deps not met`, `apt update + upgrade`


# ── driver ──────────────────────────────────────────────────────────────

def lint_repo(repo, paths=None, oracle=None) -> dict:
    """Lint the README set; returns the report dict (never raises for a
    finding). ``oracle=None`` builds the CLI oracle once for the run."""
    repo = Path(repo).resolve()
    files = [Path(p) for p in paths] if paths else default_readme_set(repo)
    if oracle is None:
        oracle = build_oracle()
    names = tracked_basenames(repo)
    expected = repo_version(repo)

    findings, checked_badges = check_version_badges(files, expected, repo)
    checked_commands = 0
    skipped = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for n, tok in iter_inline_code(text):
            verdict, payload = classify_token(tok, f, repo, names, oracle)
            if verdict == "fail":
                kind, detail = payload
                findings.append(_finding(
                    kind, f, n, tok, repo, detail,
                    "point the quote at a real command (verify with "
                    "`--help`), restore the file, or extend the allow-list "
                    "only for a genuinely external CLI"))
            elif verdict == "skip":
                skipped[payload] = skipped.get(payload, 0) + 1
            else:
                checked_commands += 1
    report_files = []
    for f in files:
        try:
            report_files.append(str(Path(f).relative_to(repo)))
        except ValueError:
            report_files.append(str(f))
    return {
        "repo": str(repo),
        "version_expected": expected,
        "files": sorted(report_files),
        "badges_checked": checked_badges,
        "commands_checked": checked_commands,
        "skipped": skipped,
        "findings": findings,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "R7 README lint").splitlines()[0])
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--files", nargs="*", default=None,
                    help="explicit README files (default: tracked *README.md)")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when there are findings")
    args = ap.parse_args(argv)
    try:
        report = lint_repo(args.repo, args.files)
    except Exception as e:  # a lint that cannot run must not wave through
        print(f"readme_lint: CANNOT RUN: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"json -> {args.json_out}")
    print(f"files: {len(report['files'])}   version: "
          f"{report['version_expected']}   badges: {report['badges_checked']}   "
          f"commands: {report['commands_checked']}   "
          f"skipped: {sum(report['skipped'].values())}   "
          f"findings: {len(report['findings'])}")
    for f in report["findings"]:
        print(f"[{f['kind']}] {f['file']}:{f['line']} :: {f['quote']!r} :: "
              f"{f['detail']}")
    return 1 if (args.strict and report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
