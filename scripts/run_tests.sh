#!/usr/bin/env bash
# Run the HSCC test suite.
#
# HSCC ships SEVEN INDEPENDENT plugins (hscc-bootstrap, hscc-commands, hscc-roles,
# hscc-cluster, hscc_daemon, sparkrun-hermes, hscc-api). Their dirs are deployed
# standalone into ~/.hermes/plugins, so several are hyphenated (not importable
# package names) and each tests/conftest.py puts its OWN dir on sys.path and
# imports its module bare (`import clusterlib`, `import __init__`, `import
# fleet`). Collected together in a single pytest process those bare names
# collide in sys.modules and leak sys.path between dirs — a handful of tests
# then fail purely on collection order. Each dir is green on its own, so we run
# one pytest process PER dir (true isolation, matching how they deploy) and
# aggregate. Exit non-zero if any dir fails.
#
# Usage:  scripts/run_tests.sh [extra pytest args...]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${HSCC_TEST_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$PY" ] || PY="python3"

DIRS=(hscc-bootstrap hscc-commands hscc-roles hscc-cluster hscc_daemon sparkrun-hermes hscc-api)

rc=0
declare -a summary
for d in "${DIRS[@]}"; do
  echo "━━━ $d ━━━"
  "$PY" -m pytest -q "$ROOT/$d/tests" -p no:cacheprovider "$@"
  code=$?
  if [ $code -eq 0 ]; then
    summary+=("  ✓ $d")
  else
    summary+=("  ✗ $d (pytest exit $code)")
    rc=1
  fi
done

echo
echo "━━━ Summary ━━━"
printf '%s\n' "${summary[@]}"
[ $rc -eq 0 ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit $rc
