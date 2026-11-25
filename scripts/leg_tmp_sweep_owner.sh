#!/usr/bin/env bash
# Which account can actually REMOVE a dir in the leg-log namespace — probed, not
# asserted (t_b94a2b39).
#
# Sourced by scripts/run_tests.sh's sweep; also callable:
#     bash scripts/leg_tmp_sweep_owner.sh
# prints one decimal uid, or nothing when the namespace refused the probe.
#
# WHY A PROBE AND NOT `id -u`. The sweep skips a candidate whose owner is not us,
# which reads correctly and deletes nothing on a Linux runner whose `/tmp` is a
# mount whose owner is not the mounter — there `stat` reports uid 0 for a dir the
# calling account created itself, so every candidate, including the litter the
# sweep exists to reclaim, is filed under "another account". Measured on the
# `leg scripts` leg of PR #825: both positive sweep tests red, the two guards
# that assert a dir SURVIVES green, because a sweep that deletes nothing passes
# every test that expects nothing to be deleted.
#
# So the sweep compares against what this filesystem reports for a dir WE made in
# that namespace — the same filesystem-level statement the later `rm -rf` acts on
# — and never against `id -u` directly. A namespace that will not let us create
# there yields an empty answer, which keeps the sweep closed.
#
# The stat FORMAT is probed too, for the same reason. GNU `stat -f`/`-c` is
# filesystem-info mode: `stat -f %u` on Linux does not fail, it prints a
# multi-line `File: … Type: overlayfs …` block and exits 0. A `stat -f %u … ||
# stat -c %u …` chain therefore keeps that block on Linux and never reaches the
# GNU form, so the owner test fails and the age test lands on a non-numeric
# string: the whole sweep no-ops. The silent `continue` made that invisible, so
# the probe is what proves the syntax on the host running it.
_leg_tmp_probe_owner() {  # $1 = dir to probe, $2 = a stat-uid format that works
  mkdir -p "$1" 2>/dev/null || return 1
  local o
  o="$("$(command -v stat)" $2 "$1" 2>/dev/null)"
  rmdir "$1" 2>/dev/null
  case "$o" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$o"
}

# Set by _leg_tmp_sweep_owner as a GLOBAL on purpose: the caller reads it back to
# ask the same question about every candidate. A `local` here silently emptied
# the caller's `stat $_uid_fmt …`, which failed, produced an empty owner, and
# skipped every candidate — the same no-op shape this helper exists to kill, and
# caught only by running the sweep for real.
_leg_tmp_uid_fmt=""

_leg_tmp_sweep_owner() {
  local _me _owner _probe_target
  _me="$(id -u)" || return 1
  # Which one-argument uid format does this host's stat answer with OUR number?
  # Probed against $HOME — a path this account owns by definition, on both
  # flavours — and the answer must equal our own uid, so a format that prints
  # prose (GNU `stat -f`, whose -f is filesystem-info mode and which exits 0)
  # can never win the probe.
  _leg_tmp_uid_fmt=""
  _probe_target="${HOME:-.}"
  for _f in '-c %u' '-f %u'; do
    if [ "$(stat $_f "$_probe_target" 2>/dev/null)" = "$_me" ]; then
      _leg_tmp_uid_fmt="$_f"
      break
    fi
  done
  [ -n "$_leg_tmp_uid_fmt" ] || return 1
  _owner="$(_leg_tmp_probe_owner /tmp/hscc-legs-owner-probe.$_me.$$ "$_leg_tmp_uid_fmt")" || return 1
  printf '%s\n' "$_owner"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _leg_tmp_sweep_owner
fi
