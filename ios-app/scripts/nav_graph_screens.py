#!/usr/bin/env python3
"""nav_graph_screens.py — CODE-DERIVED screen enumerator for the nav-graph
walker gate (t_810ab152, operator brief v2 2026-10-15).

WHY THIS EXISTS
---------------
The walker's screen list must be DERIVED from the app's own sources, not
handwritten, so a new screen cannot dodge the walk. This parser reads:

  * Sources/HSCC/Views/AppShell.swift   -> every `AppDestination` case + the
                                           drawer titles + the legacy-alias
                                           map (`normalized`) + the
                                           ProjectRoute registrations
  * Sources/HSCC/Views/Sidebar.swift    -> every drawer row (`row(.x`)
  * UITests/NavGraphUITests.swift       -> the CURATED walk expectations
                                           (`expect("raw", ...)`) — the part
                                           a human owns: which marker proves
                                           a screen landed, which empty/error
                                           text proves the honest states,
                                           which feed path is the PTR receipt.

EMITTED RULES (the "cannot dodge" contract, enforced by the generated table
and re-checked by NavGraphUITests itself):
  1. every AppDestination case is EITHER a drawer row OR an alias mapping to
     a drawer row (AppShell.normalized);
  2. every drawer row has an expectation block in NavGraphUITests.swift
     (`expect("<raw>", ...)`) — a row without one emits expectation: null and
     the SUITE fails naming the screen;
  3. expectations exist only for real drawer rows (no orphan coverage).

USAGE
  scripts/nav_graph_screens.py check    drift vs the committed table (exit 1)
  scripts/nav_graph_screens.py emit     print NavGraphScreenTable.swift

The parser is deliberately regex-over-swift (the same shape of extraction the
repo's build_check.sh/check_sources.sh already use on project.yml) — it does
not need a Swift toolchain and fails LOUDLY when a pattern disappears (no
silent empty list), which is the failure mode that matters.
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
APPSHELL = APP / "Sources/HSCC/Views/AppShell.swift"
SIDEBAR = APP / "Sources/HSCC/Views/Sidebar.swift"
UITESTS = APP / "UITests/NavGraphUITests.swift"
TABLE = APP / "UITests/NavGraphScreenTable.swift"


def die(msg):
    sys.stderr.write(f"nav_graph_screens: {msg}\n")
    sys.exit(2)


def read(p):
    if not p.exists():
        die(f"missing source: {p}")
    return p.read_text()


def app_destinations(src):
    """Every `case a, b, c` line inside `enum AppDestination`."""
    m = re.search(r"enum AppDestination:.*?\{(.*?)\n}\n", src, re.S)
    if not m:
        die("could not locate `enum AppDestination` in AppShell.swift")
    cases = []
    for line in m.group(1).splitlines():
        line = line.split("//")[0].strip()
        mm = re.match(r"case\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*)\s*$", line)
        if mm:
            cases += [c.strip() for c in mm.group(1).split(",")]
    if len(cases) < 10:
        die(f"suspiciously few AppDestination cases parsed ({len(cases)})")
    return cases


def titles(src):
    """rawValue -> title from the `title` switch ONLY (entries like
    `case .home: "Home"`). Scoped to the `var title` block: the `symbol`
    switch has the identical shape and would otherwise overwrite it."""
    blk = re.search(r"var title: String \{(.*?)\n    \}", src, re.S)
    if not blk:
        die("could not locate AppDestination.title switch")
    out = {}
    for mm in re.finditer(r"case\s+\.(\w+):\s*\"([^\"]+)\"", blk.group(1)):
        out[mm.group(1)] = mm.group(2)
    if len(out) < 10:
        die(f"suspiciously few titles parsed ({len(out)})")
    return out


def normalized_map(src):
    """Legacy alias map from AppShell.normalized: `case .selfHeal: return .autodown`."""
    blk = re.search(r"private func normalized.*?\n    \}", src, re.S)
    if not blk:
        die("could not locate AppShell.normalized")
    return {mm.group(1): mm.group(2)
            for mm in re.finditer(r"case\s+\.(\w+):\s*return\s+\.(\w+)", blk.group(0))}


def drawer_rows(src):
    """Sidebar row order: every `row(.x` / `row(.x,`."""
    rows = [mm.group(1) for mm in re.finditer(r"row\(\.(\w+)", src)]
    if len(rows) < 10:
        die(f"suspiciously few drawer rows parsed ({len(rows)})")
    # preserve order, drop duplicates
    seen, out = set(), []
    for r in rows:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def project_routes(src):
    """ProjectRoute registrations inside AppShell.projectRouteView."""
    blk = re.search(r"func projectRouteView.*?\n    \}", src, re.S)
    if not blk:
        die("could not locate AppShell.projectRouteView")
    return sorted({mm.group(1) for mm in
                   re.finditer(r"case\s+\.(\w+)\(", blk.group(0))})


def expectations(src):
    """Curated walk expectations from UITests/NavGraphUITests.swift.

    One call per drawer row, e.g.
      expect("chats", titleMarker: "Refresh chats",
             emptyMarker: "No projects", errMarker: "Couldn't load projects",
             ptrPath: "/v1/projects")
    """
    out = {}
    for mm in re.finditer(r'expect\(\s*"(\w+)"([^\n]*)\),\s*$', src, re.M):
        raw, body = mm.group(1), mm.group(2)
        def field(name):
            f = re.search(name + r":\s*(\"[^\"]*\"|nil)", body)
            if not f:
                return None
            v = f.group(1)
            return None if v == "nil" else v[1:-1]
        out[raw] = {
            "title": field("titleMarker"),
            "empty": field("emptyMarker"),
            "err": field("errMarker"),
            "ptr": field("ptrPath"),
        }
    return out


def build():
    shell = read(APPSHELL)
    sidebar = read(SIDEBAR)
    uitests = read(UITESTS)
    dests = app_destinations(shell)
    t = titles(shell)
    alias = normalized_map(shell)
    rows = drawer_rows(sidebar)
    routes = project_routes(shell)
    exp = expectations(uitests)

    problems = []
    for r in rows:
        if r not in dests:
            problems.append(f"drawer row .{r} is not an AppDestination case")
    for d in dests:
        if d in rows:
            continue
        target = alias.get(d)
        if target is None or target not in rows:
            problems.append(f"AppDestination .{d} is neither a drawer row nor an alias to one")
    for r in rows:
        if r not in exp:
            problems.append(f"drawer row .{r} has NO walk expectation in NavGraphUITests.swift")
    for e in exp:
        if e not in rows:
            problems.append(f"walk expectation for .{e} but no such drawer row")
    if problems:
        die("enumeration contract violated:\n  - " + "\n  - ".join(problems))
    return rows, t, routes, exp


def emit():
    rows, t, routes, exp = build()
    stamp = datetime.date.today().isoformat()
    lines = [
        "//",
        "//  NavGraphScreenTable.swift — GENERATED. DO NOT EDIT BY HAND.",
        "//",
        "//  Regenerate (and commit) with:  ios-app/scripts/nav_graph_screens.py emit",
        "//  Gate check (fails on drift / missing coverage):  scripts/nav_graph_check.sh",
        "//",
        "//  Source of truth: AppShell.swift (AppDestination + normalized +",
        "//  projectRouteView), Sidebar.swift (drawer rows) and the curated",
        "//  `expect(...)` blocks in NavGraphUITests.swift — parsed by",
        "//  scripts/nav_graph_screens.py. Adding a drawer row without giving it",
        "//  a walk expectation breaks the gate BEFORE the walker even runs,",
        "//  which is the point: a new screen cannot dodge the walk.",
        "//",
        f"//  Generated {stamp} from the code as committed.",
        "",
        "import Foundation",
        "",
        "/// One drawer screen's walk plan. Every field is a stable AX/fixture",
        "/// marker verified against the running app by the walker itself.",
        "struct NavScreenExpectation {",
        "    let raw: String               // AppDestination rawValue == `drawerRow:<raw>` id",
        "    let appTitle: String          // AppDestination.title (drawer row label)",
        "    let titleMarker: String       // unique control/label proving the screen landed",
        "    let emptyMarker: String?      // honest empty-state text (zero-row fixture set)",
        "    let errMarker: String?        // honest failed-row text (fixture failure mode)",
        "    let ptrPath: String?          // feed path whose served-count is the PTR receipt",
        "}",
        "",
        "enum NavGraphScreenTable {",
        "    /// Drawer screens in Sidebar order — the exact order the walker taps them.",
        "    static let screens: [NavScreenExpectation] = [",
    ]
    for r in rows:
        e = exp[r]
        def sw(v):
            return "nil" if v is None else '"' + v.replace('"', '\\"') + '"'
        lines.append(
            f"        NavScreenExpectation(raw: \"{r}\", appTitle: {sw(t.get(r))}, "
            f"titleMarker: {sw(e['title'])}, emptyMarker: {sw(e['empty'])}, "
            f"errMarker: {sw(e['err'])}, ptrPath: {sw(e['ptr'])}),")
    lines += [
        "    ]",
        "",
        "    /// Drawer row rawValues in Sidebar order — the walker iterates",
        "    /// THIS (the code-derived list), never a hand-kept one.",
        "    static var drawerRows: [String] { screens.map(\\.raw) }",
        "",
        "    /// ProjectRoute pushes registered by AppShell.projectRouteView — the",
        "    /// detail walks must cover each name here (the walker's",
        "    /// testProjectRouteRegistrationCoverage asserts this set verbatim).",
        "    static let projectRoutes: [String] = [",
    ]
    for r in routes:
        lines.append(f'        "{r}",')
    lines += [
        "    ]",
        "}",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["emit", "check"])
    args = ap.parse_args()
    generated = emit()
    if args.mode == "emit":
        sys.stdout.write(generated)
        return
    current = TABLE.read_text() if TABLE.exists() else ""
    # Compare everything except the generation-date line (touch-stamp only).
    strip = lambda s: "\n".join(l for l in s.splitlines()
                                if "Generated " not in l)
    if strip(current) != strip(generated):
        sys.stderr.write(
            "nav_graph_screens: UITests/NavGraphScreenTable.swift is DRIFTED "
            "from the code.\nRun: scripts/nav_graph_screens.py emit > "
            "UITests/NavGraphScreenTable.swift and commit the delta.\n")
        # Show a compact diff of the differing lines for the operator.
        cur, gen = strip(current).splitlines(), strip(generated).splitlines()
        import difflib
        for line in list(difflib.unified_diff(cur, gen, "committed", "derived", lineterm=""))[:40]:
            sys.stderr.write("  " + line + "\n")
        sys.exit(1)
    print(f"nav_graph_screens: table matches the code "
          f"({len(build()[0])} drawer screens, {len(build()[2])} ProjectRoutes)")


if __name__ == "__main__":
    main()
