#!/usr/bin/env python3
"""One file per widget -> the lists every widget PR used to edit in common.

Measured hot spots (last 100 squash merges on main, 2026-11-19):
``ios-app/project.yml`` (25 commits), ``macos-app/project.yml`` (24),
``ios-app/docs/WIDGET_COVERAGE.md`` (14), ``ios-app/scripts/model_decode_check/
main.swift`` (11), ``WidgetSurfaceKind`` + its labels (the settings list) and
``HSCCWidgetsBundle.swift``. Every widget card appended to those same lines, so
the first merge broke every other widget PR.

The source of truth is now ``ios-app/widgets/<id>.json`` — one file per widget
or Live Activity. Adding a widget means adding one JSON file and running
``--write``. Nothing in this generator reaches the network, git, gh or the live
Hermes/HSCC state.

    python3 ios-app/scripts/widget_registry.py --check
    python3 ios-app/scripts/widget_registry.py --write

``--check`` is the gate: it fails when a manifest is missing from a generated
artifact, when a generated artifact carries a record no manifest declares, when
a widget source file on disk has no owner, or when a target spec stopped using
the computed widget directory.

The generated files are append-only record lists, which is what makes
``merge=union`` provably safe for them (see .gitattributes and
scripts/tests/test_widget_registry.py). project.yml is NOT marked union: its
widget source lists are replaced by one directory path per target, so the file
is simply not touched when a widget is added.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios-app"
WIDGET_DIR = IOS / "widgets"
SOURCES = IOS / "Sources" / "HSCCWidgets"
SHARED_JSON = WIDGET_DIR / "_shared.json"

ENUM_FILE = IOS / "Sources" / "Shared" / "WidgetSurfaceKind.generated.swift"
BUNDLE_FILE = IOS / "Sources" / "HSCCWidgets" / "HSCCWidgetList.generated.swift"
LABELS_FILE = IOS / "Sources" / "HSCC" / "Views" / "WidgetSurfaceLabels.generated.swift"
COVERAGE_FILE = IOS / "docs" / "WIDGET_COVERAGE.widgets.generated.md"
DECODE_FILE = IOS / "scripts" / "model_decode_check" / "widgets.generated.swift"

REQUIRED = (
    "id", "order", "kind", "surface_case", "surface_raw", "display_name", "icon",
)
WIDGET_REQUIRED = (
    "widget_type", "sources", "coverage_ios", "coverage_macos", "coverage_source",
)
IOS_FAMILIES = (
    "systemSmall", "systemMedium", "systemLarge",
    "accessoryInline", "accessoryCircular", "accessoryRectangular",
)
MACOS_FAMILIES = ("systemSmall", "systemMedium", "systemLarge", "systemExtraLarge")
BUNDLE_ORDER_FALLBACK = 1000


def load_manifests() -> tuple[list[dict], dict]:
    """Load and validate the per-surface JSON files plus the shared-source file."""
    if not WIDGET_DIR.is_dir():
        raise SystemExit(f"widget registry: missing {WIDGET_DIR}")
    shared = json.loads(SHARED_JSON.read_text()) if SHARED_JSON.exists() else {"sources": []}
    manifests = []
    for path in sorted(WIDGET_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"widget registry: {path.name}: {exc}") from exc
        data["_file"] = path.name
        for key in REQUIRED:
            if not data.get(key):
                raise SystemExit(f"widget registry: {path.name}: missing {key}")
        if data["kind"] == "widget":
            for key in WIDGET_REQUIRED:
                if key not in data or data[key] in ("", []):
                    raise SystemExit(f"widget registry: {path.name}: missing {key}")
            if len(data["coverage_ios"]) != len(IOS_FAMILIES):
                raise SystemExit(
                    f"widget registry: {path.name}: coverage_ios needs "
                    f"{len(IOS_FAMILIES)} cells"
                )
            if len(data["coverage_macos"]) != len(MACOS_FAMILIES):
                raise SystemExit(
                    f"widget registry: {path.name}: coverage_macos needs "
                    f"{len(MACOS_FAMILIES)} cells"
                )
        elif data["kind"] != "live_activity":
            raise SystemExit(f"widget registry: {path.name}: unknown kind {data['kind']!r}")
        manifests.append(data)

    if not manifests:
        raise SystemExit("widget registry: no manifests found")

    for field in ("id", "order", "surface_case", "surface_raw"):
        seen = {}
        for m in manifests:
            value = m[field]
            if value in seen:
                raise SystemExit(
                    f"widget registry: {field} {value!r} is declared by both "
                    f"{seen[value]} and {m['_file']}"
                )
            seen[value] = m["_file"]

    widgets = [m for m in manifests if m["kind"] == "widget"]
    seen_types = {}
    for m in widgets:
        t = m["widget_type"]
        if t in seen_types:
            raise SystemExit(
                f"widget registry: widget_type {t!r} is declared twice "
                f"({seen_types[t]}, {m['_file']})"
            )
        seen_types[t] = m["_file"]

    # Every Swift file in the widget directory must have exactly one owner:
    # a widget manifest or _shared.json. An unowned file would compile via the
    # directory glob but never appear in the generated lists.
    owners = {}
    for m in widgets:
        for rel in m["sources"]:
            owners.setdefault(rel, []).append(m["id"])
    for rel in shared.get("sources", []):
        owners.setdefault(rel, []).append("_shared")
    for rel, who in sorted(owners.items()):
        if len(who) != 1:
            raise SystemExit(f"widget registry: {rel} is owned by {who}")
        if not (IOS / rel).is_file():
            raise SystemExit(f"widget registry: {rel} is listed but missing")
    on_disk = {
        str(p.relative_to(IOS)) for p in SOURCES.glob("*.swift")
    }
    unowned = sorted(on_disk - set(owners))
    if unowned:
        raise SystemExit(
            "widget registry: widget source(s) with no manifest owner: "
            + ", ".join(unowned)
        )
    stale = sorted(set(owners) - on_disk)
    if stale:
        raise SystemExit(
            "widget registry: manifest lists source(s) not in "
            f"Sources/HSCCWidgets: {', '.join(stale)}"
        )
    return manifests, shared


def by_order(manifests: list[dict]) -> list[dict]:
    return sorted(manifests, key=lambda m: (m["order"], m["id"]))


def render_enum(manifests: list[dict]) -> str:
    lines = [
        "// GENERATED by ios-app/scripts/widget_registry.py --write — do not edit.",
        "// Source of truth: ios-app/widgets/*.json (one file per surface).",
        "// Append-only record list: .gitattributes marks it merge=union.",
        "",
        "enum WidgetSurfaceKind: String, CaseIterable, Codable {",
    ]
    lines += [f'    case {m["surface_case"]} = "{m["surface_raw"]}"' for m in by_order(manifests)]
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_bundle(manifests: list[dict]) -> str:
    widgets = [m for m in manifests if m["kind"] == "widget"]
    widgets.sort(key=lambda m: (m.get("bundle_order", BUNDLE_ORDER_FALLBACK), m["order"]))
    lines = [
        "// GENERATED by ios-app/scripts/widget_registry.py --write — do not edit.",
        "// Source of truth: ios-app/widgets/*.json (one file per widget).",
        "// Append-only record list: .gitattributes marks it merge=union.",
        "",
        "import SwiftUI",
        "import WidgetKit",
        "",
        "/// The widget list compiled into HSCCWidgets (iOS) and HSCCMacWidgets (macOS).",
        "/// `HSCCWidgetsBundle` delegates to this generated bundle so a widget PR never",
        "/// edits the hand-written bundle header.",
        "struct HSCCWidgetList: WidgetBundle {",
        "    var body: some Widget {",
    ]
    lines += [f'        {m["widget_type"]}()' for m in widgets]
    lines += ["    }", "}"]
    return "\n".join(lines) + "\n"


def render_labels(manifests: list[dict]) -> str:
    ordered = by_order(manifests)
    lines = [
        "// GENERATED by ios-app/scripts/widget_registry.py --write — do not edit.",
        "// Source of truth: ios-app/widgets/*.json (one file per surface).",
        "// Append-only record list: .gitattributes marks it merge=union.",
        "",
        "import Foundation",
        "",
        "extension WidgetSurfaceKind {",
        "    /// Display name shown in the settings rows and editor titles.",
        "    var displayName: String {",
        "        switch self {",
    ]
    lines += [
        f'        case .{m["surface_case"]}: return "{m["display_name"]}"'
        for m in ordered
    ]
    lines += [
        "        }",
        "    }",
        "",
        "    /// SF Symbol for the settings row.",
        "    var displayIcon: String {",
        "        switch self {",
    ]
    lines += [
        f'        case .{m["surface_case"]}: return "{m["icon"]}"'
        for m in ordered
    ]
    lines += ["        }", "    }", "}"]
    return "\n".join(lines) + "\n"


def coverage_row(m: dict) -> str:
    cells = [m["display_name"].replace(" Widget", "")] + list(m["coverage_ios"]) + [m["coverage_source"]]
    return "| " + " | ".join(cells) + " |"


def coverage_row_macos(m: dict) -> str:
    cells = [m["display_name"].replace(" Widget", "")] + list(m["coverage_macos"]) + [m["coverage_source"]]
    return "| " + " | ".join(cells) + " |"


def render_coverage(manifests: list[dict]) -> str:
    widgets = [m for m in manifests if m["kind"] == "widget"]
    widgets.sort(key=lambda m: (m["order"], m["id"]))
    lines = [
        "<!-- GENERATED by ios-app/scripts/widget_registry.py --write — do not edit. -->",
        "<!-- Source of truth: ios-app/widgets/*.json (one file per widget). -->",
        "<!-- Append-only row lists: .gitattributes marks this file merge=union. -->",
        "",
        "# Widget coverage — generated rows",
        "",
        "The tables below are the per-widget rows of `WIDGET_COVERAGE.md`. A widget",
        "card edits only its own `ios-app/widgets/<id>.json`; two cards touching",
        "different widgets never share a row here.",
        "",
        "## 1. iOS widgets × family",
        "",
        "| Widget | " + " | ".join(IOS_FAMILIES) + " | Source |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [coverage_row(m) for m in widgets]
    lines += [
        "<!-- END iOS WIDGET ROWS -->",
        "",
        "## 3. macOS desktop widgets × family",
        "",
        "| Widget | " + " | ".join(MACOS_FAMILIES) + " | Source |",
        "|---|---|---|---|---|",
    ]
    lines += [coverage_row_macos(m) for m in widgets]
    lines += ["<!-- END macOS WIDGET ROWS -->", ""]
    return "\n".join(lines)


def render_decode(manifests: list[dict]) -> str:
    hooks = [m for m in by_order(manifests) if m.get("decode_hook")]
    lines = [
        "// GENERATED by ios-app/scripts/widget_registry.py --write — do not edit.",
        "// Source of truth: ios-app/widgets/*.json (one file per widget).",
        "// Append-only call list: .gitattributes marks it merge=union.",
        "",
        "/// Run every per-widget decode block without editing main.swift.",
        "func checkWidgetDecodeFixtures(_ c: DecodeCheck) {",
    ]
    lines += [f'    {m["decode_hook"]}(c)' for m in hooks]
    lines += ["}", ""]
    return "\n".join(lines)


TARGETS = {
    "ios": (IOS / "project.yml", "HSCCWidgets", "Sources/HSCCWidgets"),
    "macos": (ROOT / "macos-app" / "project.yml", "HSCCMacWidgets", "../ios-app/Sources/HSCCWidgets"),
}
GENERATED_PROJECT_FILES = {
    ("ios", "HSCCWidgets"): ["Sources/Shared/WidgetSurfaceKind.generated.swift"],
    ("ios", "HSCC"): [
        "Sources/Shared/WidgetSurfaceKind.generated.swift",
        "Sources/HSCC/Views/WidgetSurfaceLabels.generated.swift",
    ],
    ("ios", "HSCCLiveActivityMonitor"): ["Sources/Shared/WidgetSurfaceKind.generated.swift"],
    ("macos", "HSCCMacWidgets"): ["../ios-app/Sources/Shared/WidgetSurfaceKind.generated.swift"],
    ("macos", "HSCCMac"): [
        "../ios-app/Sources/Shared/WidgetSurfaceKind.generated.swift",
        "../ios-app/Sources/HSCC/Views/WidgetSurfaceLabels.generated.swift",
    ],
}


def check_project_specs() -> list[str]:
    problems = []
    for label, (path, target, widget_path) in TARGETS.items():
        text = path.read_text()
        if not re.search(rf"^  {re.escape(target)}:$", text, re.M):
            problems.append(f"{path.relative_to(ROOT)}: target {target} missing")
            continue
        block = text.split(f"\n  {target}:", 1)[1]
        block = re.split(r"\n  [A-Za-z]\w*:\n", block, maxsplit=1)[0]
        if f"- path: {widget_path}" not in block:
            problems.append(
                f"{path.relative_to(ROOT)}: {target} must use the computed "
                f"directory '- path: {widget_path}'"
            )
        for generated in GENERATED_PROJECT_FILES.get((label, target), []):
            if f"- path: {generated}" not in block:
                problems.append(
                    f"{path.relative_to(ROOT)}: {target} does not compile {generated}"
                )
    return problems


def check_file(path: Path, expected: str, problems: list[str], records: set[str],
               pattern: str) -> None:
    if not path.exists():
        problems.append(f"{path.relative_to(ROOT)}: missing (run --write)")
        return
    actual = set(re.findall(pattern, path.read_text(), re.M))
    missing = records - actual
    extra = actual - records
    if missing:
        problems.append(
            f"{path.relative_to(ROOT)}: missing generated record(s): "
            + ", ".join(sorted(missing)[:8])
        )
    if extra:
        problems.append(
            f"{path.relative_to(ROOT)}: record(s) no manifest declares: "
            + ", ".join(sorted(extra)[:8])
        )
    if not problems:
        # Stable headers still matter even when the record set is right.
        for line in expected.splitlines():
            if line.startswith("//") or line.startswith("<!--") or line.startswith("#"):
                if line not in path.read_text():
                    problems.append(f"{path.relative_to(ROOT)}: generated header drifted")


def check(manifests: list[dict], shared: dict) -> list[str]:
    problems = check_project_specs()
    enum_records = {
        f'{m["surface_case"]} = "{m["surface_raw"]}"' for m in manifests
    }
    check_file(ENUM_FILE, render_enum(manifests), problems, enum_records,
               r'^\s*case ([A-Za-z]\w* = "[^"]+")$')

    widgets = [m for m in manifests if m["kind"] == "widget"]
    bundle_records = {f'{m["widget_type"]}()' for m in widgets}
    check_file(BUNDLE_FILE, render_bundle(manifests), problems, bundle_records,
               r"^\s*([A-Za-z]\w*\(\))$")

    label_records = set()
    for m in manifests:
        label_records.add(f'{m["surface_case"]}: return "{m["display_name"]}"')
        label_records.add(f'{m["surface_case"]}: return "{m["icon"]}"')
    check_file(LABELS_FILE, render_labels(manifests), problems, label_records,
               r"^\s*case \.([A-Za-z]\w*: return \"[^\"]+\")$")

    coverage_records = {coverage_row(m) for m in widgets} | {
        coverage_row_macos(m) for m in widgets
    }
    check_file(COVERAGE_FILE, render_coverage(manifests), problems, coverage_records,
               r'^(\| (?!Widget \|).+ \|)$')

    hooks = {f'{m["decode_hook"]}(c)' for m in manifests if m.get("decode_hook")}
    check_file(DECODE_FILE, render_decode(manifests), problems, hooks,
               r"^\s*([A-Za-z]\w*\(c\))$")
    return problems


def write(manifests: list[dict], shared: dict) -> None:
    ENUM_FILE.write_text(render_enum(manifests))
    BUNDLE_FILE.write_text(render_bundle(manifests))
    LABELS_FILE.write_text(render_labels(manifests))
    COVERAGE_FILE.write_text(render_coverage(manifests))
    DECODE_FILE.write_text(render_decode(manifests))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if generated files drift")
    mode.add_argument("--write", action="store_true", help="rewrite the generated files")
    args = parser.parse_args(argv)
    manifests, shared = load_manifests()
    if args.write:
        write(manifests, shared)
        print(f"widget registry: wrote {len(manifests)} surfaces from {WIDGET_DIR.relative_to(ROOT)}")
        return 0
    problems = check(manifests, shared)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print("widget registry: run `python3 ios-app/scripts/widget_registry.py --write`", file=sys.stderr)
        return 1
    print(
        f"widget registry: {len(manifests)} surfaces "
        f"({sum(1 for m in manifests if m['kind'] == 'widget')} widgets, "
        f"{sum(1 for m in manifests if m['kind'] == 'live_activity')} Live Activities); "
        "generated files are current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
