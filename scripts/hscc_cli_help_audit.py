#!/usr/bin/env python3
"""CLI help-surface audit for the unified ``hscc`` CLI (card t_7b0f65c8).

Walks EVERY command and subcommand on the surface — code-derived, not
hardcoded — runs both ``hscc <cmd> --help`` and ``hscc help <cmd>`` at 80, 120
and 500 columns, and checks the operator's four invariants plus a style log:

  (A) no standalone ``flightdeck`` string in RENDERED help/output text.
      Boundary: package paths / internal symbols / ``--flightdeck-home``-style
      flags in SOURCE are internal names, not user-facing — we assert on the
      rendered stdout/stderr only, never on source files.
      IDENTITY (t_cfdcefa9): one LOGICAL help line, not one physical rendered
      line — (A) is derived from the widest render only and the detail is
      whitespace-canonicalised. argparse widened its help column by 2 chars in
      CPython 3.13, so a raw rendered line is not a stable identity: the same
      logical line wrapped at width 80 on 3.13 and unwrapped on 3.11, which
      counted one violation twice on one interpreter and once on the other.
      Narrow renders are kept only as a GUARD (see ``a_width_fragments``): a
      narrow canonical line that is neither in the wide render nor a strict
      prefix of one means the help text is width-aware in CONTENT, which is
      itself a defect and is emitted as an ``A-WIDTH`` finding.
  (B) no line wraps without continuation indent, at BOTH 80 and 120 columns
      (a run of >=2 full-width lines: lines after the first must be indented;
      lines LONGER than the width that were not wrapped are "overflow").
  (C) every ``example:``/Examples line PARSES — parse-only, against the real
      in-process parser (flightdeck) or the code-derived command tables (hscc
      daemon groups). An example is NEVER executed.
  (D) ``--help`` executes NOTHING. Proven empirically per invocation with
      four sensors: sandboxed-HOME write diff, ``sparkrun``/``hermes`` PATH
      canaries, LaunchAgent existence, api-port tripwire.
  (E) the help STYLE of every surface path (rich / argparse / print), so the
      fix cards can converge on one.

Safety model (the operator was burned by a "green" test that RAN a destructive
command; this module must never execute a mutating verb):

  * only ``--help``/``help`` argv is executed, and only for names the
    enumeration proves are real registered commands;
  * every probe runs with HOME at a throwaway sandbox, so every ``~/.hscc``,
    ``~/.flightdeck`` and ``~/Library/LaunchAgents`` path resolves under the
    sandbox; a NEW file appearing in the sandbox during a help probe is a
    side effect and a FINDING, not a normal write;
  * PATH is prefixed with a directory holding recording tripwire ``sparkrun``
    and ``hermes`` binaries (exit 97, log their argv). PATH resolution finds
    the canary first, so the real CLIs are never reached; a canary trip during
    a ``--help`` probe is a (D) finding;
  * ``hscc api`` verb ``--help`` probes (start included) run ONLY because
    t_bf9f1909 made ``cmd_api`` intercept ``--help`` before dispatch — they
    print the verb help and fork/bind/write nothing. The port tripwire below
    stays armed as a regression guard: if that intercept ever regresses, the
    probe fails here on (D) sensors (port flip / new files / hang).

Usage:
  python3 scripts/hscc_cli_help_audit.py                 # audit repo root
  python3 scripts/hscc_cli_help_audit.py --json out.json # machine findings
  python3 scripts/hscc_cli_help_audit.py --strict        # exit 1 on findings

The pytest gate that executes in scripts/run_tests.sh lives in
scripts/tests/test_cli_help_surface.py and consumes :func:`audit`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

WIDTHS_WRAP = (80, 120)   # operator-mandated wrap widths for (B)
WIDTH_WIDE = 500          # "no-wrap" width: (A)/(C) read logical lines intact
WIDTHS_ALL = WIDTHS_WRAP + (WIDTH_WIDE,)

# The probe interpreter is whichever python runs pytest, so the RENDERED help
# is that interpreter's argparse output. (B) legitimately measures it; (A)
# normalises it away instead of encoding a CPython minor-version quirk into the
# expectations file (t_cfdcefa9 / GATE-313 — the gate was red on 3.13 only).
PY = sys.executable

API_DEFAULT_PORT = 8787   # hscc-api/api_server.py DEFAULT_PORT (tripwire)
API_SMOKE_PORT = 8788     # scripts/api_smoke.py default (defensive second hold)
API_PLIST_NAME = "com.hscc.api.plist"

# Launch-class verbs: a REGRESSED help intercept on these would fork a
# daemon (start / start-daemon), talk to launchd (stop / install /
# uninstall) or hang a live tail (watch). Their `--help` probes run (the
# intercepts are proven inert — zero canary trips, F5/BROKEN-HEAL pins) and
# stay. Their short `-h` half is NOT probed here: the exact argv that makes
# them dangerous is one missed intercept away from launching the loop or
# re-writing the LaunchAgent under the operator's real HOME, and a hang here
# costs 3x60 s per width. They are pinned IN-PROCESS instead by
# landmine-raise tests (hscc_daemon/tests/test_cli_h_shortflag.py: patch
# cli.cmd_start_daemon AND daemon_ops.run_daemon_loop to RAISE, then drive
# main() — a -h that reaches dispatch fails there with zero real side
# effects). The exemption is never silent: every exempted -h probe is
# recorded in report["skips"] with kind "h-exempt", and the gate pins the
# exempted set to this constant exactly (test_cli_help_surface.py).
LAUNCH_PROBE_EXEMPT = frozenset({
    ("start",), ("stop",), ("install",), ("uninstall",),
    ("start-daemon",), ("watch",),
})


def _h_probe_exempt(argv) -> bool:
    """True when this surface's argv is a launch-class verb (no -h probe)."""
    return tuple(argv[:1]) in LAUNCH_PROBE_EXEMPT


