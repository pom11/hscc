#!/usr/bin/env bash
# Which account may REMOVE a dir in the leg-log namespace — probed, not asserted
# (t_b94a2b39).
#
# Sourced by scripts/run_tests.sh's sweep. Callable, but the useful contract is
# the SOURCED one: call `_leg_tmp_sweep_owner` (never inside `$(…)` — a command
# substitution is a subshell and would discard what it sets) and then read
#
#     _leg_tmp_probe_owner_out   the uid that owns dirs we create there
#     _leg_tmp_uid_fmt           the stat format that answers with a uid
#
# Both are empty when the namespace refused the probe, which keeps the sweep
# closed. The caller uses them for EVERY candidate, so the probe pays for itself
# once per pack.
#
# WHY A PROBE AND NOT `id -u`. The sweep skips a candidate whose owner is not us,
# which reads correctly and deletes nothing on a Linux runner whose `/tmp` is a
# mount whose owner is not the mounter — there `stat` reports uid 0 for a dir the
# calling account created itself, so every candidate, including the litter the
# sweep exists to reclaim, is filed under "another account". Measured on the
# `leg scripts` leg of PR #825: both tests that assert a dir is GONE were red and
# both guards that assert a dir SURVIVES were green, because a sweep that deletes
# nothing passes every test that expects nothing to be deleted. So the sweep
# compares against what this filesystem reports for a dir WE made in that
# namespace — the same filesystem-level statement the later `rm -rf` acts on —
# and never against `id -u` directly.
#
# WHY THE FORMAT IS PROBED TOO, AND WHY NOTHING HERE IS A `||` CHAIN. GNU
# `stat -f`/`-c` is filesystem-info mode: `stat -f %u` on Linux does not fail, it
# prints a multi-line `File: … Type: overlayfs …` block and exits 0. A
# `stat -f %u … || stat -c %u …` chain therefore keeps the prose on Linux and
# never reaches the GNU form, so the owner test fails, the age test lands on a
# non-numeric string, and the whole sweep no-ops. BSD answers `-c %u` the same
# way — not with an error, but with the full traditional listing. Both are exit 0
# and neither is a number, so every guard `continue`s and nothing is written.
# That is the second time this card was red for the same reason: a wrong answer
# from `stat` is indistinguishable from a legitimate skip when the skip is
# silent. Both formats are therefore PROBED against a path this account owns, and
# a candidate is only ever deleted when both probes answered with a number.
_leg_tmp_probe_owner_out=""
_leg_tmp_uid_fmt=""

_leg_tmp_probe_owner() {  # $1 = dir to probe, $2 = a stat-uid format that works
  mkdir -p "$1" 2>/dev/null || return 1
  local o
  o="$(stat $2 "$1" 2>/dev/null)"
  rmdir "$1" 2>/dev/null
  case "$o" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$o"
}

_leg_tmp_sweep_owner() {
  _leg_tmp_probe_owner_out=""
  _leg_tmp_uid_fmt=""
  local _me _owner _probe_target _f
  _me="$(id -u)" || return 1
  # Which one-argument uid format does this host's stat answer with OUR number?
  # Probed against $HOME — a path this account owns by definition, on both
  # flavours — and the answer must equal our own uid, so prose can never win.
  _probe_target="${HOME:-.}"
  for _f in '-c %u' '-f %u'; do
    if [ "$(stat $_f "$_probe_target" 2>/dev/null)" = "$_me" ]; then
      _leg_tmp_uid_fmt="$_f"
      break
    fi
  done
  [ -n "$_leg_tmp_uid_fmt" ] || return 1
  _owner="$(_leg_tmp_probe_owner "/tmp/hscc-legs-owner-probe.${_me}.$$" "$_leg_tmp_uid_fmt")" || return 1
  _leg_tmp_probe_owner_out="$_owner"
  printf '%s\n' "$_owner"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _leg_tmp_sweep_owner
fi
