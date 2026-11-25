#!/usr/bin/env bash
# t_b94a2b39: does a CO-INSTALLED trap change what bash does with an UNtrapped fatal
# signal, and does sitting in an external foreground command change it?
set -u
arm() {  # $1=sig  $2=extra traps ("" | "TERM" | "INT" | "TERM INT")  $3=how to sit ("ext"|"wait")
  local sig="$1" extra="$2" sit="$3" out rc marker d
  out="$(mktemp)"
  (
    d="$(mktemp -d /tmp/hscc-arm2-XXXXXX)"
    trap "echo CLEANED >> '$out'; rmdir '$d' 2>/dev/null" EXIT
    echo "DIRDIR $d" >> "$out"
    for s in $extra; do trap "echo GOT_$s >> '$out'; exit 143" "$s"; done
    if [ "$sit" = ext ]; then sleep 20; else sleep 20 & wait $!; fi
  ) &
  pid=$!
  sleep 0.7
  kill "-$sig" "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null; rc=$?
  sleep 0.3
  marker="$(tr '\n' '|' < "$out" 2>/dev/null)"
  printf 'sig=%-5s traps=[%-9s] sit=%-4s rc=%-4s [%s]\n' "$sig" "$extra" "$sit" "$rc" "$marker"
  rm -f "$out"
}
arm QUIT ""     ext
arm QUIT "TERM" ext
arm QUIT "INT"  ext
arm QUIT "TERM INT" ext
arm HUP  ""     ext
arm HUP  "TERM" ext
arm PIPE ""     ext
arm KILL "TERM" ext
arm TERM "TERM" ext
echo "=== leftover arm2 dirs ==="
ls -d /tmp/hscc-arm2-* 2>/dev/null || echo none