def invocations_for(argv, kind):
    """The probe invocations for one surface path: [(argv, flag), ...].

    t_5132deb3 (the -h gap F5 recorded in docs/audits/NOTES_t_fc307a51.md):
    the oracle probed `--help` only, so every `-h` path on the CLI was a
    blind spot — and `start-daemon -h` LAUNCHED THE LOOP while `--help` was
    inert. Every flag/flag2 surface now probes BOTH forms, except the
    launch-class set above. The `hscc help <cmd>` paths print from
    COMMAND_HELP and take no flag of their own, so they stay single.

    Single source of truth: the audit walk, the (A)-only sibling pass AND
    the F5 surface-accounting test all consume this function.
    """
    if kind == "help":
        return [([ "help", *argv], None)]
    out: list = [(list(argv) + ["--help"], "--help")]
    if kind == "flag" and argv:
        out.append((["help", argv[0]], None))
    if not _h_probe_exempt(argv):
        out.append((list(argv) + ["-h"], "-h"))
    return out

sys.path.insert(0, str(REPO))
PROJECT_DIR = REPO / "hscc-project"
sys.path.insert(0, str(PROJECT_DIR))

import hscc_daemon.hscc as daemon_hscc  # noqa: E402  (module import is side-effect free)
import flightdeck.cli as fd_cli          # noqa: E402

from hscc_daemon import api_cli, autodown_cli  # noqa: E402


# ── Surface enumeration (CODE-DERIVED) ───────────────────────────────────

def flightdeck_commands():
    """{cmd: {subcmd: subparser}} exactly as flightdeck's real parser registers."""
    parser = fd_cli.build_parser()
    top = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sp in action.choices.items():
                subs = {}
                for a2 in sp._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        subs = dict(a2.choices)
                top[name] = subs
    return top


def build_surface():
    """Return (paths, fd_cmds). Each path: (group, argv, style, kind).

    kind: "flag"  -> probe ``<argv> --help`` (+ ``hscc help <argv[0]>``)
          "flag2" -> probe ``<argv> --help`` only (subcommand level)
          "help"  -> probe ``hscc help <cmd>`` only
    style is the (E) record: rich | argparse | print.
    """
    fd_cmds = flightdeck_commands()
    paths = []

    def add(group, argv, style, kind):
        paths.append((group, list(argv), style, kind))

    # ── top level (Rich grouped help + COMMAND_HELP print text)
    add("top-level", [], "rich", "flag")
    for c in sorted(daemon_hscc.COMMAND_HELP):
        add("top-level", [c], "print", "help")
    for c in sorted(daemon_hscc.DAEMON_COMMANDS):
        add("top-level", [c], "print", "flag")
    for c in ("advanced", "start-daemon", "heal-clear", "heal-status"):
        add("top-level", [c], "print", "flag")

    # ── cluster group (plain print at group level; monitor --live included)
    add("cluster", ["cluster"], "print", "flag")
    for sub in sorted(daemon_hscc.CLUSTER_SUBCOMMANDS):
        add("cluster", ["cluster", sub], "print", "flag2")
    add("cluster", ["cluster", "monitor", "--live"], "print", "flag2")
    add("cluster", ["profiles"], "print", "flag")

    # ── template group
    add("template", ["template"], "print", "flag")
    for sub in sorted(daemon_hscc.TEMPLATE_SUBCOMMANDS):
        add("template", ["template", sub], "print", "flag2")

    # ── api group (Rich console prints of HELP_TEXT / panels)
    add("api", ["api"], "rich", "flag")
    for sub in api_cli.VALID_SUBCOMMANDS:
        add("api", ["api", sub], "rich", "flag2")

    # ── autodown group (bare print HELP_TEXT)
    add("api-autodown-kanban", ["autodown"], "print", "flag")
    for sub in autodown_cli.VALID_SUBCOMMANDS:
        add("api-autodown-kanban", ["autodown", sub], "print", "flag2")

    # ── kanban group (Rich console prints)
    add("api-autodown-kanban", ["kanban"], "rich", "flag")
    for sub in ("blocked", "stale"):
        add("api-autodown-kanban", ["kanban", sub], "rich", "flag2")

    # ── project group — flightdeck argparse surface
    add("project", ["project"], "argparse", "flag")
    for cmd, subs in sorted(fd_cmds.items()):
        add("project", ["project", cmd], "argparse", "flag2")
        for sub in sorted(subs):
            add("project", ["project", cmd, sub], "argparse", "flag2")
    for v in sorted(daemon_hscc._PROJECT_SUBGROUP_VERBS):  # aliased verbs
        add("project", ["project", v], "argparse", "flag2")

    # de-duplicate (group, argv, kind)
    seen, uniq = set(), []
    for g, argv, style, kind in paths:
        key = (g, tuple(argv), kind)
        if key not in seen:
            seen.add(key)
            uniq.append((g, argv, style, kind))
    return uniq, fd_cmds


# ── Sensors / probing ────────────────────────────────────────────────────

def make_canary_dir(root: Path) -> Path:
    """A PATH-prefix dir with recording tripwire sparkrun + hermes binaries."""
    canary = root / "canary-bin"
    canary.mkdir(parents=True, exist_ok=True)
    for name in ("sparkrun", "hermes", "gh", "git"):
        p = canary / name
        p.write_text(
            "#!/bin/sh\n"
            'echo "' + name + ' $*" >> "$AUDIT_CANARY_LOG"\n'
            "exit 97\n")
        p.chmod(0o755)
    (canary / "trips.log").write_text("")
    os.environ["AUDIT_CANARY_LOG"] = str(canary / "trips.log")
    return canary


