#!/bin/bash
# history_route_guard_check.sh — the Swift widget layer reads metrics history
# through EXACTLY ONE path: `GET /v1/cluster/history`.
#
# Why this exists (DECISION-t_447cc7fc, half of it): the closed `WidgetDataPath.all`
# allowlist is audited precisely because "the set of reads an extension may make
# is enumerable and reviewable" is a property, not a promise. For two days that
# set carried two history reads — the canonical `/v1/cluster/history` and the
# retired `/v1/metrics/history` — and a tile that rendered a history series could
# pick the stale one with a plausible-looking line of code. Nothing else in the
# fleet notices: it compiles, it decodes, it draws a chart off a route the
# ruling retired.
#
# Three legs, because a guard that only bans things can be satisfied by deleting
# the wrong side:
#
#   1. RETIRED READ BANNED. No Swift source under `ios-app/Sources` or
#      `macos-app/Sources` may contain `metrics/history` outside comments.
#      Comments are stripped first, so the ruling can still be *documented* in
#      these files (it is, in three of them) without tripping the guard, while
#      any spelling of the path in code — literal, welded query, or a fragment
#      of a concatenation — is caught.
#   2. NO OTHER HISTORY PATH. Every string literal naming a history route must
#      be one of a set this file names exactly. `/v1/cluster/history` is the
#      canonical metrics history; `/v1/daemon/history` is a DIFFERENT dataset (the
#      automated-action timeline, `HSCCClient.swift`) and is allowed by name, not
#      by pattern. Adding a third allowed spelling has to be a deliberate edit to
#      this file.
#   3. THE CANONICAL READ IS STILL THERE. `WidgetDataClient.swift` still declares
#      `static let clusterHistory = "/v1/cluster/history"` and still routes its
#      read through `WidgetDataPath.clusterHistory`, and carries exactly ONE
#      history path in the audited set. Without this leg the guard could be
#      silenced by deleting the canonical read too.
#
# Proof it bites: `HISTORY_ROUTE_GUARD_NEGATIVE_TEST=1` re-runs the same scan over
# planted copies (a `metrics/history` read, a second `…/history` path, and a
# client whose canonical constant was deleted) and fails unless each one goes
# red. A guard nobody has seen fail is a decoration.
#
# Usage: ios-app/scripts/history_route_guard_check.sh
#        HISTORY_ROUTE_GUARD_NEGATIVE_TEST=1 ios-app/scripts/history_route_guard_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

IOS_ROOT="Sources"
MAC_ROOT="../macos-app/Sources"
CLIENT="Sources/Shared/WidgetDataClient.swift"
for d in "$IOS_ROOT" "$MAC_ROOT"; do
  [ -d "$d" ] || { echo "error: $d is missing — run from the repo, do not copy this script" >&2; exit 1; }
done
[ -f "$CLIENT" ] || { echo "error: $CLIENT is missing" >&2; exit 1; }

run_scan() {  # run_scan <label> <root> [<root> …]  — exit 1 on any finding
  local label="$1"; shift
  echo "scanning $* — $label"
  python3 - "$@" <<'PY'
import os, sys

# `/v1/daemon/history` is not metrics history: it is the automated-action
# timeline (watchdog / autodown actions), read by the APP client. Allowed by
# name so that "one metrics-history path" cannot quietly become "one history
# path of any kind", which would be a rule about a string, not about the data.
ALLOWED = {"/v1/cluster/history", "/v1/daemon/history"}
CANONICAL = "/v1/cluster/history"
RETIRED_FRAGMENT = "metrics/history"

def scan(src):
    """Return (code_with_comments_stripped, [(line, literal_text), ...]).

    Comments go, literals stay: the ruling is DOCUMENTED in these files (three
    of them quote the retired path in prose), and prose must not trip a guard
    that is about code. Literals are consumed atomically, so a `//` inside a
    URL string is never mistaken for a comment, an escaped quote never ends a
    literal early, and a nested literal inside `\\(...)` never desynchronises
    the scan of the rest of the file.
    """
    literals, code = [], []
    i, n, line = 0, len(src), 1
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if src.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif src.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    if src[i] == "\n":
                        line += 1
                    i += 1
            continue
        if src[i] == '"':
            start = line
            if src.startswith('"""', i):
                j = src.find('"""', i + 3)
                j = n if j < 0 else j + 3
                body = src[i:j]
                line += body.count("\n")
                literals.append((start, body))
                code.append(body)
                i = j
                continue
            j = i + 1
            while j < n and src[j] not in '"\n':
                if src[j] == "\\":
                    if src.startswith("\\(", j):        # interpolation: skip whole
                        j += 2
                        depth = 1
                        while j < n and depth:
                            if src[j] == "(":
                                depth += 1
                            elif src[j] == ")":
                                depth -= 1
                            j += 1
                        continue
                    j += 2                              # escaped character
                    continue
                j += 1
            body = src[i:j + 1] if j < n and src[j] == '"' else src[i:j]
            literals.append((start, body))
            code.append(body)
            line += body.count("\n")
            i = j + 1
            continue
        code.append(src[i])
        if src[i] == "\n":
            line += 1
        i += 1
    return "".join(code), literals


def literal_value(lit):
    """The literal's own text, quotes and raw-string hashes removed."""
    s = lit.strip()
    while s.startswith("#"):
        s = s[1:]
    while s.endswith("#"):
        s = s[:-1]
    if s.startswith('"""') and s.endswith('"""') and len(s) >= 6:
        return s[3:-3]
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    return s


findings, clients, files = [], [], 0
for root in sys.argv[1:]:
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(".swift"):
                continue
            path = os.path.join(dirpath, name)
            files += 1
            code, literals = scan(open(path, encoding="utf-8").read())
            # Leg 1 — the retired route, in any spelling code can hold.
            for num, text in enumerate(code.split("\n"), 1):
                if RETIRED_FRAGMENT in text:
                    findings.append(
                        "%s:%d: requests the RETIRED metrics-history route "
                        "(`%s` appears in code, not in a comment)"
                        % (path, num, RETIRED_FRAGMENT))
            # Leg 2 — no other history path may be named by a literal.
            for start, lit in literals:
                value = literal_value(lit)
                if "/history" in value and value not in ALLOWED:
                    findings.append(
                        "%s:%d: string literal %s is a history path that is not "
                        "the canonical `%s` (allowed set: %s) — a history series "
                        "must not be able to pick a stale endpoint"
                        % (path, start, lit, CANONICAL, ", ".join(sorted(ALLOWED))))
            if name == "WidgetDataClient.swift":
                clients.append((path, code, literals))

# Leg 3 — the canonical read must still exist and still be the one used.
if not clients:
    findings.append("WidgetDataClient.swift was not in the scanned tree, so the "
                    "canonical history read cannot be verified — the guard must "
                    "not be satisfiable by scanning less")
for path, code, literals in clients:
    if 'static let clusterHistory = "%s"' % CANONICAL not in code:
        findings.append("%s: the canonical history constant "
                        '`static let clusterHistory = "%s"` is gone' % (path, CANONICAL))
    if "WidgetDataPath.clusterHistory" not in code:
        findings.append("%s: no read goes through `WidgetDataPath.clusterHistory` "
                        "— the canonical path constant exists but nothing uses it, "
                        "so the audited allowlist is lying about the surface" % path)
    in_set = sorted({literal_value(lit) for _s, lit in literals if "/history" in literal_value(lit)})
    if in_set != [CANONICAL]:
        findings.append("%s: the audited allowlist must carry exactly ONE history "
                        "path (`%s`), it carries %s" % (path, CANONICAL, in_set))

print("  %d Swift files scanned under: %s" % (files, ", ".join(sys.argv[1:])))
if findings:
    print("FAIL: history reads must go through %s only" % CANONICAL)
    for f in findings:
        print("    %s" % f)
    sys.exit(1)
print("  ok: one canonical metrics-history path (%s), no retired or extra history "
      "read in Swift" % CANONICAL)
PY
}

