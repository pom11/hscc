#!/usr/bin/env python3
"""t_5db4cbba: enumerate every place this script's OWN writes can hit a closed pipe.

A self-inflicted SIGPIPE needs two things at once:
  (1) the writer is the SHELL (a builtin: echo/printf/`>` redirect the shell
      performs), because an external producer's write kills the producer, not the
      shell, and
  (2) the consumer on the far end closes before the writer is done.

The card's premise is that `run_tests.sh` is "a 967-line script that pipes through
head/tr" and that trapping SIGPIPE therefore changes error paths nobody enumerated.
This enumerates them instead. Every top-level `|` is split (descending into `$( )`
and `<()`, tracking quote state), and each pipe is reported with:

    writer     the command whose write goes into that pipe
    builtin    whether that writer is a shell builtin (=> a self-PIPE candidate)
    subshell   whether the pipe sits inside a command substitution (a dead writer
               there costs a value, not the pack)
    closer     whether the consumer can close early (`head`, `grep -q`, a
               command substitution that reads to EOF, …)

Usage: pipe_sites.py <script>
"""
import re
import sys

BUILTIN_WRITERS = {"echo", "printf", "read", ":", "true", "false", "export",
                   "local", "declare", "let", "set", "shift", "cd", "return",
                   "exit", "break", "continue", "test", "[", "[[", "source"}
EARLY_CLOSERS = {"head", "grep", "sed", "awk", "read"}


def strip_comment(line):
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == "\\" and q in "\"'":
                out.append(ch)
                continue
            if ch == q:
                q = None
            out.append(ch)
        elif ch in "\"'":
            q = ch
            out.append(ch)
        elif ch == "#" and (not line[:i] or line[i - 1] in " \t;(&|"):
            break
        else:
            out.append(ch)
    return "".join(out)


def split_pipes(text):
    """Split at every unquoted `|` that is not `||`, at ANY nesting depth."""
    parts, starts, buf, i, q, depth = [], [], [], 0, None, 0
    start = 0
    while i < len(text):
        ch = text[i]
        if q:
            if ch == "\\" and q == '"':
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == q:
                q = None
            buf.append(ch)
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "|" and depth >= 0:
            if text[i + 1:i + 2] == "|" or text[i - 1:i] == "|":
                buf.append(ch)
            else:
                parts.append("".join(buf))
                starts.append(start)
                buf, start = [], i + 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    starts.append(start)
    return list(zip(starts, parts))


def head_token(seg):
    s = seg.strip()
    s = re.sub(r"^[\w]+=[$(]['\"]?[^\s)]*", "", s)      # strip VAR=… prefixes
    m = re.match(r"^[\[\]\w./-]+", s)
    return m.group(0) if m else s[:12]


def inside_subst(text, offset):
    """Rough: is this byte position inside a $( … ) / `…` / <( … ) ?"""
    before = text[:offset]
    return (before.count("$(") + before.count("<(") + before.count("`")) > \
           (before.count(")") + before.count("`"))


def main(path):
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    rows = []
    for n, raw in enumerate(lines, 1):
        code = strip_comment(raw)
        if "|" not in code:
            continue
        # a `case` pattern list / a `||`-only line is not a pipe
        if re.match(r"^\s*(case|if|while|elif|\*)\b", code) or "|" not in code.replace("||", ""):
            continue
        segs = split_pipes(code)
        if len(segs) < 2:
            continue
        for idx in range(1, len(segs)):
            off, wseg = segs[idx - 1]
            _, cseg = segs[idx]
            w, c = head_token(wseg), head_token(cseg)
            wl = w.lstrip("$(").strip()
            rows.append(dict(line=n, writer=w, builtin=wl in BUILTIN_WRITERS,
                             consumer=c, early=c in EARLY_CLOSERS,
                             subst=inside_subst(code, off), src=raw.strip()))
    print(f"file={path}  real pipes found: {len(rows)}\n")
    print(f"{'line':>5}  {'self-PIPE?':>10}  {'early-close?':>12}  writer -> consumer")
    for r in rows:
        risk = "SELF" if (r["builtin"] and r["early"]) else ("builtin" if r["builtin"] else "-")
        print(f"{r['line']:>5}  {risk:>10}  {('yes' if r['early'] else 'no'):>12}  "
              f"{r['writer']} -> {r['consumer']}"
              f"{'   [in $( )]' if r['subst'] else ''}")
    self_pipe = [r for r in rows if r["builtin"] and r["early"]]
    print(f"\nWRITER-IS-A-BUILTIN and CONSUMER-CAN-CLOSE-EARLY: {len(self_pipe)}")
    for r in self_pipe:
        print(f"  {path}:{r['line']}  {r['src'][:110]}")
    builtin_only = [r for r in rows if r["builtin"] and not r["early"]]
    print(f"builtin writer but a read-to-EOF consumer (no early close available): "
          f"{len(builtin_only)}")
    for r in builtin_only:
        print(f"  {path}:{r['line']}  {r['writer']} -> {r['consumer']}")


if __name__ == "__main__":
    main(sys.argv[1])