class Prober:
    """Executes help invocations under a sandbox HOME; snapshots side effects."""

    def __init__(self, sandbox: Path, canary: Path, real_home: Path):
        self.sandbox = sandbox
        self.canary = canary
        self.real_home = real_home
        self._cache = {}
        self.port_busy_at_start = self._port_busy()
        # Defensive hold: bind (WITHOUT listen) the api ports for the whole
        # audit. A `--help` probe must never fork a server (t_bf9f1909 made
        # cmd_api intercept --help before dispatch); if that intercept ever
        # regresses, a stray grandchild here dies on EADDRINUSE instead of
        # staying bound to the operator's machine. No listen() means
        # connect_ex still reports "refused", so the port_flip sensor below
        # keeps its exact semantics (a regression is then detected via
        # new_files/hang findings instead). A port already in use (operator's
        # live API, or a concurrent audit) degrades to holding nothing.
        self._tripwire_socks = []
        for port in (API_DEFAULT_PORT, API_SMOKE_PORT):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                s.close()
            else:
                self._tripwire_socks.append(s)

    def _env(self, width: int) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CONDA_")
               and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                             "HERMES_DELEGATED_CHILD_CONTEXT",
                             "HSCC_SPARKRUN_BIN", "COLUMNS")}
        env.update({
            "HOME": str(self.sandbox),
            "PYTHONPATH": os.pathsep.join([str(REPO), str(PROJECT_DIR)]),
            "COLUMNS": str(width),
            "NO_COLOR": "1",
            "TERM": "dumb",
            "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(self.canary), env.get("PATH", "")]),
        })
        return env

    def _files(self):
        return set(str(p.relative_to(self.sandbox))
                   for p in self.sandbox.rglob("*"))

    def _trips(self):
        return (self.canary / "trips.log").read_text().splitlines()

    def _port_busy(self):
        with socket.socket() as s:
            s.settimeout(0.25)
            return s.connect_ex(("127.0.0.1", API_DEFAULT_PORT)) == 0

    def _plist(self):
        return (self.real_home / "Library" / "LaunchAgents"
                / API_PLIST_NAME).exists()

    def run(self, argv: list, width: int) -> dict:
        key = (tuple(argv), width)
        if key in self._cache:
            return self._cache[key]
        files_before = self._files()
        trips_before = len(self._trips())
        plist_before = self._plist()
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [PY, "-m", "hscc_daemon.hscc", *argv],
                env=self._env(width), cwd=str(self.sandbox),
                capture_output=True, text=True, timeout=60)
            rc, out, err, hung = proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired:
            rc, out, err, hung = None, "", "", True
        result = {
            "argv": list(argv), "width": width, "rc": rc, "hung": hung,
            "out": out, "err": err, "secs": round(time.monotonic() - t0, 3),
            "new_files": sorted(self._files() - files_before),
            "trips": self._trips()[trips_before:],
            "plist_created": (not plist_before) and self._plist(),
            "port_flip": (not self.port_busy_at_start) and self._port_busy(),
        }
        self._cache[key] = result
        return result


# ── Checks ───────────────────────────────────────────────────────────────

FLIGHTDECK_RE = re.compile(r"flightdeck")
_BOX = "┏┡└┌╞╟├┃│╭╰╮╯═╒╕╘╛╓╖╙╜┌┐╔╗╚╟╠│"
_BOX_ART = re.compile(r"^[\s─│┌┐└┘├┤┬┴┼╭╮╰╯━┃┏┓┗┛┣┫╋╇╥╩═╒╕╘╛╔╗╚╝╞╟╠╡]+$")

# daemon-log-shaped line: "[2026-...T...+00:00] [ WARN] …". Its presence in
# help output is EXECUTION evidence (a check/interlock ran) — it is a (D)
# finding and must not pollute the (B) wrap analysis of real help text.
LOG_LINE = re.compile(r"^\[\d{4}-\d\d-\d\dT[^\]]+\]\s*\[\s*(INFO|WARN|ERROR|DEBUG)\]")

# Commands advertised in help that no longer DO anything: the Telegram-era
# message verbs ("Telegram removed — no live source / no delivery") and the
# ed-* placeholder trio ("placeholder, not yet available"). The fix cards
# (F1 for message, F5 for ed-*) must hide or remove them; until then every
# such rendered line is a DEAD finding so the gate keeps the inventory honest.
DEAD_RE = re.compile(r"Telegram removed|not yet available|no delivery mechanism",
                     re.I)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and (s[0] in _BOX or bool(_BOX_ART.match(s)))


def canonical_line(ln: str) -> str:
    """A help line's IDENTITY: whitespace-collapsed and stripped.

    CPython 3.13's argparse widened its help column by 2 characters, so the
    padding between a subcommand name and its description is interpreter
    version dependent. Canonicalising absorbs that drift, which keeps (A)
    details — and the `needle`/`example` fields of cli_help_expected.json that
    match against them — stable across argparse versions.
    """
    return re.sub(r"\s+", " ", ln).strip()


def check_flightdeck(text: str):
    """(A) every rendered line containing 'flightdeck' (raw, legacy form).

    NOT how (A) findings are built any more — see :func:`a_findings_for_surface`
    for the logical-line form the gate consumes. Kept because it is the raw
    "which physical lines mention flightdeck" primitive the wide/narrow
    comparison is built from.
    """
    return [ln.strip()[:200] for ln in text.splitlines()
            if FLIGHTDECK_RE.search(ln)]


