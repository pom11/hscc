#!/usr/bin/env python3
"""Keep the landing page's command list honest about the real CLI.

The landing page (``site/index.html``, published at the gh-pages root) lists
every ``hscc`` command group. A hand-maintained list drifts the moment the CLI
gains, renames or drops a command, and a stale list on a public page is a
claim that is no longer true.

So the list is not hand-maintained:

  * the **command list, its order and its grouping** come from the installed
    CLI — ``hscc --help`` (same parser as ``scripts/gen_cli_help_a_baseline.py``
    reads: group headings at column 0, entries indented two spaces);
  * the **prose** comes from ``site/COPY.md`` — the copy card is the source of
    truth for wording, and this script never invents an explanation. If the CLI
    grows a command nobody has written a line for, ``--write`` refuses and says
    which one; you write the line in ``site/COPY.md``, then regenerate.

The two are keyed off the ``<!-- COMMANDS:BEGIN -->`` / ``<!-- COMMANDS:END -->``
markers: the block in ``site/COPY.md`` (format contract, one bullet per
top-level command) and the generated region in ``site/index.html``.

Usage::

    python3 scripts/gen_site_commands.py              # check, no writes
    python3 scripts/gen_site_commands.py --write      # rewrite site/index.html
    python3 scripts/gen_site_commands.py --help-file f.txt   # offline help

Exit codes: 0 in agreement, 1 on drift (with a diff of what changed), 2 on a
usage / parse problem (missing markers, unreadable help).
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COPY_MD = REPO / "site" / "COPY.md"
INDEX_HTML = REPO / "site" / "index.html"

BEGIN = "<!-- COMMANDS:BEGIN"
END = "<!-- COMMANDS:END -->"

# ANSI colours (the CLI themes itself; a piped help is already plain).
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Column-0 lines that are prose, not a command-group heading.
NON_GROUPS = (
    "HSCC", "Turn ", "Usage:", "Examples", "Docs:", "Theme:", "Run ",
    "hscc ",
)

# A token that counts as part of a command path: `cluster`, `up`, `--`-less,
# no placeholders (`<name>`), no optionals (`[flags]`), no alternates (`a|b`).
WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def fetch_help(help_file: str | None) -> str:
    if help_file:
        return (Path(help_file)).read_text(encoding="utf-8")
    try:
        proc = subprocess.run(
            ["hscc", "--help"], capture_output=True, text=True, timeout=60,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(
            "error: `hscc` is not on PATH. Install it, or pass --help-file "
            "with a captured `hscc --help`."
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("error: `hscc --help` timed out.")
    if proc.returncode != 0 and not proc.stdout.strip():
        raise SystemExit(
            f"error: `hscc --help` exited {proc.returncode}: "
            f"{proc.stderr.strip()[:200]}"
        )
    return strip_ansi(proc.stdout)


def cli_version(help_text: str) -> str:
    lines = help_text.splitlines()
    first = lines[0] if lines else ""
    m = re.search(r"v(\d+\.\d+\.\d+)", first)
    return m.group(1) if m else "unknown"


def command_key(signature: str) -> str:
    """`cluster down [--dry-run]` -> `cluster down`.

    The path stops at the first token that is not a bare lowercase word, so
    arguments, options and alternates never become part of the key.
    """
    words = []
    for tok in signature.split():
        if not WORD.match(tok):
            break
        words.append(tok)
    return " ".join(words)


def parse_help(help_text: str) -> list[dict]:
    """-> [{"name": group, "sigs": ["start", "stop", ...]}, ...]

    Entries are the lines indented exactly two spaces; a description that wraps
    is indented further and is not an entry. The parse stops at the first
    non-group column-0 line after the commands start (Examples, Docs, ...).
    """
    groups: list[dict] = []
    current: dict | None = None
    for raw in help_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            head = line.strip()
            if head.startswith(NON_GROUPS):
                if current is not None:
                    break          # past the command list: Examples / Docs / ...
                continue
            current = {"name": head, "sigs": []}
            groups.append(current)
            continue
        if current is None:
            continue               # the usage banner before the first group
        if not line.startswith("  ") or line.startswith("   "):
            continue               # wrapped description, not an entry
        sig = line.strip()
        if not sig:
            continue
        current["sigs"].append(sig)
    return [g for g in groups if g["sigs"]]


BULLET = re.compile(r"^- `hscc (?P<sig>[^`]+)` — (?P<prose>.*)$")
H3 = re.compile(r"^### (?P<name>.+)$")


def parse_copy_block(copy_text: str) -> list[dict]:
    """-> [{"name": group, "note": str|None, "items": [{"sig","prose"}]}, ...]

    Reads the block between the COMMANDS markers in site/COPY.md. A bullet's
    explanation may wrap onto indented continuation lines; those are joined
    with a single space, which is the shape the format contract describes.
    """
    try:
        start = copy_text.index(BEGIN)
        end = copy_text.index(END, start)
    except ValueError:
        raise SystemExit(
            "error: site/COPY.md is missing the COMMANDS markers "
            f"({BEGIN!r} ... {END!r})."
        )
    block = copy_text[start:end]
    # Drop the BEGIN marker comment itself.
    block = block[block.index("-->") + 3:]

    groups: list[dict] = []
    current: dict | None = None
    last: dict | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            last = None
            continue
        m = H3.match(line.strip())
        if m:
            current = {"name": m.group("name").strip(), "note": None,
                       "items": []}
            groups.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if line.startswith("  ") and last is not None:
            last["prose"] = (last["prose"] + " " + stripped).strip()
            continue
        m = BULLET.match(stripped)
        if m:
            last = {"sig": m.group("sig").strip(),
                    "prose": m.group("prose").strip()}
            current["items"].append(last)
            continue
        last = None
        if current["note"] is None and not stripped.startswith("<!--"):
            current["note"] = stripped
    return groups


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


BACKTICK = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    """Escape, then render a copy card's `code` span as an HTML <code>.

    The copy stays character-for-character what site/COPY.md says; only the
    markup convention changes, so the page cannot reword the card by accident.
    """
    return BACKTICK.sub(lambda m: f"<code>{esc(m.group(1))}</code>", esc(text))


def key_matches(help_sig_key: str, copy_sig_key: str) -> bool:
    """A COPY bullet may name a concrete subcommand of a help entry.

    `hscc api start` stands for the help entry `api <cmd>`; `hscc check`
    matches `check [stream]`. Anything else has to be the same path.
    """
    return (copy_sig_key == help_sig_key
            or copy_sig_key.startswith(help_sig_key + " "))


def align(help_groups: list[dict], copy_groups: list[dict]) -> tuple[list[dict],
                                                                    list[str]]:
    """Pair each CLI command with its COPY line. Returns rows + problems."""
    problems: list[str] = []
    rows: list[dict] = []

    cli_flat = [(g["name"], s) for g in help_groups for s in g["sigs"]]
    copy_flat = [(g["name"], it) for g in copy_groups for it in g["items"]]

    for g in copy_groups:
        if g["name"] not in {h["name"] for h in help_groups}:
            problems.append(
                f"site/COPY.md has a command group the CLI does not list: "
                f"{g['name']!r}"
            )
    for h in help_groups:
        if h["name"] not in {g["name"] for g in copy_groups}:
            problems.append(
                f"`hscc --help` lists a command group the page does not have: "
                f"{h['name']!r}"
            )

    if len(cli_flat) != len(copy_flat):
        problems.append(
            f"the page lists {len(copy_flat)} commands, "
            f"`hscc --help` lists {len(cli_flat)}"
        )

    for idx in range(min(len(cli_flat), len(copy_flat))):
        (hg, hsig), (cg, item) = cli_flat[idx], copy_flat[idx]
        hkey = command_key(hsig)
        ckey = command_key(item["sig"])
        if hg != cg:
            problems.append(
                f"#{idx + 1}: `{item['sig']}` sits under {cg!r} but the CLI "
                f"has it under {hg!r}"
            )
        elif not key_matches(hkey, ckey):
            problems.append(
                f"#{idx + 1}: the CLI's `{hsig}` (key `{hkey}`) is not the "
                f"page's `hscc {item['sig']}` (key `{ckey}`)"
            )
        if not item["prose"]:
            problems.append(f"#{idx + 1}: `hscc {item['sig']}` has no "
                            f"explanation in site/COPY.md")
        rows.append({"group": hg, "sig": item["sig"],
                     "prose": item["prose"],
                     "note": next((g["note"] for g in copy_groups
                                   if g["name"] == hg), None)})
    return rows, problems


def render(rows: list[dict]) -> str:
    """The generated region of site/index.html, markers included."""
    out = [
        BEGIN + " — generated by scripts/gen_site_commands.py from `hscc --help`;",
        "     do not hand-edit. Regenerate: python3 scripts/gen_site_commands.py --write",
        "     Prose comes from site/COPY.md (the copy card); the command list, order and",
        "     grouping come from the installed CLI, so the two cannot drift apart. -->",
    ]
    seen: list[str] = []
    for row in rows:
        if row["group"] not in seen:
            if seen:
                out += ["</ul>", "</div>"]
            seen.append(row["group"])
            out += ['<div class="cmdgroup">',
                    f"<h3>{esc(row['group'])}</h3>"]
            if row["note"]:
                out += [f'<p class="note">{inline(row["note"])}</p>']
            out += ['<ul class="cmds">']
        out.append(f'<li><code>{esc("hscc " + row["sig"])}</code>'
                   f'<span class="say">{inline(row["prose"])}</span></li>')
    if seen:
        out += ["</ul>", "</div>"]
    out.append(END)
    return "\n".join(out)


def splice(page: str, region: str) -> str:
    try:
        start = page.index(BEGIN)
        end = page.index(END, start) + len(END)
    except ValueError:
        raise SystemExit(
            "error: site/index.html is missing the COMMANDS markers "
            f"({BEGIN!r} ... {END!r})."
        )
    return page[:start] + region + page[end:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="rewrite the generated region in site/index.html")
    ap.add_argument("--help-file",
                    help="use a captured `hscc --help` instead of running the CLI")
    ap.add_argument("--copy", default=str(COPY_MD),
                    help="copy card to read prose from (default site/COPY.md)")
    ap.add_argument("--page", default=str(INDEX_HTML),
                    help="page to check or rewrite (default site/index.html)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    copy_path, page_path = Path(args.copy), Path(args.page)
    help_text = fetch_help(args.help_file)
    help_groups = parse_help(help_text)
    if not help_groups:
        raise SystemExit("error: parsed zero command groups out of `hscc --help` "
                         "— the output format changed; teach parse_help().")
    copy_groups = parse_copy_block(copy_path.read_text(encoding="utf-8"))
    rows, problems = align(help_groups, copy_groups)
    if problems:
        print("drift between the CLI and the landing page:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nWrite the missing line(s) in site/COPY.md (the copy card owns "
              "the wording — this script will not invent one), then re-run "
              "with --write.", file=sys.stderr)
        return 1

    region = render(rows)
    page = page_path.read_text(encoding="utf-8")
    updated = splice(page, region)

    if args.write:
        if updated != page:
            page_path.write_text(updated, encoding="utf-8")
            if not args.quiet:
                print(f"wrote {page_path} "
                      f"({len(rows)} commands, hscc {cli_version(help_text)})")
            return 0
        if not args.quiet:
            print(f"{page_path} is up to date "
                  f"({len(rows)} commands, hscc {cli_version(help_text)})")
        return 0

    if updated != page:
        print(f"{page_path} is not what the CLI generates "
              f"(hscc {cli_version(help_text)}). Diff:", file=sys.stderr)
        diff = difflib.unified_diff(page.splitlines(), updated.splitlines(),
                                    fromfile=str(page_path),
                                    tofile=f"{page_path} (generated)",
                                    lineterm="", n=2)
        for line in diff:
            print(line, file=sys.stderr)
        print("\nFix: python3 scripts/gen_site_commands.py --write",
              file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{page_path} matches `hscc --help` "
              f"({len(rows)} commands, hscc {cli_version(help_text)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