rc=0
run_scan "one canonical history path" "$IOS_ROOT" "$MAC_ROOT" || rc=1

if [ "${HISTORY_ROUTE_GUARD_NEGATIVE_TEST:-0}" = "1" ]; then
  echo ""
  echo "negative proofs (each plant MUST turn this guard red):"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  plants=0
  fail=0

  plant_with_read() {  # plant_with_read <dir> <literal-to-plant>
    mkdir -p "$1/Shared"
    cp "$CLIENT" "$1/Shared/WidgetDataClient.swift"
    cat > "$1/Shared/PlantedTrendReader.swift" <<PLANT
import Foundation

// Planted by HISTORY_ROUTE_GUARD_NEGATIVE_TEST — a surface reading history off
// a path the ruling retired. The guard must not survive this.
enum PlantedTrendReader {
    static let path = "$2"
    static func read() -> String { path }
}
PLANT
  }

  # Plant FIRST, then scan. A scan of a directory that does not exist would go
  # red for the wrong reason and the proof would be worth nothing.
  plant_with_read "$TMP/mh" "/v1/metrics/history"
  plant_with_read "$TMP/extra" "/v1/other/history"
  # Third proof: the guard is not satisfiable by deleting the canonical read.
  mkdir -p "$TMP/gone/Shared"
  grep -v 'static let clusterHistory = "/v1/cluster/history"' "$CLIENT" \
    > "$TMP/gone/Shared/WidgetDataClient.swift"

  for plant in "mh:/v1/metrics/history" "extra:/v1/other/history"; do
    dir="${plant%%:*}"; lit="${plant#*:}"
    plants=$((plants + 1))
    [ -f "$TMP/$dir/Shared/PlantedTrendReader.swift" ] \
      && grep -q "\"$lit\"" "$TMP/$dir/Shared/PlantedTrendReader.swift" \
      || { echo "  NEGATIVE PROOF IS WORTHLESS: $dir holds no plant of $lit"; fail=1; continue; }
    if run_scan "PLANTED $lit (expected RED)" "$TMP/$dir/Shared"; then
      echo "  NEGATIVE PROOF FAILED: planting $lit stayed GREEN"; fail=1
    else
      echo "  ok: planting $lit turned the guard RED as it must"
    fi
  done

  plants=$((plants + 1))
  grep -q 'static let clusterHistory' "$TMP/gone/Shared/WidgetDataClient.swift" \
    && { echo "  NEGATIVE PROOF IS WORTHLESS: the canonical constant was not removed"; fail=1; }
  if run_scan "PLANTED canonical read deleted (expected RED)" "$TMP/gone/Shared"; then
    echo "  NEGATIVE PROOF FAILED: deleting the canonical read stayed GREEN"; fail=1
  else
    echo "  ok: deleting the canonical read turned the guard RED as it must"
  fi
  rc=$((rc + fail))
fi

echo ""
if [ "$rc" -eq 0 ]; then
  echo "HISTORY ROUTE GUARD PASSED — one metrics-history path in Swift, and the guard was seen to bite"
else
  echo "HISTORY ROUTE GUARD FAILED — see the findings above"
fi
exit "$rc"
