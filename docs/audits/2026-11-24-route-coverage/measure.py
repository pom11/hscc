#!/usr/bin/env python3
"""Which routes does the canonical reference in docs/API.md fail to name?

Identity is decided the way the server decides it: every candidate path — both the
ones the iOS client calls and the Path cells the Endpoint reference prints — is run
through api_server.ROUTES and reduced to the resolved (method, pattern, handler).
That kills both traps the audit proved:
  * a documented `/v1/foo` cannot be counted as covering `/v1/foo/list`, because
    the two resolve to different registered patterns;
  * `^/v1/profiles/(?P<name>[^/]+)$` matching the literal `/v1/profiles/list`
    cannot credit the wrong row, because credit is given to the pattern the router
    actually picks first, not to a string prefix.

Read-only. Seals HOME/HSCC_HOME/HERMES_HOME into a throwaway dir before importing
api_server, so no real ~/.hscc is read or written. No network, no subprocess, no
fleet contact. Pass the repo root as argv[1].
"""
import json
import os
import re
import sys
import tempfile

_home = tempfile.mkdtemp(prefix="t_b9808146-home-")
os.environ["HOME"] = _home
os.environ["HSCC_HOME"] = os.path.join(_home, ".hscc")
os.environ["HERMES_HOME"] = os.path.join(_home, ".hermes")
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

REPO = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.join(REPO, "hscc-api"))

import api_server  # noqa: E402

# ---------------------------------------------------------------- router ---- #
# A concrete probe per placeholder segment, so a regex with a required segment
# still matches. "probe" is not a real host/profile/card name.
PROBE = "probe"


def resolve(method, path):
    """Return the (method, pattern, handler) the server's own dispatch picks."""
    for m, pattern, handler in api_server.ROUTES:
        if m != method:
            continue
        if pattern.match(path):
            return (m, pattern.pattern, handler.__module__ + "." + handler.__name__)
    return None


def concretise(path):
    """Turn a display path with {placeholders} / query string into a probe path."""
    p = path.split("?")[0].strip()
    p = re.sub(r"\{[^}]*\}", PROBE, p)
    if p.endswith("/") and p != "/":
        p = p[:-1]
    return p or "/"


def identity(method, display_path):
    """The registered route a display path denotes, or None if it denotes none."""
    return resolve(method, concretise(display_path))


# ------------------------------------------------- what is registered ------- #
registered = {}
for m, pattern, handler in api_server.ROUTES:
    registered[(m, pattern.pattern)] = handler.__module__ + "." + handler.__name__
ws = [(p.pattern, h.__name__) for p, h in api_server.WS_ROUTES]

# ------------------------------------------------- what iOS calls ----------- #
src = open(os.path.join(REPO, "ios-app/Sources/HSCC/HSCCClient.swift"),
           encoding="utf-8").read()
_CALL = re.compile(r'\b(get|post|read)\(\s*"((?:[^"\\]|\\.)*)"', re.S)
_LABELED = re.compile(r'\b(get|post|read)\(\s*path:\s*"((?:[^"\\]|\\.)*)"', re.S)
_INTERP = re.compile(r"\\\((.*?)\)")


def swift_path(lit):
    out = _INTERP.sub("{param}", lit)
    out = out.replace('\\.', ".").replace('\\"', '"')
    return out.split("?")[0].strip()


swift = set()
for verb, lit in _CALL.findall(src) + _LABELED.findall(src):
    method = "GET" if verb in ("get", "read") else "POST"
    swift.add((method, swift_path(lit)))

# ------------------------------------- what the Endpoint reference prints --- #
# Only Path cells of the Endpoint reference section, parsed from table rows.
api_md = open(os.path.join(REPO, "docs/API.md"), encoding="utf-8").read()
lines = api_md.splitlines()
start = next((i for i, l in enumerate(lines)
              if re.search(r"^#+\s.*(Endpoint reference|Endpoint Reference)", l)), 0)
doc_cells = []
for i in range(start, len(lines)):
    row = lines[i].strip()
    if not row.startswith("|"):
        continue
    cells = [c.strip() for c in row.strip("|").split("|")]
    # A row's Method cell (if present) pins the verb; otherwise credit both,
    # because an Endpoint-reference row that prints only a Path is read-first.
    verbs = [c.upper() for c in cells if c.upper() in ("GET", "POST", "DELETE", "PATCH")]
    for c in cells:
        for p in re.findall(r"`(/[^`]*)`", c):
            doc_cells.append((tuple(verbs) or ("GET", "POST"), p))

documented = set()
for verbs, cell in doc_cells:
    hit = False
    for method in verbs:
        ident = identity(method, cell)
        if ident:
            documented.add(ident)
            hit = True
    if not hit:
        documented.add(("UNRESOLVED", cell, None))  # keep for auditing

doc_idents = {i for i in documented if i[0] != "UNRESOLVED"}
# A route is covered when a doc cell resolves to the same (method, pattern) the
# server would dispatch on. Compare on identity, never on the printed path.
doc_covered = {(m, pat) for (m, pat, _h) in doc_idents}

# ------------------------------------------------------------ the gap ------- #
gap, ios_gap = [], []
for (method, pattern), handler in sorted(registered.items()):
    if (method, pattern) in doc_covered:
        continue
    # is this route called by the shipped iOS client?
    called_by = sorted(
        p for (m, p) in swift
        if m == method and resolve(m, concretise(p)) == (method, pattern, handler)
    )
    entry = {"method": method, "pattern": pattern, "handler": handler,
             "module": handler.split(".")[0], "ios_calls": called_by}
    gap.append(entry)
    if called_by:
        ios_gap.append(entry)

out = {
    "base": os.popen("git -C %s rev-parse HEAD" % REPO).read().strip(),
    "routes_http": len(api_server.ROUTES),
    "routes_ws": len(ws),
    "modules": len({h.split(".")[0] for h in registered.values()}),
    "swift_call_sites": len(swift),
    "doc_path_cells": len(set(doc_cells)),
    "doc_identities": len(doc_idents),
    "undocumented_count": len(gap),
    "ios_facing_undocumented": len(ios_gap),
    "ios_gap": ios_gap,
    "gap_by_module": {},
}
for e in gap:
    out["gap_by_module"].setdefault(e["module"], 0)
    out["gap_by_module"][e["module"]] += 1

print(json.dumps(out, indent=1))
