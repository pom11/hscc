#!/usr/bin/env bash
# t_b94a2b39: does bash's EXIT trap run when the shell dies from an UNTRAPPED signal?
# Four arms, each prints RESULT on the shell's stdout (a pipe) so a surviving
# background subshell cannot fake it.
set -u
arm() {
  local sig="$1"
  local out rc marker
  out="$(mktemp)"
  # inner shell: make a dir, install ONLY an EXIT trap, then sleep in the
  # FOREGROUND so a TERM/INT/HUP/QUIT/PIPE is delivered to the shell itself.
  (
    d="$(mktemp -d /tmp/hscc-arm-XXXXXX)"
    trap "echo CLEANED >> '$out'; rmdir '$d' 2>/dev/null" EXIT
    echo "DIRDIR $d" >> "$out"
    sleep 20 &
    wait $!
  ) &
  pid=$!
  sleep 0.7
  kill "-$sig" "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null; rc=$?
  sleep 0.3
  marker="$(cat "$out" 2>/dev/null | tr '\n' '|')"
  printf '%-6s rc=%-4s trap_output=[%s]\n' "$sig" "$rc" "$marker"
  rm -f "$out"
}
for s in TERM INT HUP QUIT PIPE KILL; do arm "$s"; done
