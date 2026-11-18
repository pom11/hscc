#!/usr/bin/env python3
"""Generate the CLI-side golden for one cluster-monitor fixture.

WHY THIS EXISTS
---------------
`ClusterMonitorViewModel.swift` claims to format EVERY column the way
`hscc cluster monitor` does. That claim is only checkable if the expected
strings come from the CLI itself rather than from someone reading the code and
transcribing it (a transcribed golden tests the transcription, not the CLI —
the same trap `hscc-ios-docs-no-transcribed-counts` records for counts).

So this script feeds a fixture's bytes into the REAL renderers
(`hscc_daemon.cluster_live.host_table` / `serving_table` / `render`) and writes
out what the CLI actually put in each cell, plus the plain-text render of the
whole view. `model_decode_check` then runs the Swift view-model on the same
fixture and compares. Edit `cluster_live.py`'s formatting and the golden moves;
run this with `--write` to refresh it, and the Swift parity check fails until
the view-model moves too. That is the whole point of the pairing.

The golden is COMMITTED: `model_decode_check.sh` runs offline and must stay
that way, so the harness reads the golden, it does not regenerate it.

USAGE
-----
    python3 ios-app/scripts/cluster_monitor_cli_parity.py \\
        ios-app/scripts/model_decode_check/fixtures/v1_cluster_monitor_live.json
        # prints the golden to stdout

    ... --write        # write <fixture>.cli.json + <fixture>.cli.txt beside it

Needs `rich` (the CLI's own dependency) on the interpreter; the hscc formula's
venv has it:
    /opt/homebrew/Cellar/hscc/<version>/libexec/venv/bin/python3 \\
        ios-app/scripts/cluster_monitor_cli_parity.py ... --write

WHAT IT REFUSES
---------------
A fixture carrying an address this repo may not commit. The committed fixtures are
sanitized to RFC 5737 TEST-NET (`192.0.2.x`) and the tailnet placeholder
(`100.64.0.1`) per `fixtures/README.md`, and a golden is generated FROM a fixture,
so a leak would otherwise be laundered into a second committed file. The check is
`scripts/address_guard.py`'s own — imported, not copied — so the generator cannot
disagree with the pre-commit hook about what counts.
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

# `render()` at two widths: 140 keeps every column, 90 is below
# `cluster_live.WIDE_MIN` (110), which is the only thing that drops `Routes`
# and `Prompt t/s`. Both are recorded so the Swift side's width rule is pinned
# against the CLI's, not against a reading of it.
WIDE_WIDTH = 140
NARROW_WIDTH = 90


def leaked_addresses(text: str) -> list[str]:
    """Addresses in `text` this repo may not commit, measured by its OWN guard.

    Imported from `scripts/address_guard.py` rather than re-typed here. A copied
    ban list is a second thing to keep correct, and the way a generator like this
    one goes quietly wrong is by disagreeing with the hook after someone adds a
    range to one side. This is the tool that can still fix a leak (it is about to
    write the second copy of it into a golden), so it enforces the real rule
    rather than an approximation of it.
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import address_guard  # stdlib-only, and side-effect-free at import
    return [f"line {line}: {addr}" for line, addr in address_guard.find_in_text(text)]


def plain(cell) -> str:
    """A rich renderable as the characters the terminal showed."""
    return cell.plain if hasattr(cell, "plain") else str(cell)


def table_cells(table) -> tuple[list[str], list[list[str]]]:
    """(header cells, rows of cell strings) of a built rich `Table`.

    Read off the table object rather than its rendered text: the render pads
    each column to a computed width, and a Swift re-implementation of rich's
    width algorithm would be a second thing to keep correct (and to get
    wrong). The CELL strings are the CLI's formatting, which is the claim.
    """
    header = [plain(c.header) for c in table.columns]
    # `Column.cells` is an iterator in rich, so materialize it once per column.
    cells = [list(col.cells) for col in table.columns]
    n = len(cells[0]) if cells else 0
    if any(len(col) != n for col in cells):
        raise SystemExit("REFUSED: the CLI table's columns are ragged — "
                         "a cell-based golden would silently drop a column")
    rows = [[plain(col[i]) for col in cells] for i in range(n)]
    return header, rows


def render_view(fixture: dict, width: int) -> str:
    """`cluster_live.render(...)` at `width`, through the CLI's OWN console.

    `cli_theme.make_console` is what registers the `accent`/`title`/`warn`
    styles the renderables name — a bare `Console()` raises
    `MissingStyle: 'accent'`, and hand-registering a subset would be
    re-implementing the CLI's setup instead of using it.
    """
    from hscc_daemon import cli_theme, cluster_live

    console = cli_theme.make_console(cli_theme.DEFAULT_THEME, width=width,
                                     color_system=None, force_terminal=False)
    with console.capture() as cap:
        console.print(cluster_live.render(fixture.get("json"),
                                          fixture.get("serving") or [], None,
                                          fixture.get("serving_proxy_error"),
                                          fixture.get("error"),
                                          snapshot=True, width=width))
    return cap.get()


def build_golden(fixture: dict, label: str) -> dict:
    from hscc_daemon import cluster_live

    frame = fixture.get("json")
    rows = fixture.get("serving") or []

    host_header, host_rows = table_cells(cluster_live.host_table(frame))
    wide_head, wide_rows = table_cells(cluster_live.serving_table(rows, WIDE_WIDTH))
    narrow_head, narrow_rows = table_cells(cluster_live.serving_table(rows, NARROW_WIDTH))

    return {
        "generated_by": "ios-app/scripts/cluster_monitor_cli_parity.py",
        "source_module": "hscc_daemon/cluster_live.py",
        "fixture": label,
        # The header line `render()` builds. The host count in it is the same
        # number the view-model's `hostCount` must equal.
        "title_wide": render_view(fixture, WIDE_WIDTH).splitlines()[0].strip(),
        "host_header": host_header,
        "host_rows": host_rows,
        "serving_header_wide": wide_head,
        "serving_rows_wide": wide_rows,
        "serving_header_narrow": narrow_head,
        "serving_rows_narrow": narrow_rows,
    }


def golden_texts(fixture: dict) -> dict[str, str]:
    """The full plain-text CLI render at the width the cell goldens came from.

    One file, wide only: at the narrow width rich is allowed to elide cells and
    to wrap them across lines, so the text there is not a checkable view of the
    cell strings (the narrow CELLS are still recorded, from the table object).
    Kept as a `.txt` fixture rather than a field of the JSON so no text is
    committed twice — a copy is a thing that can drift.
    """
    return {".cli.txt": render_view(fixture, WIDE_WIDTH)}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    write = "--write" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    path = os.path.abspath(args[0])
    text = open(path, encoding="utf-8").read()

    bad = leaked_addresses(text)
    if bad:
        print(f"REFUSED: {os.path.basename(path)} carries address(es) this repo may "
              f"not commit ({', '.join(bad)}) — sanitize to the documented "
              f"placeholders first (fixtures/README.md, scripts/address_guard.py)",
              file=sys.stderr)
        return 1

    fixture = json.loads(text)
    golden = build_golden(fixture, os.path.basename(path))
    payload = json.dumps(golden, indent=1, ensure_ascii=False, sort_keys=True) + "\n"

    if not write:
        sys.stdout.write(payload)
        return 0
    files = {".cli.json": payload}
    files.update(golden_texts(fixture))
    for suffix, body in files.items():
        out = path.replace(".json", suffix)
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"wrote {os.path.relpath(out, REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
