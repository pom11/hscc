#!/usr/bin/env python3
"""The ONE implementation of "does this content carry a real operator address
or home path".

pom11/hscc is a PUBLIC repository, so a real LAN host or tailnet address that
reaches a commit is a leak, not a typo. This module is the detector; it has TWO
callers and must stay the single source of truth for the pattern:

  * ``hscc_daemon/tests/test_no_real_addresses_committed.py`` — the pytest gate,
    which scans everything git *tracks*.
  * ``.githooks/pre-commit`` — the commit-time gate (``--staged``), which scans
    the files in the index. Needed because the pytest gate only fires when
    somebody runs the suite: the 2026-10-08 leak (a ledger tick that recorded a
    verbatim ``mount_nfs`` command) was a docs-only commit that never ran it.

Detection lives HERE, not in either caller — a second regex is a second
opportunity to get it wrong, and the two gates must never disagree about what
is a leak.

Placeholders are the documented convention and are ALLOWED:
  * ``10.0.0.x``   for a LAN node
  * ``100.64.0.1`` for a tailnet host

An absolute home path is the SAME class of disclosure and is carried by the SAME
pattern (t_3480956c). It got through PR #724 precisely because this module had no
home-path concept: a reviewer grepping for addresses — or running this guard —
could not see ``/Users/<real-user>/.hermes/...`` any better than a gate could.

  * ``/Users/<x>/...`` and ``/home/<x>/...`` name the operator's machine and
    account, so every *concrete* user is a leak. The rule is deliberately NOT
    "the operator's username" *as a literal*: banning every concrete user is
    strictly stronger for a path, and writing the real name into this file would
    publish it in the guard itself.

  * ``/Users/<operator>`` is the documented replacement (the repo's own redaction
    prose already uses it) and is allowed BY CONSTRUCTION — ``<`` and ``>`` are not
    path-name characters, so the pattern cannot match it.
  * ``/Users/hscc-fixture/...`` is the SANCTIONED FIXTURE USER: the home-path
    analogue of the fixture block below. A test that must build a POSIX-looking
    path uses that name, never the operator's, and never by exempting a directory.
  * ``/home/runner`` is allowed as well: GitHub's own ephemeral CI account. It
    appears in CI-shape code (``scripts/hermetic_spawn_guard.py``) and in a Linux
    runner's pytest tmp paths, and names no operator machine.

Home offenders are BLOCKING (``HOME_PATHS_ENFORCE``, flipped by t_34de41c3 once
the tracked-tree baseline was measured and scrubbed). They were advisory at
first, for the reason ``ADDRESS_GUARD_ENFORCE`` documents for the CI job: a rule
that turns the suite red the moment it lands blocks the merge queue instead of
fixing the files.

A name written where NO home root precedes it is the fourth class (t_25f7a806), and
no path rule can reach it: ``no crontab for <user>``, a database role
``<user>_hermes``, and the sharpest of them a path flattened into a slug,
``-Users-<user>`` — where the literal ``/Users/`` never occurs, so ``git grep
"/Users/"`` and this module's home branch both read the line as clean. It is
detected by IDENTITY, never by literal: the name is read from the runtime
environment, so no tracked byte of this file carries it and the control cannot
publish what it does not hold. The class is reported WITHOUT the name for the same
reason — see ``OPERATOR_IDENTITIES_ENFORCE`` and the identity section below.

Identity offenders are BLOCKING (``OPERATOR_IDENTITIES_ENFORCE``, flipped by
t_d42e6d48 once ``blocking()`` over the tracked tree measured 0 on the host whose
identity the branch derives). They were advisory at landing, for the same reason
the home class was: a rule whose teeth depend on the machine running it must be
measured there before it can fail the merge queue.

100.64.0.0/10 is CGNAT, which is where Tailscale hands out addresses, so a real
tailnet address lives somewhere in it. Rather than ban the whole range — tests
legitimately need a tailnet-shaped host — 100.64.0.0/24 is reserved as the
SANCTIONED FIXTURE BLOCK (100.64.0.1, 100.64.0.3, ...). Anything else in CGNAT,
or anything on the live LAN, is a real address and must not be committed.

The operator's LAN moved to the 10.0.0.x range, so a concrete 10.0.0.N is now
a real address. Only the documented ``10.0.0.x`` placeholder is allowed; the
older 192.168.88.x branch stays banned because published history still carries it.

Stdlib only, no network, no config files: the pre-commit hook runs this on every
commit, so anything it costs gets paid by every commit in the repo.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

LAN_PLACEHOLDER = "10.0.0.x"
TAILNET_PLACEHOLDER = "100.64.0.1"
HOME_PLACEHOLDER = "/Users/<operator>"

# The sanctioned fixture users (t_3480956c), the home-path analogue of the
# 100.64.0.0/24 fixture block below. ONE source of truth: the allowance is built
# FROM this set, so a name cannot be added to the list without the pattern that
# honours it changing in the same diff.
#
#   hscc-fixture  the name a test uses when it must build a POSIX-looking path
#   runner        GitHub's own ephemeral CI account (CI-shape code, and a Linux
#                 runner's pytest tmp paths); it names no operator machine
FIXTURE_HOME_USER = "hscc-fixture"
CI_HOME_USER = "runner"
SANCTIONED_HOME_USERS = frozenset({FIXTURE_HOME_USER, CI_HOME_USER})

# The address branch, verbatim from before t_3480956c. The dotted fragments are
# pinned by test_forbidden_pattern_is_defined_in_exactly_one_tracked_file.
_ADDRESS_BRANCH = (
    r"\b(?:"
    r"100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # CGNAT above 100.64
    r"|100\.64\.(?!0\.)\d{1,3}\.\d{1,3}"                          # 100.64.x, x != 0
    r"|192\.168\.88\.\d{1,3}"                                     # the old live LAN
    r"|10\.0\.0\.\d{1,3}"                                          # the live LAN
    r")\b"
)

# The home-path branch. No ``\b`` on either side, and it must not have one:
# ``\b`` before ``/`` requires a word character in front of the slash, so a quoted
# ``"/Users/<user>"`` or a path after ``(`` would dodge the match — the same
# boundary trap that makes the text scan blind inside a `.pyc` (see
# docs/audits/address-guard-binary-policy-t_3e6d3db7.md).
_HOME_USER_CHARS = "A-Za-z0-9._-"
# Deliberately strict, and one consequence is worth knowing before writing prose
# here: `.` is a user-name character, so a dotted ellipsis after a home root reads
# as a user named `...` and is reported. The scrub writes the placeholder, not an
# ellipsis, and this file's own comments must not plant findings either — the
# pre-commit hook reported three of them on the first commit of this card.
# Where a path stops being one: whitespace and the delimiters a shell, a Markdown
# link or a JSON string puts around it. Truncating here is safe — the offence is
# the ``/Users/<user>`` prefix, and a shorter match never UNDER-reports.
_HOME_TAIL_STOP = r"\s'\"`<>|;,)()\[\]{}"

# A user-name component must START alphanumeric. This kills the two shapes the
# strictness above manufactures as findings — a prose ellipsis (`/Users/` plus
# `...`) and a dotfile mistaken for an account (`/home/.local/bin` in a test's
# docstring) — and no real POSIX account name begins with `.`.
#
# It is a LOOKAHEAD, and that is the whole reason it is allowed here. A lookahead
# can only shorten or decline a match at a position the engine already reached;
# it cannot stop the engine from *reaching* the `/Users/` that starts the offence.
# A left-boundary LOOKBEHIND (`(?<![A-Za-z0-9._-])`) was measured over the tracked
# tree on t_34de41c3 as the fix for three relative-path false positives, and it
# costs a blind spot: a home path written after a `\n` escape inside a string
# literal (`".../bin/zsh\n/Users/<user>/.ssh/id_rsa"`) has a word char in front of
# the slash in the FILE BYTES, so the lookbehind declines it and every gate — the
# hook, the pytest gate and the CI backstop — passes a real leak. That is the
# `.pyc` boundary trap one layer up, and the same reason this branch refuses `\b`.
# Pinned by test_a_newline_escaped_home_path_still_trips_the_branch.
_HOME_USER_START = r"(?=[A-Za-z0-9])"


def _unsanctioned_home_lookahead():
    """Lookaheads that skip a sanctioned fixture user and match every other one.

    ``(?!<name>(?!<user chars>))`` rejects the name only when it is the WHOLE
    user component, so the fixture name alone is allowed while the fixture name
    plus a suffix is not — an allowlist that silently widened to any name
    *starting with* a fixture would be the exemption-by-prefix this card
    refuses to write.
    """
    return "".join(
        "(?!" + re.escape(name) + "(?![" + _HOME_USER_CHARS + "]))"
        for name in sorted(SANCTIONED_HOME_USERS)   # sorted: the pattern is text
    )


_HOME_BRANCH = (
    r"(?:/Users|/home)/"
    + _unsanctioned_home_lookahead()
    + _HOME_USER_START
    + "[" + _HOME_USER_CHARS + "]+"
    r"(?:/[^" + _HOME_TAIL_STOP + r"]*)?"
)

# ── the operator-identity class (t_25f7a806) ─────────────────────────────────
#
# The fourth spelling of the class this module exists for: the operator's own short
# name written where no home root precedes it. Measured on origin/main before this
# card: 21 occurrences in 8 tracked files, and `FORBIDDEN` saw 0 of them (the three
# published shapes each miss — `no crontab for <user>`, `<user>_hermes`, and the
# slug form `-Users-<user>`). Reasoning and the rejected options:
# docs/audits/operator-identity-decision_t_25f7a806.md.
#
# WHY this may be detected when t_3480956c refused to. That card refused to write
# the LITERAL into the guard — "publishing it in the security control itself" — and
# an enumerated deny-list of operator names carries that cost plus a worse one: it
# only ever covers the machines somebody has already named. Neither applies to a
# name READ FROM THE RUNTIME ENVIRONMENT. No tracked byte of this file holds a
# username, so the control cannot publish what it does not hold, and the rule covers
# whoever runs it rather than whoever was named when it was written.
#
# The honest price, stated where a reader will find it: the verdict is
# machine-dependent. On a GitHub-hosted runner the derived name is `runner`, which
# is sanctioned, so CI stays blind to this class exactly as it always was. The teeth
# are where a human commits — the local hook and the local suite, on the machine
# whose identity is actually at risk.
IDENTITY_PLACEHOLDER = "<operator>"

# A name that describes a role rather than a person, and the length below which a
# name cannot carry identity. Both exist so the class declines itself rather than
# crying wolf: on a machine owned by `root` or `test`, a rule that flagged every
# occurrence of that word would train everyone to reach for `--no-verify`, which is
# the failure mode this repo has already been paid for twice.
GENERIC_ACCOUNT_NAMES = frozenset({
    "admin", "administrator", "bin", "build", "builder", "ci", "daemon", "demo",
    "dev", "docker", "guest", "hermes", "hscc", "info", "jenkins", "main",
    "mysql", "nginx", "nobody", "operator", "owner", "postgres", "root", "runner",
    "runneradmin", "sample", "service", "shared", "support", "sys", "system",
    "team", "test", "tester", "ubuntu", "user", "vagrant", "www", "www-data",
})
MIN_IDENTITY_NAME_LEN = 4


def runtime_identity_names():
    """The names of whoever is running the guard, minus the ones that name nobody.

    Three sources, not one: `Path.home().name` is what a home path and a flattened
    slug both embed, while `USER`/`LOGNAME` still name the operator in a container
    that was given HOME=/ . The filters are the class declining itself — a service
    account, a sanctioned fixture name, or a token too short to be a name.
    """
    candidates = set()
    for who in (os.environ.get("USER"), os.environ.get("LOGNAME")):
        if who:
            candidates.add(who)
    try:
        home = Path.home()
    except (OSError, RuntimeError, KeyError):        # HOME unset or unusable
        home = None
    if home is not None and home.name:
        candidates.add(home.name)
    return frozenset(
        n for n in candidates
        if len(n) >= MIN_IDENTITY_NAME_LEN
        and n.lower() not in GENERIC_ACCOUNT_NAMES
        and n not in SANCTIONED_HOME_USERS
    )


def identity_branch_for(names):
    """The identity branch's regex TEXT for ``names``, or None when there are none.

    The delimiters matter both ways. ``(?<![A-Za-z0-9])`` / ``(?![A-Za-z0-9])`` make
    the name a NAME: `desk` + name and name + `y` are different words and must not
    trip, while `_`, `-`, `/`, `.` and whitespace may sit on either side, because
    that is exactly how a name reaches an identifier (`name_hermes`), a flattened
    path (`-Users-name`) and a shell fragment (`for name`).

    A left boundary is ALLOWED here and REFUSED for the home branch, and the
    difference is worth writing down because the home card's reasoning is emphatic:
    for a path a glued prefix does not change the fact the path is there (a `\\n`
    escape leaves a word char in front of the slash in the file bytes, and a
    lookbehind would blind every gate to a real leak); for a name a glued prefix DOES
    change the fact — `xname` is not the operator's name. So the boundary buys the
    false-positive floor this rule needs at no cost in coverage.

    `(?i:...)` is scoped to this branch on purpose. A global IGNORECASE would widen
    `/Users|/home` as a side effect, and inside ONE pattern a new branch has to earn
    its keep without moving another branch's semantics.
    """
    ordered = sorted(names)                          # sorted: the pattern is text
    if not ordered:
        return None
    return (r"(?i:(?<![A-Za-z0-9])(?:"
            + "|".join(re.escape(n) for n in ordered)
            + r")(?![A-Za-z0-9]))")


IDENTITY_NAMES = runtime_identity_names()


def forbidden_pattern(names=IDENTITY_NAMES):
    """THE pattern text — one definition, so a test can compile the same rule for an
    injected name instead of carrying a copy of it (a second regex is a second
    opportunity to get it wrong, and the CI redactor loads this one and nothing else).

    The default is bound at import time because it is the value `FORBIDDEN` itself
    was compiled from; `test_the_shipped_identity_branch_is_the_one_the_pattern_uses`
    pins that the two never drift.
    """
    text = _ADDRESS_BRANCH + "|" + _HOME_BRANCH
    branch = identity_branch_for(names)
    return text + ("|" + branch if branch else "")


FORBIDDEN = re.compile(forbidden_pattern())


def identity_lookup(names):
    """The post-pattern classifier for ``names`` — anchored whole, or None.

    NOT a second detector. It is applied only to text `FORBIDDEN` has already
    matched, and a name has no shape to match by, which is why this compares rather
    than parses. The `\\A...\\Z` anchors are load-bearing: an unanchored classifier
    could widen a match on its own and re-open the exemption-by-prefix this repo
    already refuses for the fixture allowlist.
    """
    ordered = sorted(names)
    if not ordered:
        return None
    return re.compile(r"\A(?:" + "|".join(re.escape(n) for n in ordered) + r")\Z",
                      re.IGNORECASE)


IDENTITY_LOOKUP = identity_lookup(IDENTITY_NAMES)


# Which class a FORBIDDEN match belongs to. Anchored, and applied to the match
# text only: `report()` has to explain an address and a home path differently, and
# re-parsing ``rel:line: value`` to find out would be wrong the first time a path
# contains a colon (measured to exist in the t_ec2c2f95 probe battery).
_HOME_MATCH = re.compile(r"\A(?:/Users|/home)/")

# Binary/vendored paths that would only produce noise.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")

# Build artefacts: refused outright, whatever their content (t_3e6d3db7).
#
# Why a *refusal* and not an extract-and-scan. The guard used to treat any
# NUL-bearing blob as out of scope, so a committed `.pyc` was invisible to the
# hook, the pytest gate and the CI backstop alike -- and a `.pyc` embeds the
# string constants of the source it was compiled from, which is exactly where a
# real address lands (this repo had one in published history:
# `hscc-provision/__pycache__/hscc.cpython-313.pyc`, removed by the 2026-10-09
# rewrite). `.gitignore` cannot help once the blob is tracked: a fresh clone and
# a CI checkout get what was committed.
#
# Extract-and-scan is not merely slower and noisier than this -- for the exact
# blob class in question it is measurably BLIND. A real `py_compile` output of
# source carrying a real-shaped address has that address visible to a naive
# `192\.168\.88\.\d{1,3}` over the decoded bytes, and INVISIBLE to FORBIDDEN:
# FORBIDDEN's `\b` boundaries assume a delimited (text-shaped) occurrence, and
# in a marshal'd code object the byte immediately AFTER the string constant is
# the first char of the next interned name -- a word character -- so the
# trailing `\b` fails. Pinned by
# test_a_genuine_compiled_pyc_defeats_the_text_detector_so_refusal_is_required;
# the refusal never reads the bytes, so it cannot be defeated that way.
#
# It would also have no stopping rule -- a `.pyc` is a container, but so are
# `.zip`, `.gz`, `.jar` and PDFs with embedded fonts -- and every layer is code
# paid for on every commit in the repo, which is the thing that gets
# `--no-verify`'d. Full reasoning + measurements:
# docs/audits/address-guard-binary-policy-t_3e6d3db7.md
BUILD_ARTEFACT_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".o", ".a", ".so", ".dylib", ".dll",
    ".class", ".jar", ".egg", ".whl", ".pyz",
)
BUILD_ARTEFACT_PATH_SEGMENTS = ("__pycache__",)

# The escape hatch for a binary that genuinely belongs in this repo. It is
# deliberately WEAKER than it looks, and the reason is this card's own §1b
# finding (docs/audits/address-guard-binary-policy-t_3e6d3db7.md): the text scan
# is BLIND to a compiled blob, so a hatch that could waive the refusal of a
# compiled artefact would waive detection with it. Hence two tiers:
#
#   * a path whose SUFFIX is in BUILD_ARTEFACT_SUFFIXES is refused no matter what
#     this list says -- a compiled artefact is never waivable, so widening the
#     hatch can never hide a leak;
#   * everything else the refusal covers (a `__pycache__/` segment, a path with an
#     unknown extension that is NUL-bearing) may be waived, and such a path is
#     then decoded and text-scanned even when it carries NUL bytes -- it stays
#     open to the one detector that works on non-compiled bytes.
#
# Consequence to weigh before widening: a prebuilt `.so` can never be tracked
# here. Today's tracked binary population is two `.png` files, so that costs
# nothing; if a real need appears, add a *non-artefact* extension rather than
# reaching for this list. Empty today, and it lives in the one file the guard's
# tests pin, so widening it is a visible diff in a security control rather than a
# config edit.
ALLOWED_BINARY_PATHS = frozenset()


class GuardError(RuntimeError):
    """The guard could not run (no git, unreadable repo). Not a leak verdict."""


# ── pure detection ───────────────────────────────────────────────────────────

def all_hits(text):
    """EVERY match on every line — the gate's view (``scan_blob`` builds from this).

    Not the same function as ``find_in_text``, and the difference is load-bearing
    now that one class is advisory: with ``first match wins`` a line holding a home
    path *and* a real LAN address reported only the home path, so the *blocking*
    address left the gate unseen and the commit passed. Pinned by
    test_an_address_never_hides_behind_a_home_path_on_the_same_line.
    """
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        hits.extend((i, m.group(0)) for m in FORBIDDEN.finditer(line))
    return hits


def find_in_text(text):
    """Return [(line_no, matched_address), ...], at most one hit per line.

    The pre-existing contract, restored deliberately: this builds ``file:line``
    findings, and ``scripts/ledger_scrub.py`` imports it on the assumption of one
    hit per line (its own ``test_count_is_per_address_not_per_line`` pins exactly
    that, and counts every address on the line separately via ``FORBIDDEN.subn``).
    The guard's gates use ``all_hits``; do not "unify" the two and silently change
    what the ledger tick records.

    Both classes come back in one list, because the pattern is ONE regex and a
    caller that wants the whole verdict must not run two searches and merge them.
    ``is_home_occurrence`` says which class a match is.
    """
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        m = FORBIDDEN.search(line)
        if m:
            hits.append((i, m.group(0)))
    return hits


def is_home_occurrence(matched):
    """True if a FORBIDDEN match is an absolute home path, false otherwise."""
    return bool(_HOME_MATCH.match(matched))


def is_identity_occurrence(matched):
    """True if a FORBIDDEN match is the runtime identity's own name.

    Classification AFTER the pattern, by comparison, never by re-parsing the
    offender — and a name has no shape to match by, so there is nothing else to
    compare against. Returns False on a host where the class declined itself.
    """
    return bool(IDENTITY_LOOKUP.match(matched)) if IDENTITY_LOOKUP else False


def address_hits(text):
    """The address-only view of ``all_hits``.

    For the places that must reason about an *address* specifically while another
    class is still advisory: ``report``'s fix text, and the compiled-`.pyc` test,
    whose blob legitimately carries its own ``co_filename`` — which on a Linux
    runner lives inside that user's home directory, and is not the thing that test
    is blind to.
    """
    return [(i, v) for i, v in all_hits(text)
            if not is_home_occurrence(v) and not is_identity_occurrence(v)]


def home_hits(text):
    """The home-path-only view of ``all_hits``."""
    return [(i, v) for i, v in all_hits(text) if is_home_occurrence(v)]


def identity_hits(text):
    """The operator-identity-only view of ``all_hits``."""
    return [(i, v) for i, v in all_hits(text) if is_identity_occurrence(v)]


# ── the home class: BLOCKING (t_34de41c3) ────────────────────────────────────
#
# This was advisory since t_3480956c landed, because the tracked tree carried
# concrete home paths in 180+ files and a hard rule turns
# `test_no_real_addresses_committed.py` red the moment it lands — which freezes
# the merge queue (it runs that suite), including the cards that would have
# cleared the baseline. t_34de41c3 measured the baseline at 198 offenders / 87
# files, classified every one of them (docs/audits/home-path-enforcement-
# decision_t_34de41c3.md), and scrubbed all 198: generic fixture users to
# `hscc-fixture`, the fleet's real service account to `<ssh_user>`, prose about
# the operator's machine to `/Users/<operator>`, and the shapes that were never
# accounts (a prose ellipsis, a dotfile) by tightening this branch instead of
# the files. `scan_tracked` on the tree that flips this returns 0.
#
# The knob below stays, and it is the only way to prove the rule bites before a
# change to it ships: `HSCC_ADDRESS_GUARD_HOME_ENFORCE=1` on a tree with a
# planted path must exit 1. Do not "temporarily" set HOME_PATHS_ENFORCE back to
# False to get a suite green — an advisory home class is how the PR #724 class
# reached origin, and a rule nobody can turn off is also a rule nobody notices
# when it silently stops matching.
HOME_PATHS_ENFORCE = True
HOME_PATHS_ENFORCE_ENV = "HSCC_ADDRESS_GUARD_HOME_ENFORCE"

# Recognisable tail on a home offender, so `report()` can explain the two classes
# apart and `blocking()` can tell them apart WITHOUT re-parsing the offender
# (an offender is `rel:line: value` and a tracked path may contain a colon).
HOME_PATH_OFFENDER_SUFFIX = f"absolute home path — scrub to {HOME_PLACEHOLDER}"


def home_enforced():
    """Whether home-path offenders block. Read at call time, not import time."""
    if os.environ.get(HOME_PATHS_ENFORCE_ENV, "").strip().lower() in (
            "1", "true", "yes", "on"):
        return True
    return bool(HOME_PATHS_ENFORCE)


def home_offender(rel, line, matched):
    """The offender string for a home path — one formatter, both gates."""
    return f"{rel}:{line}: {matched} — {HOME_PATH_OFFENDER_SUFFIX}"


def is_home_offender(offender):
    """True for an offender produced by ``home_offender``."""
    return offender.endswith(HOME_PATH_OFFENDER_SUFFIX)


# ── the operator-identity class: ENFORCED (t_25f7a806 → flipped by t_d42e6d48) ─
#
# Advisory at landing, enforced now, and the flip was taken on a measurement rather
# than on a hope: `scan_tracked` / `blocking()` over every tracked file, run on the
# host whose identity this branch derives (so the branch is LIVE, not inert), on the
# tree that actually flips — `origin/main` merged with the #829 branch, because the
# tree that landed #829 alone is not the tree the merge queue will test. Result:
# 0 offenders in 2571 tracked files, so `blocking()` == 0 and the CLI exits 0 with an
# empty report. That is the same gate `HOME_PATHS_ENFORCE` documents: a rule that
# turns the suite red the moment it lands freezes every merge, because the merge
# queue runs this suite on the operator's own host where this class has teeth.
#
# The honest asymmetry that stays after the flip: the verdict is machine-dependent.
# On a GitHub-hosted runner the derived name is `runner`, which is sanctioned, so CI
# stays blind to this class — the teeth are on the machine that commits. And a name
# that is NOT the operator's (a third party's, in someone else's captured output) is
# invisible by construction; no shape rule can catch it without the enumerated
# deny-list the decision doc rejects.
#
# The env knob may ONLY STRENGTHEN, exactly like the home knob: an exported variable
# in CI must never be able to mute a class, and `test_the_identity_knob_may_only_
# strengthen` pins the enforced-plus-falsy-env case, which is the one that would
# quietly turn a hard rule off.
OPERATOR_IDENTITIES_ENFORCE = True
OPERATOR_IDENTITIES_ENFORCE_ENV = "HSCC_ADDRESS_GUARD_IDENTITY_ENFORCE"

# Recognisable tail on an identity offender, same reason as the home suffix:
# `report()` and `blocking()` classify by `str.endswith` instead of re-parsing
# `rel:line: value`, which a tracked path containing a colon would break.
IDENTITY_OFFENDER_SUFFIX = f"operator identity — scrub to {IDENTITY_PLACEHOLDER}"


def identity_enforced():
    """Whether identity offenders block. Read at call time, not import time."""
    if os.environ.get(OPERATOR_IDENTITIES_ENFORCE_ENV, "").strip().lower() in (
            "1", "true", "yes", "on"):
        return True
    return bool(OPERATOR_IDENTITIES_ENFORCE)


def identity_offender(rel, line):
    """The offender string for an operator name — and it carries NO name.

    This is the reason the class may live inside `FORBIDDEN` at all. The CI redactor
    masks the public job log with the pattern *its own environment derives*, so a
    report generated on the operator's machine and redacted somewhere else — the
    pattern crosses to it over a pipe, from a different host — would have no way to
    see the value in the text. An offender that never embeds the match does not
    depend on that coincidence: the detector knows the name, the report never prints
    it. `test_an_identity_offender_never_carries_the_name_it_matched` pins it.
    """
    return f"{rel}:{line}: {IDENTITY_PLACEHOLDER} — {IDENTITY_OFFENDER_SUFFIX}"


def is_identity_offender(offender):
    """True for an offender produced by ``identity_offender``."""
    return offender.endswith(IDENTITY_OFFENDER_SUFFIX)


def _is_advisory_now(offender):
    """True for a class that is reported but must not fail a gate right now."""
    if is_home_offender(offender):
        return not home_enforced()
    if is_identity_offender(offender):
        return not identity_enforced()
    return False


def blocking(offenders):
    """The offenders that must fail a commit / the suite / the CI job.

    Addresses and build artefacts always. A home path or an operator identity only
    once its class is enforced, so the two gates and the pytest assertion cannot
    disagree about the same tree: they all ask THIS function, in the same
    environment.
    """
    return [o for o in offenders if not _is_advisory_now(o)]


def advisory(offenders):
    """The offenders reported but not failed on (a class not yet enforced)."""
    return [o for o in offenders if _is_advisory_now(o)]


def is_build_artefact(rel):
    """True if ``rel`` is a build artefact that must never be tracked here.

    Name-based, deliberately: it must be decidable WITHOUT reading the file, so
    the verdict costs nothing on the commit path and applies even to a path whose
    bytes are unreadable or absent from the working tree. A compiled artefact
    renamed to a text extension is not caught — see the limits section of
    docs/audits/address-guard-binary-policy-t_3e6d3db7.md.

    The suffix tier runs FIRST and ignores ``ALLOWED_BINARY_PATHS`` on purpose
    (round-1 review of this card): the text scan is blind to a compiled blob, so
    if the hatch could waive a `.pyc`/`.so` refusal it would waive detection too,
    and the guarantee "widening the hatch can never hide a leak" would be false.
    Only the segment tier is waivable.
    """
    lowered = rel.lower()
    if lowered.endswith(BUILD_ARTEFACT_SUFFIXES):
        return True                                  # never waivable
    if rel in ALLOWED_BINARY_PATHS:
        return False                                 # segment tier only
    # Git always reports paths with forward slashes; a backslash is matched too
    # so a Windows-authored `pkg\__pycache__\x.pyc` cannot dodge the segment test.
    normalised = lowered.replace("\\", "/")
    return any(seg in normalised.split("/") for seg in BUILD_ARTEFACT_PATH_SEGMENTS)


# The verdict text for a refused build artefact. A constant, not an inline
# literal, because `artefact_offender` writes it and `report` has to recognise it
# to sort offenders into the two classes it explains differently.
BUILD_ARTEFACT_OFFENDER_SUFFIX = "build artefact must not be tracked"


def artefact_offender(rel):
    """The offender string for a refused build artefact, or None.

    One formatter for both gates: `scan_blob` (staged) and `scan_paths` (tracked)
    must name an artefact identically, or the hook and the CI job disagree about
    the same tree -- the exact failure this repo keeps being reminded of.
    """
    if is_build_artefact(rel):
        # Line 0: there is no line to point at. The offence is the path itself.
        return f"{rel}:0: {BUILD_ARTEFACT_OFFENDER_SUFFIX}"
    return None


def is_skipped_asset(rel):
    """A declared asset the guard does not decode (`.png`, `.zip`, ...).

    ONE predicate for both gates: `scan_paths` has a pre-read skip shortcut and
    `scan_blob` has the same rule after the read, and if those two spellings ever
    drift the hook and the CI job start disagreeing about the same tree. So both
    call this. It is also the reason the hatch belongs here and not in the
    callers: an allowlisted path must never be skipped unread by either gate,
    whatever extension it has.
    """
    return rel not in ALLOWED_BINARY_PATHS and rel.endswith(SKIP_SUFFIXES)


def scan_blob(rel, data):
    """Scan one file's bytes; return offender strings ``rel:line: address``.

    Bytes (not str) so the same function serves a working-tree read and a staged
    blob read. Four cases, in this order:

      * a build artefact is refused on its NAME, before anything is decoded
        (t_3e6d3db7), and a compiled-artefact SUFFIX is refused even if it sits on
        ``ALLOWED_BINARY_PATHS`` — the text scan below cannot see an address in a
        compiled blob, so waiving the refusal would waive detection;
      * a path on ``ALLOWED_BINARY_PATHS`` (segment tier only, per the previous
        bullet) is exempt from the refusal and is therefore decoded and scanned
        EVEN IF it carries NUL bytes and even if its extension is in
        ``SKIP_SUFFIXES`` — the hatch may say "this path may be tracked", never
        "this path may be unread" (round-1 review: an allowlisted `.pdf` used to
        short-circuit here and go unscanned);
      * anything else whose extension is in ``SKIP_SUFFIXES`` is skipped — declared
        assets the guard has no business decoding;
      * anything else with a NUL byte is treated as binary and skipped — same
        intent as SKIP_SUFFIXES, and it keeps a vendored asset from being decoded.
    """
    offender = artefact_offender(rel)
    if offender:
        return [offender]
    # Two ways a blob stays unread -- a declared asset (`is_skipped_asset`, which
    # already exempts the hatch) and an unknown NUL-bearing blob (exempted here).
    # The hatch may say "this path may be tracked", never "this path may be unread".
    if is_skipped_asset(rel) or (rel not in ALLOWED_BINARY_PATHS
                                 and not isinstance(data, str) and b"\0" in data):
        return []
    if not isinstance(data, str):
        data = data.decode("utf-8", errors="ignore")
    return [_offender(rel, line, matched) for line, matched in all_hits(data)]


def _offender(rel, line, matched):
    """Format one hit as an offender, naming the class whose fix differs.

    ONE dispatcher, so `scan_blob` (staged) and `scan_paths` (tracked) can never
    name a class differently — the hook and the CI job disagreeing about the same
    tree is the failure this module keeps being paid for. The identity arm
    deliberately drops the match on the floor: see ``identity_offender``.
    """
    if is_home_occurrence(matched):
        return home_offender(rel, line, matched)
    if is_identity_occurrence(matched):
        return identity_offender(rel, line)
    return f"{rel}:{line}: {matched}"


def scan_paths(root, rels, read=None):
    """Scan ``rels`` (relative to ``root``) from disk. Missing/unreadable files
    are skipped, matching the pytest gate (a deleted-but-tracked path is not a
    leak, and an unreadable file cannot be judged) -- EXCEPT a build artefact,
    which is refused on its name alone: a path in HEAD's index is a tracked
    artefact whether or not the working tree happens to carry the bytes."""
    root = Path(root)
    read = read or (lambda p: p.read_bytes())
    offenders = []
    for rel in rels:
        offender = artefact_offender(rel)
        if offender:
            offenders.append(offender)
            continue
        if is_skipped_asset(rel):
            continue
        p = root / rel
        if not p.is_file():
            continue
        try:
            blob = read(p)
        except OSError:
            continue
        offenders.extend(scan_blob(rel, blob))
    return offenders


# ── git plumbing ─────────────────────────────────────────────────────────────

def _git(repo, args, *, input_bytes=None, check=True):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo)] + args,
            capture_output=True, input=input_bytes,
        )
    except OSError as exc:  # git not on PATH
        raise GuardError(f"git is not runnable ({exc})") from exc
    if check and proc.returncode != 0:
        raise GuardError(
            "git " + " ".join(args) + f" failed: {proc.stderr.decode('utf-8', 'ignore').strip()}"
        )
    return proc


def tracked_paths(repo):
    """Every path git tracks in HEAD's index (the pytest gate's population)."""
    out = _git(repo, ["ls-files", "-z"]).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def staged_paths(repo):
    """Paths ADDED/COPY/MODIFIED/RENAMED/TYPED-CHANGE in the index.

    Deletions are excluded on purpose: a removed file cannot introduce a new
    leak, and its staged blob does not exist so it could not be read anyway.
    """
    if _git(repo, ["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode != 0:
        # No HEAD yet: the whole index is the "staged tree".
        out = _git(repo, ["ls-files", "--cached", "-z"]).stdout
        return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    out = _git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", "HEAD"],
    ).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def staged_blobs(repo, rels):
    """Return {rel: bytes} for the STAGED content of ``rels``.

    Reads the INDEX (``:path`` specs), never the working tree, via ONE
    ``cat-file --batch`` subprocess. This is the whole point of the hook: the
    2026-08-30 audit's pre-push check grepped the working tree, which had already
    been scrubbed while the committed blob still carried the address.

    ``--batch`` eats one spec per LINE, so a path containing a newline would be
    split into two bogus specs and silently skipped — an evasion. Those names
    (git allows them) go through a per-path ``cat-file blob`` instead, where the
    path is a single argv element and cannot be split. Normal names keep the
    single-batched read, so the common case stays one subprocess.
    """
    if not rels:
        return {}
    batchable = [r for r in rels if "\n" not in r]
    awkward = [r for r in rels if "\n" in r]
    blobs = {}

    if batchable:
        specs = b"\n".join(b":" + r.encode("utf-8", "surrogateescape") for r in batchable)
        buf = _git(repo, ["cat-file", "--batch"], input_bytes=specs).stdout
        i = 0
        for rel in batchable:
            nl = buf.find(b"\n", i)
            if nl < 0:
                break  # batch stream ended early (should not happen)
            header = buf[i:nl].decode("utf-8", "ignore").split()
            if len(header) == 3 and header[1] == "blob":
                size = int(header[2])
                blobs[rel] = buf[nl + 1: nl + 1 + size]
                i = nl + 1 + size + 1  # skip the record terminator
            else:
                # Unresolvable spec ("<op> <path> missing"): skip this path
                # rather than desynchronise the stream.
                i = nl + 1

    for rel in awkward:
        proc = _git(repo, ["cat-file", "blob", ":" + rel], check=False)
        if proc.returncode == 0:
            blobs[rel] = proc.stdout
    return blobs


# ── gates ────────────────────────────────────────────────────────────────────

def scan_staged(repo):
    """Offenders in the staged tree (the pre-commit gate).

    The artefact verdict is taken from `staged_paths` directly, not from the blob
    dict: `staged_blobs` legitimately has no entry for a spec git could not
    resolve, and a name-based refusal must not depend on having read the bytes.
    """
    rels = staged_paths(repo)
    offenders = [o for o in (artefact_offender(r) for r in rels) if o]
    offenders.extend(
        o
        for rel, blob in staged_blobs(repo, rels).items()
        if not is_build_artefact(rel)   # already refused by name above
        for o in scan_blob(rel, blob)
    )
    return offenders


def scan_tracked(repo):
    """Offenders across every tracked file on disk (the pytest gate)."""
    return scan_paths(repo, tracked_paths(repo))


def report(offenders, scope="staged tree"):
    """The failure text: offending file:line, then the fix for each class.

    Four classes of offender, four different fixes, so they are reported apart.
    An address is scrubbed to a placeholder; a home path to ``/Users/<operator>``;
    an operator identity is never echoed back at all; a build artefact is removed
    from the index (telling someone to "scrub to 10.0.0.x" a `.pyc` is a dead end,
    and a dead end on a commit path is how `--no-verify` gets used).
    """
    artefacts, addresses, homes, identities = [], [], [], []
    for o in offenders:
        if o.endswith(BUILD_ARTEFACT_OFFENDER_SUFFIX):
            artefacts.append(o)
        elif is_home_offender(o):
            homes.append(o)
        elif is_identity_offender(o):
            identities.append(o)
        else:
            addresses.append(o)
    homes_enforced = home_enforced()
    identities_enforced = identity_enforced()
    hard = bool(addresses or artefacts or (homes and homes_enforced)
                or (identities and identities_enforced))

    if addresses:
        header = (f"REAL OPERATOR ADDRESS in the {scope} — this repo is PUBLIC, "
                  "the commit is blocked.")
    elif artefacts:
        header = (f"BUILD ARTEFACT in the {scope} — this repo is PUBLIC, "
                  "the commit is blocked.")
    elif homes and homes_enforced:
        header = (f"OPERATOR HOME PATH in the {scope} — this repo is PUBLIC, "
                  "the commit is blocked.")
    elif identities and identities_enforced:
        header = (f"OPERATOR IDENTITY in the {scope} — this repo is PUBLIC, "
                  "the commit is blocked.")
    elif identities:
        header = (f"OPERATOR IDENTITY in the {scope} — this repo is PUBLIC. "
                  "ADVISORY: reported, not blocked — which means "
                  "OPERATOR_IDENTITIES_ENFORCE has been turned back off from the "
                  "True it ships with. Nothing in this repo asks for that; if you "
                  "are reading this on a fork, the class is a hard rule upstream.")
    else:
        header = (f"OPERATOR HOME PATH in the {scope} — this repo is PUBLIC. "
                  "ADVISORY: reported, not blocked (HOME_PATHS_ENFORCE is off "
                  "until the existing baseline is scrubbed).")

    lines = [header, ""]
    lines += [f"  {o}" for o in addresses[:20]]
    if len(addresses) > 20:
        lines.append(f"  ... and {len(addresses) - 20} more")
    if addresses:
        lines += [
            "",
            "Scrub them to the documented placeholders:",
            f"  LAN node     -> {LAN_PLACEHOLDER}",
            f"  tailnet host -> {TAILNET_PLACEHOLDER}",
        ]
    if homes:
        lines += [f"  {o}" for o in homes[:20]]
        if len(homes) > 20:
            lines.append(f"  ... and {len(homes) - 20} more")
        lines += [
            "",
            "Scrub a home path to the documented placeholder — it names the",
            "operator's machine and account, which is a leak in the same class as",
            "a real address (this is the class PR #724 reached origin through):",
            f"  any path     -> {HOME_PLACEHOLDER}",
            f"  in a test    -> /Users/{FIXTURE_HOME_USER}"
            f"   (the sanctioned fixture user; never exempt a whole directory)",
        ]
    if identities:
        lines += [f"  {o}" for o in identities[:20]]
        if len(identities) > 20:
            lines.append(f"  ... and {len(identities) - 20} more")
        lines += [
            "",
            "Scrub the name. It identifies the operator and their machine — the same",
            "disclosure as a real address — and it reached here by a spelling no path",
            "rule can see, because a path flattened into a slug keeps no home root to",
            "grep for:",
            f"  captured output -> {IDENTITY_PLACEHOLDER}",
            "  an identifier   -> a generic role name (e.g. hermes_role)",
            "  a slug path     -> drop the flattened home component",
            "  in a test       -> a token assembled at runtime, never a literal",
        ]
    if artefacts:
        lines += [
            "",
            f"BUILD ARTEFACTS tracked in the {scope} — refused whatever they contain",
            "(a .pyc embeds the string constants of its source, so it can carry an",
            "address no text scan will ever see):",
        ]
        lines += [f"  {o}" for o in artefacts[:20]]
        if len(artefacts) > 20:
            lines.append(f"  ... and {len(artefacts) - 20} more")
        lines += [
            "",
            "Untrack them:  git rm --cached <path>   (and keep them ignored; see",
            "BUILD_ARTEFACT_SUFFIXES in scripts/address_guard.py for what is refused",
            "and ALLOWED_BINARY_PATHS for the escape hatch — which can waive a path",
            "segment, never a compiled-artefact suffix).",
        ]
    if not hard:
        # Advisory only: say so where the blocked wording would otherwise be, so a
        # reader does not hunt for a block that is not happening.
        lines += [
            "",
            "Nothing here blocks this commit today. Scrub it anyway: the class is",
            "the one that leaked, and this becomes a hard failure once the existing",
            "tracked-tree baseline is scrubbed (the class's *_ENFORCE constant in",
            "scripts/address_guard.py).",
        ]
        return "\n".join(lines)
    lines += [
        "",
        "If a file genuinely must carry a real address it must not be committed"
        " to this repo at all. Do not use `git commit --no-verify` to get past"
        " this on a docs file: that is how the 2026-10-08 leak reached origin.",
    ]
    return "\n".join(lines)



def main(argv=None):
    ap = argparse.ArgumentParser(description="Block real operator addresses from commits.")
    ap.add_argument("--staged", action="store_true", help="scan the staged tree (pre-commit default)")
    ap.add_argument("--tracked", action="store_true", help="scan every tracked file on disk")
    ap.add_argument("--repo", default=None, help="repo/worktree to inspect (default: cwd)")
    args = ap.parse_args(argv)

    try:
        repo = Path(args.repo).resolve() if args.repo else Path(
            _git(".", ["rev-parse", "--show-toplevel"]).stdout.decode().strip()
        )
        if args.tracked:
            offenders, scope = scan_tracked(repo), "tracked tree"
        else:
            offenders, scope = scan_staged(repo), "staged tree"
    except GuardError as exc:
        print(f"address_guard: CANNOT RUN — {exc}", file=sys.stderr)
        return 2
    if offenders:
        print(report(offenders, scope), file=sys.stderr)
    # The exit code is the BLOCKING subset, not the whole report. The home class is
    # advisory until the tracked-tree baseline is scrubbed, and an advisory note
    # must not fail a commit, the suite or the CI job — a check that fails on
    # something the author cannot fix is the check that gets --no-verify'd.
    if blocking(offenders):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