def a_findings_for_surface(renders: dict) -> list:
    """(A) findings for one help surface: one entry per LOGICAL help line.

    ``renders`` maps width -> probe result. Two rules make the result
    interpreter-independent (t_cfdcefa9 / GATE-313):

    1. only the WIDEST render available is authoritative (``WIDTH_WIDE`` for
       every surface that has one; the 120 render for the ``hscc help <cmd>``
       paths, which are probed at the wrap widths only). At a no-wrap width one
       logical help line is one physical line. A line read at 80/120 is a wrap
       fragment of a wide line (see rule 2), so unioning the narrow renders in
       only re-counts a violation — that is exactly what made the gate count 15
       findings on 3.13 and 14 on 3.11 for the same 14 logical lines;
    2. a narrow render may only ever ADD fragments, never content. Any
       canonical narrow line that is not contained in some authoritative line
       means the help text changes with terminal WIDTH — a defect in its own
       right — so it is reported as ``A-WIDTH`` instead of being dropped
       silently. Containment, not prefix: a wrapped continuation fragment
       ("… flightdeck …" landing on the second physical line) is a substring
       of its wide line but not a prefix of it. The assumption in rule 1 is
       checked, not trusted.

    Returns [{"kind": "A"|"A-WIDTH", "detail": str, "width": int}] with
    details truncated to 200 chars (the committed contract length); the guard
    comparison uses the untruncated canonical lines so a truncated wide line
    cannot fake a content change.
    """
    def lines_at(width, truncate_after):
        r = renders.get(width)
        if not r:
            return []
        out = []
        for ln in (r["out"] + r["err"]).splitlines():
            if not FLIGHTDECK_RE.search(ln):
                continue
            c = canonical_line(ln)
            if truncate_after:
                c = c[:truncate_after]
            if c and c not in out:
                out.append(c)
        return out

    top = max(renders) if renders else WIDTH_WIDE
    wide_full = lines_at(top, truncate_after=None)
    wide = []
    for ln in wide_full:
        ln = ln[:200]
        if ln and ln not in wide:
            wide.append(ln)
    findings = [{"kind": "A", "detail": ln, "width": top} for ln in wide]
    for w in sorted(set(renders) - {top}):
        for ln in lines_at(w, truncate_after=None):
            if any(ln in wl for wl in wide_full):
                continue  # a fragment (head or continuation) — counted once
            findings.append({"kind": "A-WIDTH", "detail": ln[:200], "width": w})
    return findings


def a_count_table(findings: list) -> dict:
    """{surface command: sorted canonical (A) detail lines} from raw findings.

    The shape the gate pins interpreter-independence on (the width is left out
    on purpose: width-dependent (A) detail is what we are guarding against).
    """
    table = {}
    for f in findings:
        if f["kind"] != "A":
            continue
        table.setdefault(f["command"], []).append(f["detail"])
    return {k: sorted(v) for k, v in sorted(table.items())}


def check_wraps(text: str, width: int):
    """(B) bare wraps + overflow, ignoring Rich table border lines.

    Two wrap signatures are caught:
      * run: Rich/argparse emit consecutive physical lines of exactly
        ``width`` columns — every line after the first must be indented
        (an item's wrapped continuation must align to its item column);
      * spill: Rich leaves the wrap-space at EOL and drops the continuation
        at column 0 (e.g. the '  check [stream]' line whose 'streams: …'
        tail spills with no indent) — a space-terminated line followed by a
        col-0 lowercase/'(' line is a bare wrap;
      * overflow: a line LONGER than the width was emitted unwrapped.
    Box-drawing table lines are excluded: Rich owns cell wrapping and border
    lines are legitimately full-width by design.
    """
    offenders = []
    run = []
    lines = text.rstrip("\n").split("\n")
    for i, raw in enumerate(lines):
        ln = raw.rstrip()
        if _is_table_line(ln) or not ln.strip() or LOG_LINE.match(ln):
            if len(run) >= 2:
                offenders.extend(("bare-wrap", c[:240]) for c in run[1:]
                                 if not c[:1].isspace())
            run = []
            continue
        if len(ln) > width:
            offenders.append(("overflow", ln[:240]))
            continue
        if len(ln) == width:
            run.append(ln)
        else:
            if len(run) >= 2:
                offenders.extend(("bare-wrap", c[:240]) for c in run[1:]
                                 if not c[:1].isspace())
            run = []
        # spill signature: Rich keeps the wrap-space at EOL and drops the
        # continuation at column 0. Require the line to be near-full width
        # (it actually wrapped) and the next line to be a short lowercase /
        # '(' continuation — never a section header (options:, usage:, …).
        content = raw.rstrip()
        if (raw.endswith(" ") and content
                and len(content) >= width - 20):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            head_ok = (nxt[:1] == "("
                       or (nxt[:1].isalpha() and nxt[:1].islower()))
            not_header = not nxt.strip().lower().startswith(
                ("options:", "usage:", "positional arguments:",
                 "optional arguments:", "subcommands:", "valid "))
            if head_ok and not_header and not _is_table_line(nxt):
                offenders.append(("bare-wrap", nxt[:240]))
    if len(run) >= 2:
        offenders.extend(("bare-wrap", c[:240]) for c in run[1:]
                         if not c[:1].isspace())
    # de-dupe per (how, line)
    uniq, seen = [], set()
    for o in offenders:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq


_EX_HEAD = re.compile(r"^\s*(?:examples?|usage examples)\s*:?\s*(.*)$", re.I)
_CMD_RE = re.compile(r"(?:hscc|flightdeck)\s")


def _split_command_starts(chunk: str):
    """Segment a chunk at each quote-outside command occurrence.

    An occurrence inside quotes (or backticks) is prose, not a new example:
    e.g. ``flightdeck incident "…" --fix "re-ran flightdeck project repair"``
    is ONE example whose second occurrence sits inside the --fix argument.
    """
    starts, in_quote, quote_ch, i, n = [], "", 0, 0, len(chunk)
    while i < n:
        c = chunk[i]
        if in_quote:
            if c == in_quote:
                in_quote = ""
            i += 1
            continue
        if c in "\"'`":
            in_quote, i = c, i + 1
            continue
        m = _CMD_RE.match(chunk, i)
        if m:
            starts.append(i)
            i = m.end()
            continue
        i += 1
    return starts


