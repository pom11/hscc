#!/usr/bin/env python3
"""Generate the CLI-side golden for the merge-queue widget fixture.

WHY THIS EXISTS
---------------
`MergeQueueTableReading.swift` claims to draw exactly the cells
`hscc kanban monitor`'s merge-queue table draws. That claim is only checkable
if the expected strings come from the CLI itself rather than from someone
reading the code and transcribing it (a transcribed golden tests the
transcription, not the CLI).

So this script feeds a committed `GET /v1/merge-queue` fixture into the REAL
renderer (`hscc_daemon.kanban_live.merge_queue_table`) and writes out each
column title and each cell. `model_decode_check.sh` then runs the Swift
projection on the same fixture and compares. Edit `merge_queue_table`/`fmt_age`
and the golden moves; `--write` refreshes it, and the Swift parity check fails
until the widget's reading moves too.

Note the honesty condition: since 2.17.5 the CLI monitors print ONE compact
`mq` line (`kanban_live.merge_queue_line`), not a table — this table is the
renderer the WIDGET is pinned to, kept in the CLI module so the golden still
comes from real CLI code rather than from a transcription of it. The widget
still draws the table (there is room for it on the iPhone home screen); the
two surfaces were never claimed to render identically, only that the widget's
cells match this function's cells.

The golden is COMMITTED: the decode gate runs offline and must stay that way.

USAGE
    python3 ios-app/scripts/merge_queue_cli_parity.py \\
        ios-app/scripts/model_decode_check/fixtures/v1_merge_queue.json
        # prints the golden to stdout

    ... --write        # writes <fixture>.cli.json beside it

Needs `rich` (the CLI's own dependency). The repo's test venv has it; the
hscc formula's venv does too.
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)


def plain(cell) -> str:
    """A rich renderable as the characters the terminal actually showed."""
    if hasattr(cell, "plain"):
        return cell.plain
    from rich.text import Text
    return Text.from_markup(str(cell)).plain


def table_cells(table) -> tuple[list[str], list[list[str]]]:
    """(header cells, rows of cell strings) of a built rich `Table`."""
    header = [plain(c.header) for c in table.columns]
    cells = [list(col.cells) for col in table.columns]
    n = len(cells[0]) if cells else 0
    if any(len(col) != n for col in cells):
        raise SystemExit("REFUSED: the CLI table's columns are ragged — "
                         "a cell-based golden would silently drop a column")
    rows = [[plain(col[i]) for col in cells] for i in range(n)]
    return header, rows


def build_golden(payload: dict, label: str) -> dict:
    from hscc_daemon.kanban_live import merge_queue_table

    projects = payload.get("projects") or {}
    tables = {}
    flattened = []
    for name, project in projects.items():
        rows = project.get("rows") or []
        header, cells = table_cells(merge_queue_table(rows))
        tables[name] = {"header": header, "rows": cells}
        flattened.extend(rows)
    if flattened != (payload.get("rows") or []):
        raise SystemExit("REFUSED: the fixture's flattened rows differ from its "
                         "per-project rows — the two representations disagree")

    return {
        "generated_by": "ios-app/scripts/merge_queue_cli_parity.py",
        "source_module": "hscc_daemon/kanban_live.py::merge_queue_table",
        "fixture": label,
        "tables": tables,
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    write = "--write" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    path = os.path.abspath(args[0])
    payload = json.loads(open(path, encoding="utf-8").read())

    golden = build_golden(payload, os.path.basename(path))
    text = json.dumps(golden, indent=1, ensure_ascii=False, sort_keys=True) + "\n"

    if not write:
        sys.stdout.write(text)
        return 0
    out = path.replace(".json", ".cli.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(out, REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
