#!/usr/bin/env bash
# rc143_guard_selftest.sh — proves the SIGTERM-forensics trap in scripts/run_tests.sh
# (t_6bb29d46). Sleep-based ONLY: it never runs pytest, so it is safe next to a
# timing-sensitive suite running on the same host (unlike every other check here).
#
# Test A: a stand-in runner with the trap receives a per-pid SIGTERM -> log carries
#         the "RECEIVED SIGTERM" note and the shell exits 143.
# Test B: the EXACT incident shape — zsh -lic wrapper + `timeout 2400 bash <run_tests.sh>`
#         + `pkill -f "bash <run_tests.sh>"`. Asserts (1) the note lands in the log,
#         (2) the wrapper survives the sweep and reports inner rc=143 — the same
#         survival that let the 03:38 workers jump straight to run2.
#         The real runner is invoked with HSCC_TEST_PY=<sleep stub>, so no tests run.
#
# Usage: bash scripts/audits/rc143_guard_selftest.sh   (writes to a mktemp dir)
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SUT="$REPO/scripts/run_tests.sh"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/rc143-selftest.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
fail=0

# --- Test A: per-pid kill, trap-only stand-in --------------------------------
cat > "$SCRATCH/standin.sh" <<'EOF'
#!/usr/bin/env bash
CURRENT_SUITE="(startup)"
on_sigterm() {
  echo "!! run_tests.sh RECEIVED SIGTERM — during suite: $CURRENT_SUITE"
  exit 143
}
trap on_sigterm TERM
CURRENT_SUITE=sleepy; sleep 20
EOF
bash "$SCRATCH/standin.sh" > "$SCRATCH/A.log" 2>&1 &
p=$!; sleep 1; kill -TERM "$p"; wait "$p"; rcA=$?
if grep -q "RECEIVED SIGTERM" "$SCRATCH/A.log" && [ "$rcA" = "143" ]; then
  echo "TEST A: ok (note in log, rc=143)"
else
  echo "TEST A: FAIL (rc=$rcA, note=$(grep -c 'RECEIVED SIGTERM' "$SCRATCH/A.log"))"; fail=1
fi

# --- Test B: incident-shaped pkill sweep --------------------------------------
printf '#!/usr/bin/env bash\nsleep 25\n' > "$SCRATCH/stubpy"
chmod +x "$SCRATCH/stubpy"
BLOG="$SCRATCH/B.log"
zsh -lic "HSCC_TEST_PY='$SCRATCH/stubpy' timeout 2400 bash '$SUT' > '$BLOG' 2>&1; echo \"inner rc=\$?\" >> '$BLOG'" &
zp=$!
sleep 3
pkill -f "bash $SUT" 2>/dev/null   # matches the timeout parent AND the inner bash; the
                                  # zsh -lic wrapper ignores SIGTERM (incident behaviour)
wait "$zp"; sleep 1
if grep -q "RECEIVED SIGTERM" "$BLOG" && grep -q "inner rc=143" "$BLOG" \
   && ! grep -qE "^ALL GREEN|^FAILURES ABOVE" "$BLOG"; then
  echo "TEST B: ok (sweep killed inner leg, note captured, wrapper survived to report rc=143)"
else
  echo "TEST B: FAIL"; tail -5 "$BLOG"; fail=1
fi

[ $fail -eq 0 ] && echo "rc143_guard_selftest: ALL PASS" || echo "rc143_guard_selftest: FAILURES"
exit $fail