def _clean_segment(seg: str):
    """One candidate command string from a segment, cleaned; None if empty."""
    seg = seg.split("#", 1)[0].strip().rstrip(";,.")
    # a description tail: two+ spaces before prose that is not a flag
    # (maxsplit is keyword: passing it positionally is a DeprecationWarning on
    # 3.13 and an error in a future CPython)
    seg = re.split(r"\s{2,}(?!--)", seg, maxsplit=1)[0]
    # an inline note: "cmd (does x)" keeps the command
    seg = re.split(r"\s\((?![^)]*--)", seg, maxsplit=1)[0]
    seg = seg.strip().rstrip(";,")
    if not seg or "`" in seg:
        return None
    if seg.count('"') % 2 or seg.count("'") % 2:
        toks = seg.split()
        while toks and (toks[-1].count('"') % 2 or toks[-1].count("'") % 2):
            toks.pop()
        seg = " ".join(toks)
    return seg or None


def _looks_like_example_line(ln: str) -> bool:
    """A region line is an example when the command starts the line (after
    indent/quotes/bullets) or sits after a wide gap (two-column help). A
    prose line mentioning a command mid-sentence (``Try `flightdeck …``` ``)
    is not an example."""
    stripped = ln.lstrip()
    if re.match(r"^[\"'`-]*\s*(hscc|flightdeck)\s", stripped):
        return True
    return bool(re.search(r"\s{2,}`?(hscc|flightdeck)\s", ln))


def _chunk_candidates(chunk: str, try_parse):
    """Example commands from one region line/chunk.

    Splitting at each `flightdeck `/`hscc ` occurrence is ambiguous: the
    word is ALSO a positional project-name argument
    (``flightdeck release flightdeck 0.6.0`` is ONE example). The oracle is
    the parser itself — take the longest span that parses; only when no
    longer span parses do we split at the next occurrence. ``try_parse``
    returns None on success (mirrors ExampleParser.parse); None disables the
    oracle (emit one candidate per occurrence).
    """
    starts = _split_command_starts(chunk)
    if not starts:
        return []
    bounds = starts + [len(chunk)]
    out = []
    i = 0
    while i < len(starts):
        emitted = False
        if try_parse is not None:
            for j in range(len(starts), i, -1):
                span = chunk[bounds[i]:bounds[j]]
                parts = [_clean_segment(p) for p in span.split("|")]
                if all(p and p.startswith(("hscc", "flightdeck"))
                       and try_parse(p) is None for p in parts):
                    out.extend(parts)
                    i = j
                    emitted = True
                    break
        if not emitted:
            # no longer span parses: emit the shortest candidate so a real
            # parse failure still surfaces as a (C) finding
            span = chunk[bounds[i]:bounds[i + 1]]
            out.extend(p for p in (_clean_segment(x) for x in span.split("|"))
                       if p and p.startswith(("hscc", "flightdeck")))
            i += 1
    return out


def extract_examples(text: str, try_parse=None):
    """Example COMMAND strings from rendered help (the parse targets).

    Collects `example: …` single-liners and `Examples:`/`examples:` blocks.
    Within a region, each outside-quotes occurrence of `hscc `/`flightdeck `
    may start a new candidate (handles two-column layouts and `a | b`
    pairs); ``try_parse`` resolves the ambiguity (see _chunk_candidates).
    Segments are cleaned of trailing prose notes and comments.
    """
    lines = text.splitlines()
    region, in_region = [], False
    for ln in lines:
        m = _EX_HEAD.match(ln)
        if m:
            rest = m.group(1).strip()
            if rest and _looks_like_example_line(rest):
                region.append(rest)
            in_region = True
            continue
        if not in_region:
            continue
        if not ln.strip():
            in_region = False
            continue
        s = ln.strip().lower()
        if s.startswith(("options:", "positional arguments:", "usage:",
                         "optional arguments:")):
            in_region = False
            continue
        if _looks_like_example_line(ln):
            region.append(ln)
    out = []
    for chunk in region:
        out.extend(_chunk_candidates(chunk, try_parse))
    return out


