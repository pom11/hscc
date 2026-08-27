#!/bin/bash
# check_sources.sh — every Swift file on disk must be listed in project.yml.
#
# Why this exists: project.yml lists sources EXPLICITLY. A file added to
# Sources/ but not to the spec is simply never compiled — no error, no warning.
# Five files (the whole templates library, search, and project intents) sat
# unbuilt this way while being reported as delivered. Xcode surfaced it only as
# "Cannot find 'TemplatesView' in scope" at the first use site.
#
# A `swiftc -typecheck $(find Sources -name '*.swift')` check does NOT catch it:
# find compiles everything on disk, so it type-checks files the project never
# builds and passes while the real build fails. Check the LIST, not the disk.
#
# Usage: scripts/check_sources.sh    (exit 1 on drift)

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python3 - <<'PY'
import re, os, sys
spec = open('project.yml').read()
listed = set(re.findall(r'- path: (Sources/[^\s]+\.swift)', spec))
ondisk = {os.path.join(r, f)
          for r, _, fs in os.walk('Sources')
          for f in fs if f.endswith('.swift')}

missing = sorted(ondisk - listed)   # on disk, never compiled
ghost   = sorted(listed - ondisk)   # listed, but the file is gone

if missing:
    print("NOT COMPILED — on disk but absent from project.yml:")
    for m in missing:
        print("   ", m)
if ghost:
    print("GHOST — listed in project.yml but missing on disk (the build will fail):")
    for g in ghost:
        print("   ", g)

if missing or ghost:
    print("\nAdd the missing paths to the right target's `sources:` list in project.yml.")
    sys.exit(1)

print(f"sources in sync: {len(ondisk)} Swift files, all listed in project.yml")
PY