class ExampleParser:
    """(C) parse-only validation. NEVER executes anything.

    flightdeck examples parse through flightdeck's REAL argparse parser
    (``flightdeck <cmd>`` == the documented ``hscc project <cmd>`` alias); hscc
    daemon verbs are checked against the code-derived command/subcommand
    tables (they are hand-rolled dispatchers, not argparse).

    For the argparse (project) surface, examples are read from the RAW
    ``epilog`` strings on the live parser objects, not from rendered help:
    argparse re-wraps epilogs at terminal width and collapses the alignment
    padding, which would turn every ``flightdeck standup   <description>``
    two-column example into a bogus parse failure. The raw epilog is the
    author's exact example text.
    """

    def __init__(self, fd_cmds: dict):
        self.parser = fd_cli.build_parser()
        self.daemon_cmds = (set(daemon_hscc.COMMAND_HELP)
                            | set(daemon_hscc.DAEMON_COMMANDS)
                            | {"heal-clear", "heal-status", "start-daemon",
                               "advanced"})
        # `hscc project <verb>` promotion (hscc_daemon hscc._handle_project):
        # subgroup verbs are accepted bare at the top. The oracle models the
        # SHIPPED dispatcher, not just flightdeck's raw parser — examples are
        # documented in the short form, and `hscc project chat` is real.
        self.project_alias = set(daemon_hscc._PROJECT_SUBGROUP_VERBS)
        try:
            self.fd_top_level = {
                name
                for action in (self.parser._subparsers._group_actions
                               if self.parser._subparsers else [])
                for name in (getattr(action, "choices", {}) or {})}
        except Exception:
            self.fd_top_level = set()
        self.subs = {
            "cluster": daemon_hscc.CLUSTER_SUBCOMMANDS,
            "template": daemon_hscc.TEMPLATE_SUBCOMMANDS,
            "api": set(api_cli.VALID_SUBCOMMANDS),
            "autodown": set(autodown_cli.VALID_SUBCOMMANDS),
            "kanban": {"blocked", "stale"},
        }
        # (cmd,) / (cmd, sub) -> raw epilog, from the live parser tree
        self.epilogs = {}
        for action in self.parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for cmd, sp in action.choices.items():
                    self.epilogs[(cmd,)] = sp.epilog or ""
                    for a2 in sp._actions:
                        if isinstance(a2, argparse._SubParsersAction):
                            for sub, sp2 in a2.choices.items():
                                self.epilogs[(cmd, sub)] = sp2.epilog or ""
        # top-level flightdeck parser epilog (shown by `hscc project --help`)
        self.top_epilog = self.parser.epilog or ""

    def raw_epilog_for(self, argv):
        """Raw epilog example text for an argparse surface path, or None.

        argv looks like [] (bare `project` path -> top epilog), [cmd] or
        [cmd, sub]; the 'project' prefix is stripped by the caller.
        """
        if not argv:
            return self.top_epilog
        if tuple(argv) in self.epilogs:
            return self.epilogs[tuple(argv)]
        # aliased subgroup verb: `hscc project chat` == flightdeck
        # `project chat`, whose epilog is keyed ("project", "chat").
        return self.epilogs.get(("project", *argv))

    def parse(self, cmd: str):
        """None when it parses; a human-readable reason when it does not."""
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return f"unlexable: {e}"
        argv = [a for a in argv if not (a.startswith("[") and a.endswith("]"))]
        if not argv:
            return None
        if argv[0] == "flightdeck":
            return self._parse_project(argv[1:])
        if argv[0] == "hscc":
            argv = argv[1:]
        if not argv:
            return None
        head = argv[0]
        if head == "project":
            return self._parse_project(argv[1:])
        if head in self.subs:
            rest = [a for a in argv[1:] if not a.startswith("-")]
            if rest and rest[0] not in self.subs[head]:
                return f"'hscc {head}' has no subcommand {rest[0]!r}"
            return None
        if head in self.daemon_cmds or head == "help":
            return None
        return f"unknown command {head!r}"

    def _parse_project(self, rest):
        # Model the hscc dispatcher's promotion (hscc_daemon
        # hscc._handle_project): a subgroup verb typed bare at the top of
        # `hscc project` is rewritten to the subgroup, unless a top-level
        # command shadows it (the runtime guard's rule — the top level wins).
        # Without this the oracle would reject the documented short form
        # (`hscc project chat hscc`) that F1's examples now use.
        if rest and rest[0] in self.project_alias and rest[0] not in self.fd_top_level:
            rest = ["project", *rest]
        argv = ["audit-placeholder" if re.match(r"^<.+>$", a) else a
                for a in rest]
        # --help/-h: the D probes cover help execution separately; here we
        # validate only the command/subcommand/flag shape.
        argv = [a for a in argv if a not in ("-h", "--help")]
        try:
            # argparse exits via SystemExit on rejection and prints to
            # stdout/stderr; capture both. Parse-only: nothing runs.
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.parser.parse_args(argv)
            return None
        except SystemExit as e:
            if e.code == 0:
                return None
            return f"argparse rejected {' '.join(argv)!r} (exit {e.code})"
        except Exception as e:
            return f"parser crashed: {type(e).__name__}: {e}"


# ── Driver ───────────────────────────────────────────────────────────────

# Pre-seeded sandbox cluster.json. serving.py USED to resolve cluster config
# AT IMPORT and shell `sparkrun cluster list --json` when ~/.hscc/cluster.json
# was absent (audit B11 — FIXED by t_8d2eb400: topology resolution is lazy,
# first READ not import). The seed stays as defence-in-depth for any future
# import-time reader, and the systemic probe below now measures ZERO trips;
# it is the regression detector — a reintroduced import-time shell-out comes
# back as a NEW finding (mutation-proven both directions).
# Address guard: 10.0.0.x is the documented LAN placeholder, never a real host.
_SEED_CLUSTER_JSON = json.dumps({
    "gateway": {"host": "10.0.0.1"},
    "workers": [],
    "nasDevices": [],
})


def _seed_sandbox(sandbox: Path):
    (sandbox / ".hscc").mkdir(parents=True, exist_ok=True)
    (sandbox / ".hscc" / "cluster.json").write_text(_SEED_CLUSTER_JSON)


def _systemic_import_probe(sandbox_parent: Path, canary: Path) -> str | None:
    """Evidence for the import-time sparkrun fallback; None when clean."""
    home = sandbox_parent / "empty-home"
    home.mkdir(parents=True, exist_ok=True)
    (canary / "trips.log").write_text("")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CONDA_")
           and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                         "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN")}
    env.update({"HOME": str(home),
                "PYTHONPATH": os.pathsep.join([str(REPO), str(PROJECT_DIR)]),
                "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb",
                "HSCC_THEME": "dark",
                "PATH": os.pathsep.join([str(canary), env.get("PATH", "")])})
    subprocess.run([PY, "-m", "hscc_daemon.hscc", "--help"], env=env,
                   cwd=str(home), capture_output=True, text=True, timeout=60)
    trips = [t for t in (canary / "trips.log").read_text().splitlines() if t]
    shutil.rmtree(home, ignore_errors=True)
    return "; ".join(trips[:3]) if trips else None


def _norm(argv):
    return "hscc " + " ".join(argv)


def audit(repo=None) -> dict:
    """Run the full surface audit; returns the findings report (see module doc)."""
    global REPO
    if repo:
        REPO = Path(repo).resolve()
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        globals()["PROJECT_DIR"] = REPO / "hscc-project"
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))
    tmp = Path(tempfile.mkdtemp(prefix="hscc-cli-help-audit-"))
    sandbox = tmp / "home"
    sandbox.mkdir(parents=True, exist_ok=True)
    canary = make_canary_dir(tmp)
    _seed_sandbox(sandbox)
    prober = Prober(sandbox, canary, Path(os.path.expanduser("~")))
    paths, fd_cmds = build_surface()
    ex = ExampleParser(fd_cmds)

    findings = []
    seen_findings = set()
    styles = {}
    skips = []
    probes = 0
    examples_total = 0
    examples_failed = 0
    seen_examples = set()

    def add(group, command, kind, detail, fix):
        # Normalise probe-environment noise so findings are deterministic and
        # never leak the runner's private temp paths: sandbox root -> <SANDBOX>,
        # daemon-log timestamps -> <TS>.
        detail = detail.replace(str(tmp), "<SANDBOX>")
        detail = re.sub(r"\[\d{4}-\d\d-\d\dT[^\]]+\]", "[<TS>]", detail)
        key = (command, kind, detail)
        if key in seen_findings:
            return
        seen_findings.add(key)
        findings.append({"group": group, "command": command, "kind": kind,
                         "detail": detail, "fix": fix})

    # systemic regression probe: importing the CLI must execute NOTHING.
    # (B11 — the import-time resolve_cluster_config sparkrun fallback — was
    # FIXED by t_8d2eb400; this probe now measures zero and catches any
    # reintroduction as a NEW D finding.)
    trip = _systemic_import_probe(tmp, canary)
    if trip:
        add("top-level", "python -m hscc_daemon.hscc --help (empty HOME)", "D",
            "import of hscc_daemon.hscc executes sparkrun (module-level "
            f"resolve_cluster_config fallback): {trip}",
            "make cluster-config resolution lazy (first use, not import)")

    # Empirical exec-echo detector: a --help that prints a mutation-verb echo
    # (e.g. cmd_heal treating "--help" as a node name) executed the command.
    EXEC_ECHO = re.compile(
        r"Cleared heal strikes|healing resumes|No strikes recorded for --help")

    for group, argv, style, kind in paths:
        invs = invocations_for(argv, kind)
        if kind != "help" and _h_probe_exempt(argv):
            # The exemption must be visible, not silent (see the constant).
            skips.append({"surface": _norm(argv + ["-h"]),
                          "kind": "h-exempt",
                          "why": "launch-class verb; pinned in-process"})
        last_rc = {}          # flag -> rc, for the -h-vs---help parity below
        last_out = {}         # flag -> canonical (out+err)
        for inv, flag in invs:
            name = _norm(inv)
            styles.setdefault(name, style)
            # api verb --help probes run for real since t_bf9f1909: cmd_api
            # intercepts --help before dispatch, so they fork/bind nothing.
            # There is deliberately NO skip here any more — an api start
            # regression that forks a server is caught by the (D) sensors
            # (port flip, new HOME files, 60s hang), not by refusing to look.
            probes += 1
            widths = (WIDTHS_WRAP + (WIDTH_WIDE,)) if kind != "help" else WIDTHS_WRAP
            try:
                renders = {w: prober.run(inv, w) for w in widths}
            except Exception as e:  # probe machinery failure must not pass silently
                add(group, name, "D", f"probe error: {type(e).__name__}: {e}",
                    "fix the probe")
                continue
            # Parity bookkeeping for the (D-h) check below: the rc and the
            # canonical WIDEST render of this flag's invocation. (The widest
            # render is the same authority (A) uses — the only width where
            # one logical help line is one physical line, so a legitimately
            # wrapped render can never fake a divergence.)
            _auth = renders.get(WIDTH_WIDE) or renders[WIDTHS_WRAP[-1]]
            last_rc[flag] = _auth["rc"]
            last_out[flag] = canonical_line(_auth["out"] + _auth["err"])

            # (A) rendered text — one finding per LOGICAL line: the widest
            # render only + whitespace-canonicalised, plus the width-aware
            # content guard (see a_findings_for_surface). Reading every width
            # raw is what made the count interpreter-dependent (GATE-313).
            for af in a_findings_for_surface(renders):
                if af["kind"] == "A":
                    add(group, name, "A", af["detail"],
                        "rename standalone 'flightdeck' to 'hscc project'")
                else:
                    add(group, name, "A-WIDTH",
                        f"width={af['width']} renders {af['detail']!r}, which "
                        "is not contained in any line of the authoritative "
                        "render — help CONTENT changes with terminal width",
                        "make the help text width-independent in content "
                        "(the audit counts (A) on the widest render only)")

            for w, r in renders.items():
                if r["hung"]:
                    add(group, name, "D", "help invocation hung past 60s — "
                        "it is executing something", "intercept --help first")
                    continue
                # DEAD: help advertises a command that no longer does anything
                for ln in r["out"].splitlines():
                    if DEAD_RE.search(ln):
                        add(group, name, "DEAD", ln.strip()[:200],
                            "hide or remove the dead command from help "
                            "(documented decision per card)")
                # (D) daemon-log lines in help output = a check/interlock RAN
                for ln in r["out"].splitlines() + r["err"].splitlines():
                    if LOG_LINE.match(ln):
                        add(group, name, "D",
                            "help emitted a daemon log line (a check ran "
                            f"while printing help): {ln[:200]}",
                            "intercept --help before running checks")
                # (D) empirical execution echo (e.g. heal-clear --help)
                echo = EXEC_ECHO.search(r["out"] + r["err"])
                if echo:
                    add(group, name, "D",
                        f"--help executed the mutating verb "
                        f"(output echo: {echo.group(0)!r})",
                        "intercept --help before dispatching the verb")
                # (B) wraps — only at the mandated widths
                if w in WIDTHS_WRAP:
                    for how, ln in check_wraps(r["out"], w):
                        add(group, name, "B", f"width={w} {how}: {ln}",
                            "indent continuation lines / shorten help lines")
                # (D) sensors on every render
                nf = [f for f in r["new_files"] if "trips.log" not in f]
                if nf:
                    add(group, name, "D",
                        "--help wrote state under HOME: "
                        + ", ".join(nf[:6]),
                        "intercept --help before any state write")
                if r["trips"]:
                    add(group, name, "D",
                        "--help shelled out: "
                        + "; ".join(r["trips"][:3])[:200],
                        "intercept --help before shelling out to sparkrun/hermes")
                if r["plist_created"]:
                    add(group, name, "D",
                        "--help wrote ~/Library/LaunchAgents/" + API_PLIST_NAME,
                        "intercept --help before writing the LaunchAgent")
                if r["port_flip"]:
                    add(group, name, "D",
                        f"--help bound the api port {API_DEFAULT_PORT}",
                        "intercept --help before forking the server")
                if r["rc"] not in (0, None) and not r["hung"]:
                    add(group, name, "D-rc",
                        f"{flag or 'help'} exited {r['rc']} (expected 0)",
                        "help paths should exit 0")

            # (D-h) short-flag parity (t_5132deb3 — the CLI-wide -h gap F5
            # recorded in docs/audits/NOTES_t_fc307a51.md). Both forms are
            # probed (invocations_for); if `-h` did not print help where
            # `--help` does, its output/rc diverge from the help render —
            # most often because it EXECUTED the verb instead (escalate -h
            # scanned, start-daemon -h launched the loop). Only surfaces that
            # probed BOTH flags can be compared (help-kind paths and the
            # launch-class -h exemption are covered by in-process pins).
            if "--help" in last_rc and "-h" in last_rc:
                if (last_rc["-h"] != last_rc["--help"]
                        or last_out.get("-h") != last_out.get("--help")):
                    add(group, _norm(argv + ["-h"]), "D-h",
                        "-h does not print the same help as --help "
                        f"(rc {last_rc['-h']} vs {last_rc['--help']}; "
                        "output differs: "
                        f"{(last_out.get('-h') or '')[:120]!r} vs "
                        f"{(last_out.get('--help') or '')[:120]!r})",
                        "intercept -h exactly like --help (same COMMAND_HELP "
                        "text) before dispatch")

            # (C) examples — raw parser epilogs for the argparse surface
            # (rendered help re-wraps and corrupts two-column examples);
            # rendered text for everything else.
            raw = ex.raw_epilog_for(argv[1:]) if style == "argparse" else None
            wide_render = renders.get(WIDTH_WIDE) or renders[WIDTHS_WRAP[-1]]
            src = raw if raw is not None else (
                wide_render["out"] + wide_render["err"])
            for exs in extract_examples(src, try_parse=ex.parse):
                if exs in seen_examples:
                    continue
                seen_examples.add(exs)
                examples_total += 1
                err = ex.parse(exs)
                if err:
                    examples_failed += 1
                    add(group, name, "C",
                        f"example does not parse: {exs!r} — {err}",
                        "rewrite the example against the real parser")

    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "surface_paths": len(paths),
        "probes": probes,
        "findings": findings,
        "a_counts": a_count_table(findings),
        "skips": skips,
        "styles": styles,
        "examples_total": examples_total,
        "examples_failed": examples_failed,
        "widths": list(WIDTHS_ALL),
    }


def a_table(repo=None, surfaces=None) -> dict:
    """(A)-only pass: {surface command: sorted canonical (A) lines}.

    Walks the real surface but derives ONLY class (A) (no sensors, no
    examples), and can be restricted to a set of surface names, so a sibling
    interpreter can produce the table cheaply and the gate can assert it is
    identical across interpreters (t_cfdcefa9)."""
    global REPO
    if repo:
        REPO = Path(repo).resolve()
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        globals()["PROJECT_DIR"] = REPO / "hscc-project"
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))
    want = set(surfaces) if surfaces else None
    tmp = Path(tempfile.mkdtemp(prefix="hscc-cli-help-a-table-"))
    sandbox = tmp / "home"
    sandbox.mkdir(parents=True, exist_ok=True)
    canary = make_canary_dir(tmp)
    _seed_sandbox(sandbox)
    prober = Prober(sandbox, canary, Path(os.path.expanduser("~")))
    paths, _fd = build_surface()
    table: dict = {}
    for group, argv, style, kind in paths:
        for inv, flag in invocations_for(argv, kind):
            name = _norm(inv)
            if want and name not in want:
                continue
            if inv[:2] == ["api", "start"]:
                continue  # never executed (see audit docstring)
            widths = WIDTHS_ALL if kind != "help" else WIDTHS_WRAP
            try:
                renders = {w: prober.run(inv, w) for w in widths}
            except Exception:
                continue
            lines = table.setdefault(name, [])
            for af in a_findings_for_surface(renders):
                if af["kind"] == "A" and af["detail"] not in lines:
                    lines.append(af["detail"])
    shutil.rmtree(tmp, ignore_errors=True)
    # same semantics as a_count_table(): only surfaces that actually leak
    table = {k: sorted(v) for k, v in sorted(table.items()) if v}
    return table


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--a-table", dest="a_table", nargs="?", const="*",
                    default=None, metavar="SURFACES",
                    help="run the (A)-only pass and print the per-surface "
                         "canonical (A) table as JSON (no findings, no "
                         "sensors); SURFACES is an optional comma-separated "
                         "list of surface names to restrict the walk to — "
                         "used by the interpreter-independence regression "
                         "test")
    args = ap.parse_args()

    if args.a_table is not None:
        raw = args.a_table
        surfaces = [s.strip() for s in raw.split(",")
                    if s.strip() and s.strip() != "*"]
        print(json.dumps(a_table(args.repo, surfaces or None),
                         indent=2, sort_keys=True))
        return 0

    rep = audit(args.repo)
    by_kind = {}
    for f in rep["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)

    print(f"surface paths: {rep['surface_paths']}   probes: {rep['probes']}   "
          f"examples: {rep['examples_total']} ({rep['examples_failed']} fail)")
    for k in sorted(by_kind):
        print(f"  {k}: {len(by_kind[k])}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, indent=2))
        print(f"json -> {args.json_out}")
    else:
        for f in rep["findings"]:
            print(f"[{f['kind']}] {f['group']} :: {f['command']} :: {f['detail']}")
    return 1 if (args.strict and rep["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
