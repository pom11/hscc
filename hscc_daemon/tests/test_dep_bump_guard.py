"""Dep-bump guard tests: fork-branch rule text + proxy.yaml/litellm drift check.

Covers t_39d7c915:
  * hscc_daemon.dep_bump_guard — the canonical fork-branch rule (branch name,
    the never-checkout-main prohibition, the post-bump verification).
  * TestRunbookAgreesWithGuard (t_6956da89) — the OPERATOR RUNBOOK that
    duplicates those rules must keep agreeing with the canonical tuple. The
    runbook is the only surviving prose duplicate of FORK_RULE_LINES, and it
    carried the same false read-site claim as the guard until t_669b8df7 fixed
    both by hand; before this class nothing in the suite opened the file.
  * hscc_daemon.proxy_router — the pure-read drift comparator: matching /
    missing / divergent / no-proxy-config / least-busy-trap / balancer-missing
    cases, WARN-never-fails semantics, and pure-read (input hashes unchanged).
  * verify.check_proxy_router_settings — check shape + run_all registration.
  * health.check_proxy — the proxy daemon stream carries the drift verdict on
    BOTH state writes (healthy + unhealthy), and drift never changes the
    health verdict itself.
All fixtures are fake proxy.yaml / litellm_config.yaml files under tmp paths;
nothing here reads ~/.config/sparkrun or ~/.cache/sparkrun.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from hscc_daemon import proxy_router


# ── fake-file helpers ───────────────────────────────────────────────────────

PROXY_OK = """\
aliases: {}
gateways:
  litellm:
    balancing: least-in-flight
    router_settings:
      routing_strategy: simple-shuffle
# 0.4.0 ignores this duplicate; it stays in the live file for the 0.3.10
# rollback line (see docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md).
router_settings:
  routing_strategy: simple-shuffle
"""

#: Nested key only — what a post-rollback-cleanup proxy.yaml looks like, and
#: the shape that made the old top-level-only read FALSE-WARN (t_70d0cffa).
PROXY_NESTED_ONLY = """\
aliases: {}
gateways:
  litellm:
    balancing: least-in-flight
    router_settings:
      routing_strategy: simple-shuffle
"""

#: Top-level key only — the 0.3.10 rollback shape (still must be honoured).
PROXY_TOP_LEVEL_ONLY = """\
aliases: {}
router_settings:
  routing_strategy: simple-shuffle
gateways:
  litellm:
    balancing: least-in-flight
"""

LITELLM_OK = """\
model_list:
- model_name: worker-model
  litellm_params:
    api_base: http://10.0.0.1:8000/v1
litellm_settings:
  drop_params: true
  callbacks:
  - sparkrun_balancer.handler
router_settings:
  routing_strategy: simple-shuffle
"""

#: Same, with no emitted router_settings at all — what sparkrun generates when
#: the configured value was not a mapping (it drops it and emits nothing).
LITELLM_NO_ROUTER = """\
model_list:
- model_name: worker-model
  litellm_params:
    api_base: http://10.0.0.1:8000/v1
litellm_settings:
  drop_params: true
  callbacks:
  - sparkrun_balancer.handler
"""


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _drift(tmp_path, proxy=PROXY_OK, litellm=LITELLM_OK):
    proxy_path = _w(tmp_path, "proxy.yaml", proxy) if proxy is not None else None
    lit_path = _w(tmp_path, "litellm_config.yaml", litellm) if litellm is not None else None
    return proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)


# ── dep_bump_guard ──────────────────────────────────────────────────────────

class TestDepBumpGuard:
    def test_rule_names_the_live_fork_branch(self):
        from hscc_daemon import dep_bump_guard as g
        assert g.FORK_BRANCH == "feat/proxy-least-in-flight-041"
        assert g.FORK_REMOTE == "pom11/sparkrun"
        assert g.SPARKRUN_CHECKOUT == "~/dev/sparkrun-041-verify"
        text = "\n".join(g.FORK_RULE_LINES)
        # The failure this guard exists to prevent, stated in the rule itself:
        assert "feat/proxy-least-in-flight-041" in text
        assert "checkout main" in text and "never" in text.lower()
        assert "rebase" in text
        assert "rev-parse --abbrev-ref HEAD" in text
        assert "router_settings" in text
        # The post-bump read-site sentence must point at the REAL site. Since
        # v0.4.0 the literal `router_settings` token appears in exactly ONE
        # sparkrun source file — plugins/litellm/engine.py — and grep finds
        # NOTHING in proxy/config.py (re-verified 2026-10-20 against tags
        # v0.4.0 and v0.4.1). A rule that pins the old proxy/config.py read
        # site makes the next bump worker grep the wrong file, come up empty,
        # and false-alarm that the fork's balancer work vanished. Pinned
        # PER-LINE (the tuple design): a whole-blob substring check would be
        # satisfied by the negation line naming the wrong file.
        read_site = next(ln for ln in g.FORK_RULE_LINES
                         if "still read" in ln)
        assert "src/sparkrun/plugins/litellm/engine.py" in read_site
        assert "_configured_router_settings" in read_site
        assert "build_litellm_config" in read_site
        assert "src/sparkrun/proxy/config.py" not in read_site
        # And a separate line keeps the wrong path from being grep'd at all,
        # so a worker can never read the rule as "grep proxy/config.py".
        negate = [ln for ln in g.FORK_RULE_LINES
                  if "proxy/config.py" in ln]
        assert len(negate) == 1
        assert "Do NOT grep" in negate[0] and "no-match" in negate[0]
        # simple-shuffle in the generated router config is EXPECTED with the
        # balancer live (2026-10-19 cutover) — the rule must keep saying so,
        # or the next verifier mis-flags a healthy proxy as drift.
        assert "simple-shuffle" in text and "EXPECTED" in text
        assert "sparkrun_balancer.handler" in text
        # The SUPERSEDED names must not survive anywhere in the canonical text.
        assert "feat/proxy-router-settings" not in text

    def test_rule_only_for_sparkrun(self):
        from hscc_daemon import dep_bump_guard as g
        assert g.fork_rule_block(["hermes-agent"]) == []
        assert g.fork_rule_block([]) == []
        assert len(g.fork_rule_block(["sparkrun"])) == len(g.FORK_RULE_LINES)
        assert g.fork_rule_block(["hermes-agent", "sparkrun"]) == list(g.FORK_RULE_LINES)


# ── the OPERATOR RUNBOOK must keep agreeing with the canonical rule ────────

RUNBOOK = (Path(__file__).resolve().parents[2] / "install" / "hscc-skills"
           / "hscc" / "references" / "runtime-bump-verification.md")

#: Sentence splitter for markdown. `(?<=[.!?])\s+` on its own is not enough:
#: this runbook hard-wraps at ~80 columns, so a path or identifier is always
#: followed by a newline and the whole file collapses into a handful of "sent
#: per line" fragments — an earlier draft of this pin scoped the read-site
#: assertion to the fragment `feat/proxy-…-040, and \`router_settings\` is
#: still read in` and every mutant passed on it. Normalise whitespace FIRST,
#: then split on a blank line (paragraph/list item) or sentence punctuation.
_SENT_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+")

#: Backtick-quoted identifier: a path (contains `/`) or a bare code name. Not
#: shell snippets or prose — those carry spaces and are not obligations.
_CODE_TOKEN = re.compile(r"`([^`]+)`")

#: Branch-name shape used by the sparkrun fork (greedy, so the `-040` suffix
#: is part of one token and never reads as the shorter historical name).
_BRANCH_TOKEN = re.compile(r"feat/proxy[-0-9a-z]*")

#: Words a runbook may use to say "this base strategy is not drift". The
#: canonical line says EXPECTED; the runbook says HEALTHY / "not a regression".
#: An accepted-vocabulary list, NOT a restatement of the canonical string — the
#: canonical side of this pair is pinned against itself below, so renaming the
#: guard's wording fails the test instead of silently widening it.
_NOT_DRIFT_MARKERS = ("EXPECTED", "HEALTHY", "not a regression", "not drift")


def _runbook_text():
    if not RUNBOOK.exists():
        pytest.fail(
            f"operator runbook vanished or moved: {RUNBOOK}\n"
            "This pin exists because that file duplicates FORK_RULE_LINES for "
            "a human reader; a rename must update this path, never drop the pin."
        )
    return RUNBOOK.read_text()


def _sentences(text):
    return [s.strip() for s in _SENT_SPLIT.split(re.sub(r"\s+", " ", text))
            if s.strip()]


def _code_tokens(line):
    return [t for t in _CODE_TOKEN.findall(line)
            if "/" in t or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", t)]


def _one(sents, what, text):
    """Exactly-one helper: zero or many obligations are both a RED here, since
    'which sentence states the read site' must be unambiguous to be checkable."""
    if len(sents) != 1:
        pytest.fail(f"expected exactly one {what} in {RUNBOOK.name}, "
                    f"found {len(sents)}:\n" + "\n".join(sents) + "\n\n"
                    f"--- {RUNBOOK.name} ---\n{text}")
    return sents[0]


def _balancer_line():
    """The canonical 'confirm the balancer is live' line, selected by SHAPE.

    It is the only FORK_RULE_LINES entry that names both a generated ``.yaml``
    file and a ``.py`` file (the config you open, and the module that must sit
    next to it). A shape, not a literal match on its prose: a rewording keeps
    being selected, and the exactly-one assert means the obligation cannot be
    split across two lines or quietly deleted without going RED here.
    """
    from hscc_daemon import dep_bump_guard as g
    lines = [ln for ln in g.FORK_RULE_LINES
             if any(t.endswith(".yaml") for t in _code_tokens(ln))
             and any(t.endswith(".py") for t in _code_tokens(ln))]
    assert len(lines) == 1, (
        "dep_bump_guard must carry exactly one line naming both the generated "
        f"litellm config and the balancer file beside it — found {len(lines)}; "
        "the runbook's balancer-signal leg scopes itself to that line.")
    return lines[0]


def _balancer_signal_ids():
    """The identifiers that ARE the balancer-live signal, read off the guard.

    Shape rule: a dotted attribute/callback path (``litellm_settings.callbacks``,
    ``sparkrun_balancer.handler``) or a module file (``sparkrun_balancer.py``).
    Deliberately excludes the config path itself (that is where you look, not
    what you look for) and the ``hscc verify`` check name (a no-underscore,
    no-dot name — the drift check, not the signal the canonical line calls
    decisive). Nothing here restates the guard's literals.
    """
    return [t for t in _code_tokens(_balancer_line())
            if t.endswith(".py") or ("." in t and "/" not in t)]


def _generated_config_name():
    """Basename of the generated config the guard tells the operator to open."""
    return next(t.rsplit("/", 1)[-1] for t in _code_tokens(_balancer_line())
                if t.endswith(".yaml"))


def _base_strategy_value():
    """The base routing strategy the canonical line calls EXPECTED.

    Read off the colon-bearing code token in the balancer line
    (``router_settings.routing_strategy: simple-shuffle`` -> ``simple-shuffle``)
    instead of being spelled out again in this test file, so a guard change of
    that value moves the runbook obligation with it.
    """
    keyed = [t for t in _CODE_TOKEN.findall(_balancer_line()) if ": " in t]
    assert len(keyed) == 1, (
        "the canonical balancer line must carry exactly one `key: value` code "
        f"token (the base strategy) — found {keyed}")
    return keyed[0].split(": ", 1)[1].strip()


def _negated_file():
    """The path the guard explicitly negates, taken from the guard itself."""
    from hscc_daemon import dep_bump_guard as g
    lines = [ln for ln in g.FORK_RULE_LINES if "Do NOT grep" in ln]
    assert len(lines) == 1, (
        "dep_bump_guard must carry exactly one 'Do NOT grep' negation line — "
        f"found {len(lines)}; the runbook pin scopes its negation to it.")
    paths = [t for t in _code_tokens(lines[0]) if "/" in t and t.endswith(".py")]
    assert len(paths) == 1, f"ambiguous negated file in {lines[0]!r}"
    return paths[0]


class TestRunbookAgreesWithGuard:
    """The runbook is the surviving prose duplicate of FORK_RULE_LINES.

    `dep_bump_guard`'s module docstring says these strings live in code
    "instead of being prose duplicated in two places" — but
    `install/hscc-skills/hscc/references/runtime-bump-verification.md` step 3
    IS that prose, written for the operator who runs the bump by hand. It
    carried the same false `router_settings`-read-site claim as the guard and
    stayed that way until a human verified PR #55 by hand (t_669b8df7 fixed
    both copies; nothing in the suite had ever opened the file, so no test
    could have caught it).

    Everything asserted here is DERIVED from the canonical tuple, so a future
    fork re-cut or rewording moves the obligation instead of splitting it in
    two. The runbook is allowed to say MORE than the guard (it is operator
    prose); it is not allowed to contradict the guard on an obligation the
    guard states.
    """

    def test_pin_is_not_vacuous(self):
        """Anti-vacuity for the derivation itself: if a future edit to
        FORK_RULE_LINES made the obligations empty, every agreement test below
        would pass on nothing. Pin that the canonical line still has teeth."""
        from hscc_daemon import dep_bump_guard as g
        read = next(ln for ln in g.FORK_RULE_LINES if "still read" in ln)
        assert len(_code_tokens(read)) >= 4
        assert [ln for ln in g.FORK_RULE_LINES if "proxy/config.py" in ln]
        assert _BRANCH_TOKEN.findall(g.__doc__ or "")

    def test_runbook_states_the_same_read_site_as_the_guard(self):
        """Per-line discipline, mirrored onto the runbook.

        The canonical read-site line is the ONLY place in the guard that
        asserts where `router_settings` is read, and it names engine.py; a
        separate line negates proxy/config.py. A whole-file substring check on
        the runbook would be satisfied by that negation (it names the wrong
        path too), so the positive claim is scoped to the ONE sentence that
        makes it, and every code identifier the canonical line names must be
        in it.
        """
        from hscc_daemon import dep_bump_guard as g
        canonical = next(ln for ln in g.FORK_RULE_LINES if "still read" in ln)
        runbook = _one([s for s in _sentences(_runbook_text()) if "still read" in s],
                       "read-site sentence", _runbook_text())
        missing = [t for t in _code_tokens(canonical) if t not in runbook]
        assert not missing, (
            "runbook read-site sentence no longer carries the canonical "
            f"identifiers {missing}:\n  guard:   {canonical}\n  runbook: {runbook}")
        # And the sentence that states the read site must NOT name the file the
        # guard explicitly negates — that is the exact regression (t_669b8df7).
        negated = _negated_file()
        assert negated not in runbook, (
            f"runbook read-site sentence names {negated} — since v0.4.0 that "
            "file has no `router_settings`, so the runbook is back to sending "
            f"the operator to grep an empty file:\n  {runbook}")

    def test_runbook_keeps_the_explicit_negation(self):
        """The runbook must still tell the operator NOT to grep the negated
        file and NOT to read a no-match as drift, in exactly one place."""
        from hscc_daemon import dep_bump_guard as g
        negated = _negated_file()
        canonical = next(ln for ln in g.FORK_RULE_LINES if negated in ln)
        # Canonical side first, so the two markers below cannot silently drift
        # away from the guard's own wording.
        assert "Do NOT grep" in canonical and "no-match" in canonical
        sentences_naming_it = [s for s in _sentences(_runbook_text())
                               if negated in s]
        assert sentences_naming_it, (
            f"runbook dropped its `Do NOT grep {negated}` caveat: an operator "
            "who greps there and finds nothing will false-alarm that the "
            "fork's balancer work is gone.")
        for s in sentences_naming_it:
            assert "Do NOT grep" in s and "no-match" in s, (
                f"runbook names {negated} in a sentence that does not negate "
                f"it as a read site — a positive claim is exactly the drift "
                f"this pin exists to catch:\n  {s}")

    def test_runbook_branch_name_matches_the_guard(self):
        """FORK_BRANCH agreement, so a re-cut cannot update one and not the
        other. The live-state sentence and each numbered rule may name only the
        canonical branch; any other `feat/proxy-*` name is allowed ONLY as
        history, i.e. in a sentence that says rollback/supersede AND a name the
        guard's own docstring documents as historical."""
        from hscc_daemon import dep_bump_guard as g
        text = _runbook_text()
        historical = set(_BRANCH_TOKEN.findall(g.__doc__ or "")) - {g.FORK_BRANCH}
        assert g.FORK_BRANCH in text, (
            f"runbook never names FORK_BRANCH={g.FORK_BRANCH!r}")
        # The sentence that reports what is checked out, and the rules that
        # rebase/verify it, must name the canonical branch and nothing else.
        for needle, what in (("checked out on", "live-state sentence"),
                             ("**rebase", "rebase rule"),
                             ("rev-parse --abbrev-ref HEAD", "post-bump check")):
            sent = _one([s for s in _sentences(text) if needle in s], what, text)
            found = set(_BRANCH_TOKEN.findall(sent))
            assert found == {g.FORK_BRANCH}, (
                f"{what} must name exactly FORK_BRANCH={g.FORK_BRANCH!r}, "
                f"found {sorted(found)}:\n  {sent}")
        # Everywhere else: historical names only, and only as history.
        for s in _sentences(text):
            for name in _BRANCH_TOKEN.findall(s):
                if name == g.FORK_BRANCH:
                    continue
                assert name in historical, (
                    f"runbook names branch {name!r}, which the guard does not "
                    "document as historical (superseded names: "
                    f"{sorted(historical)}): {s}")
                assert "rollback" in s.lower() or "supersede" in s.lower(), (
                    f"branch {name!r} appears as a live instruction rather "
                    f"than a documented rollback/superseded note: {s}")

    def test_runbook_keeps_the_expected_base_strategy_caveat(self):
        """`routing_strategy: simple-shuffle` in the generated config is the
        HEALTHY value while the balancer is live, not drift. Drop that caveat
        and the next verifier mis-flags a healthy proxy and 'fixes' it."""
        from hscc_daemon import dep_bump_guard as g
        canonical = next(ln for ln in g.FORK_RULE_LINES
                         if "simple-shuffle" in ln and "EXPECTED" in ln)
        assert "sparkrun_balancer.handler" in canonical   # canonical side
        caveat = [s for s in _sentences(_runbook_text())
                  if "simple-shuffle" in s and "balancer" in s
                  and any(m in s for m in _NOT_DRIFT_MARKERS)]
        assert caveat, (
            "runbook no longer states that `simple-shuffle` in "
            "litellm_config.yaml is expected/healthy with the balancer live "
            f"(accepted markers: {_NOT_DRIFT_MARKERS}). Without it the next "
            "operator treats a healthy proxy as drift.")

    def test_runbook_states_the_balancer_live_signal_the_guard_calls_decisive(self):
        """The guard calls the balancer callback *the* live signal; step 4 must
        say so, not just that `router_settings` propagates.

        `litellm_settings.callbacks` containing `sparkrun_balancer.handler`,
        with `sparkrun_balancer.py` next to the config, is the one check that
        catches a balancer which stopped being emitted while `router_settings`
        still propagated (balancer file missing beside the config, callback
        renamed). Every other step-4 signal stays green in that scenario, which
        is why the canonical line calls the callback the live signal. This leg
        is the "runbook says LESS than the guard" half of the same split:
        `test_runbook_states_the_same_read_site_as_the_guard` pins the
        positive read-site claim, this pins the positive balancer-live claim.

        Identifiers are DERIVED from the canonical line by shape
        (`_balancer_signal_ids`), and the obligation is scoped per-sentence so
        a runbook cannot satisfy it by naming the callback once in an unrelated
        paragraph and telling the operator to check only `router_settings` in
        step 4.
        """
        text = _runbook_text()
        ids = _balancer_signal_ids()
        cfg = _generated_config_name()
        strategy = _base_strategy_value()
        # Canonical-side floor, same discipline as `test_pin_is_not_vacuous`:
        # if a future edit to the guard line shrank the derived signal to one
        # or two tokens, `missing` below could hardly ever be non-empty and
        # this leg would pass on a runbook that says nothing. The line must
        # still name a dotted callback/attribute path AND the module file that
        # has to sit beside the config.
        dotted = [t for t in ids if "." in t and "/" not in t and not t.endswith(".py")]
        assert len(ids) >= 3 and dotted and any(t.endswith(".py") for t in ids), (
            "the canonical balancer line no longer names a dotted callback path "
            f"plus the module file beside the config (derived: {ids}) — step 4's "
            "obligation cannot be derived from it any more:\n  "
            f"{_balancer_line()}")

        # Every runbook sentence that points the operator at the generated
        # config, or at the base strategy the guard says is expected, must
        # carry the whole balancer-live signal.
        pointing = [s for s in _sentences(text)
                    if cfg in s or strategy in s]
        assert pointing, (
            f"runbook never mentions the generated config {cfg!r} or the base "
            f"strategy {strategy!r} — step 4's balancer check has nothing to "
            "scope against.")
        for s in pointing:
            missing = [t for t in ids if t not in s]
            assert not missing, (
                f"runbook sentence names {cfg!r}/{strategy!r} but omits the "
                f"balancer-live identifiers {missing} that the canonical "
                "FORK_RULE_LINES line calls the live signal — an operator "
                "following it would pass step 4 green on a proxy whose "
                "balancer stopped being emitted:\n  guard:   "
                f"{_balancer_line()}\n  runbook: {s}")
        # And step 4 must still state the check as a check on the generated
        # file, not merely as the base-strategy reassurance.
        step4 = _one([s for s in _sentences(text) if "Restart the proxy" in s],
                     "step-4 restart-and-confirm sentence", text)
        assert cfg in step4, (
            f"step 4 no longer tells the operator to open {cfg}: {step4}")

    def test_runbook_keeps_the_never_checkout_main_prohibition(self):
        """The failure this whole guard exists to prevent, stated in both
        copies: never checkout main / never reinstall from upstream; the bump
        is a rebase that leaves the fork branch checked out."""
        from hscc_daemon import dep_bump_guard as g
        # Backticks stripped so markers can be written as plain prose.
        text = _runbook_text().replace("`", "").lower()
        canonical = "\n".join(g.FORK_RULE_LINES).replace("`", "").lower()
        for marker in ("never git checkout main", "never reinstall",
                       "rebase", "checked out"):
            assert marker in canonical, f"guard lost the {marker!r} rule"  # canonical side
            assert marker in text, f"runbook lost the {marker!r} instruction"


# ── proxy_router.router_drift — the four required fake cases + extras ──────

class TestRouterDrift:
    def test_matching(self, tmp_path):
        res = _drift(tmp_path)
        assert res["status"] == "ok" and res["warn"] is False
        assert "router_settings" in res["detail"]

    def test_matching_carries_both_file_names(self, tmp_path):
        # Divergence text must name BOTH files (card requirement).
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: least-busy"))
        assert res["warn"] is True
        assert "proxy.yaml" in res["detail"]
        assert "litellm_config.yaml" in res["detail"]

    def test_missing_litellm_config_warns_with_both_paths(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        res = proxy_router.router_drift(
            proxy_config=proxy_path,
            litellm_config=str(tmp_path / "absent" / "litellm_config.yaml"))
        assert res["status"] == "warn" and res["warn"] is True
        assert res["detail"].startswith("WARN:")
        assert proxy_path in res["detail"]

    def test_divergent_router_settings_warns(self, tmp_path):
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: lowest-latency"))
        assert res["status"] == "warn" and "diverged" in res["detail"]
        # drift is a WARN, never a hard failure, at every layer:
        assert res["status"] != "fail"

    def test_no_proxy_config_skips(self, tmp_path):
        res = proxy_router.router_drift(
            proxy_config=str(tmp_path / "nope" / "proxy.yaml"),
            litellm_config=_w(tmp_path, "litellm_config.yaml", LITELLM_OK))
        assert res["status"] == "skip" and res["warn"] is False
        assert "not applicable" in res["detail"]

    def test_least_busy_strategy_flagged(self, tmp_path):
        res = _drift(tmp_path,
                     proxy=PROXY_OK.replace("simple-shuffle", "least-busy"),
                     litellm=LITELLM_OK.replace("simple-shuffle", "least-busy"))
        # Even when config and emitted file AGREE, least-busy is a trap.
        assert res["warn"] is True
        assert "least-busy" in res["detail"] and "no-op" in res["detail"]
        assert "least-in-flight" in res["detail"]  # points at the working knob

    def test_balancer_requested_but_absent_warns(self, tmp_path):
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "  callbacks:\n  - sparkrun_balancer.handler\n", ""))
        assert res["warn"] is True
        assert "sparkrun_balancer.handler" in res["detail"]

    def test_unimplemented_balancing_warns(self, tmp_path):
        res = _drift(tmp_path, proxy=PROXY_OK.replace(
            "least-in-flight", "round-robin"))
        assert res["warn"] is True
        assert "not implemented" in res["detail"]

    def test_non_mapping_router_settings_warns(self, tmp_path):
        # The documented footgun: bare `router_settings: least-busy`.
        res = _drift(tmp_path, proxy="router_settings: least-busy\n",
                     litellm=LITELLM_OK)
        assert res["warn"] is True
        assert "not a mapping" in res["detail"]

    # ── nested vs top-level read order (t_70d0cffa) ────────────────────────
    # sparkrun 0.4.0 reads gateways.litellm.router_settings
    # (plugins/litellm/engine.py::_configured_router_settings); the top-level
    # key is only what the 0.3.10 rollback line reads. The check must resolve
    # the SAME key sparkrun resolves, or it false-warns the moment the
    # top-level duplicate is cleaned out of proxy.yaml.

    def test_nested_only_expects_nested(self, tmp_path):
        """(a) The rollback-cleanup shape: nested key alone. This is the case
        the old top-level-only read false-warned on."""
        res = _drift(tmp_path, proxy=PROXY_NESTED_ONLY, litellm=LITELLM_OK)
        assert res["status"] == "ok" and res["warn"] is False
        assert "simple-shuffle" in res["detail"]

    def test_nested_wins_over_differing_top_level(self, tmp_path):
        """(b) Both keys present and DISAGREEING: 0.4.0 wins, so the nested
        value is what must be expected of the generated config."""
        proxy = PROXY_OK.replace(
            "router_settings:\n  routing_strategy: simple-shuffle\n",
            "router_settings:\n  routing_strategy: least-busy\n")
        # Nested says simple-shuffle, top level says least-busy; the emitted
        # file carries the nested value -> ok, and NOT the least-busy trap warn
        # (which would fire if the check had read the top-level key).
        res = _drift(tmp_path, proxy=proxy, litellm=LITELLM_OK)
        assert res["status"] == "ok" and res["warn"] is False
        assert "least-busy" not in res["detail"]

    def test_nested_wins_divergence_names_nested_key(self, tmp_path):
        """Same precedence, opposite direction: when the generated config
        disagrees with the nested value the report names the nested key, not
        the top-level duplicate nobody reads any more."""
        proxy = PROXY_NESTED_ONLY.replace("simple-shuffle", "lowest-latency")
        res = _drift(tmp_path, proxy=proxy, litellm=LITELLM_OK)
        assert res["warn"] is True and "diverged" in res["detail"]
        assert "gateways.litellm.router_settings" in res["detail"]

    def test_top_level_only_still_expected(self, tmp_path):
        """(c) 0.3.10 rollback shape (top level only) must still be honoured —
        otherwise a rollback would blind the very check that guards it."""
        res = _drift(tmp_path, proxy=PROXY_TOP_LEVEL_ONLY, litellm=LITELLM_OK)
        assert res["status"] == "ok" and res["warn"] is False
        assert "simple-shuffle" in res["detail"]

    def test_top_level_only_divergence_names_top_level_key(self, tmp_path):
        proxy = PROXY_TOP_LEVEL_ONLY.replace("simple-shuffle", "lowest-latency")
        res = _drift(tmp_path, proxy=proxy, litellm=LITELLM_OK)
        assert res["warn"] is True and "diverged" in res["detail"]
        # The top-level source is named exactly, and does not claim to be the
        # nested key.
        assert "router_settings" in res["detail"]
        assert "gateways.litellm.router_settings" not in res["detail"]

    def test_non_mapping_nested_ignored_like_top_level(self, tmp_path):
        """(d) A non-mapping NESTED value is dropped by sparkrun exactly as a
        non-mapping top-level one: same warn text semantics, and the check
        demands no router_settings from the generated file."""
        res = _drift(tmp_path,
                     proxy="gateways:\n  litellm:\n"
                           "    balancing: least-in-flight\n"
                           "    router_settings: least-busy\n",
                     litellm=LITELLM_NO_ROUTER)
        assert res["warn"] is True
        assert "not a mapping" in res["detail"]
        assert "least-busy" in res["detail"]
        # names WHERE the ignored value lives
        assert "gateways.litellm.router_settings" in res["detail"]
        # and no bogus second complaint about the (absent) emission
        assert "diverged" not in res["detail"]

    def test_non_mapping_nested_does_not_fall_back_to_top_level(self, tmp_path):
        """A present-but-non-mapping nested key is still `present`: 0.4.0 sees
        it and drops it, so a stale top-level duplicate must NOT resurrect as
        the expectation (that would invent an intent sparkrun never reads)."""
        proxy = PROXY_OK.replace(
            "    router_settings:\n      routing_strategy: simple-shuffle",
            "    router_settings: least-busy")
        res = _drift(tmp_path, proxy=proxy, litellm=LITELLM_OK)
        assert res["warn"] is True
        assert "not a mapping" in res["detail"]
        assert "gateways.litellm.router_settings" in res["detail"]

    def test_nested_null_falls_back_to_top_level(self, tmp_path):
        """`router_settings:` with no value parses to None — sparkrun treats it
        as unset (`if value is None: return {}`), so the top-level fallback
        applies rather than the key being considered present."""
        proxy = PROXY_OK.replace(
            "    router_settings:\n      routing_strategy: simple-shuffle",
            "    router_settings:")
        res = _drift(tmp_path, proxy=proxy, litellm=LITELLM_OK)
        assert res["status"] == "ok" and res["warn"] is False

    def test_non_mapping_gateways_never_raises(self, tmp_path):
        """`gateways:` holding a scalar (or a list) must not raise — the same
        defensive shape the balancing read already had."""
        for bad in ("gateways: nope\nrouter_settings:\n  routing_strategy: simple-shuffle\n",
                    "gateways:\n- nope\nrouter_settings:\n  routing_strategy: simple-shuffle\n"):
            res = _drift(tmp_path, proxy=bad, litellm=LITELLM_OK)
            assert res["status"] == "ok" and res["warn"] is False

    def test_router_settings_source_read_order(self):
        """Unit pin of the helper itself (the drift cases above could pass on
        a coincidental equality; this pins which key resolves)."""
        nested = {"gateways": {"litellm": {"router_settings": {"a": 1}}}}
        both = {"gateways": {"litellm": {"router_settings": {"a": 1}}},
                "router_settings": {"b": 2}}
        top = {"router_settings": {"b": 2}}
        assert proxy_router.router_settings_source(nested) == (
            {"a": 1}, "gateways.litellm.router_settings")
        assert proxy_router.router_settings_source(both) == (
            {"a": 1}, "gateways.litellm.router_settings")
        assert proxy_router.router_settings_source(top) == (
            {"b": 2}, "router_settings")
        assert proxy_router.router_settings_source({}) == (None, "router_settings")
        # present-but-null nested -> sparkrun sees unset, fallback applies
        assert proxy_router.router_settings_source(
            {"gateways": {"litellm": {"router_settings": None}},
             "router_settings": {"b": 2}}) == ({"b": 2}, "router_settings")
        # a non-mapping nested value is still PRESENT (no fallback)
        assert proxy_router.router_settings_source(
            {"gateways": {"litellm": {"router_settings": "least-busy"}},
             "router_settings": {"b": 2}}) == ("least-busy",
                                               "gateways.litellm.router_settings")

    def test_configured_intent_reports_resolved_source(self):
        intent = proxy_router.configured_intent(
            {"gateways": {"litellm": {"router_settings": {"a": 1}}},
             "router_settings": {"b": 2}})
        assert intent["router_settings"] == {"a": 1}
        assert intent["router_settings_source"] == "gateways.litellm.router_settings"
        intent = proxy_router.configured_intent({"router_settings": {"b": 2}})
        assert intent["router_settings"] == {"b": 2}
        assert intent["router_settings_source"] == "router_settings"

    def test_nothing_configured_nothing_emitted_skips(self, tmp_path):
        res = _drift(tmp_path, proxy="aliases: {}\n",
                     litellm="model_list: []\nlitellm_settings:\n  drop_params: true\n")
        assert res["status"] == "skip" and res["warn"] is False

    def test_unreadable_inputs_unverified_never_fail(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        bad = _w(tmp_path, "litellm_config.yaml", "model_list: [unclosed")
        res = proxy_router.router_drift(proxy_config=proxy_path, litellm_config=bad)
        assert res["status"] == "unverified" and res["warn"] is False

    def test_pure_read_inputs_unchanged(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        lit_path = _w(tmp_path, "litellm_config.yaml", LITELLM_OK)
        before = (hashlib.sha256(open(proxy_path, "rb").read()).hexdigest(),
                  hashlib.sha256(open(lit_path, "rb").read()).hexdigest())
        proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)
        proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)
        after = (hashlib.sha256(open(proxy_path, "rb").read()).hexdigest(),
                 hashlib.sha256(open(lit_path, "rb").read()).hexdigest())
        assert before == after

    def test_default_paths_are_sparkruns_own_locations(self):
        assert proxy_router.PROXY_CONFIG_PATH == "~/.config/sparkrun/proxy.yaml"
        assert proxy_router.LITELLM_CONFIG_PATH == "~/.cache/sparkrun/proxy/litellm_config.yaml"


# ── verify.check_proxy_router_settings ─────────────────────────────────────

class TestVerifyCheck:
    def _check(self, tmp_path, proxy=PROXY_OK, litellm=LITELLM_OK):
        from hscc_daemon.verify import check_proxy_router_settings
        return check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", proxy),
            litellm_config=_w(tmp_path, "litellm_config.yaml", litellm))

    def test_ok_shape_on_match(self, tmp_path):
        res = self._check(tmp_path)
        assert res["name"] == "proxy_router_settings"
        assert res["ok"] is True and not res["detail"].startswith("WARN")

    def test_drift_is_warn_not_fail(self, tmp_path):
        """Operator rule: divergence must NEVER make the check red."""
        res = self._check(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: least-busy"))
        assert res["ok"] is True            # not False — WARN, per operator
        assert res["detail"].startswith("WARN:")

    def test_unreadable_is_none_not_false(self, tmp_path):
        from hscc_daemon.verify import check_proxy_router_settings
        res = check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", PROXY_OK),
            litellm_config=str(tmp_path / "gone" / "litellm_config.yaml"))
        # absent litellm config while proxy.yaml configures settings: WARN-ok
        assert res["ok"] is True and res["detail"].startswith("WARN:")
        # unreadable (corrupt) input: unverified, still never False
        bad = _w(tmp_path, "bad.yaml", "model_list: [unclosed")
        res2 = check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", PROXY_OK),
            litellm_config=str(bad))
        assert res2["ok"] is None

    def test_registered_in_run_all(self):
        from hscc_daemon import verify
        import inspect
        src = inspect.getsource(verify.run_all)
        assert "check_proxy_router_settings" in src


# ── health.check_proxy — the proxy daemon stream carries the verdict ───────

class TestProxyStreamCarriesDrift:
    """The drift verdict must land in the proxy STATE FILE on both paths
    (healthy + unhealthy) WITHOUT ever changing the health verdict itself."""

    _SERVING_NO_UNITS = {"version": 2, "units": []}

    def _setup(self, tmp_hfcc_dir, monkeypatch, drift):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: self._SERVING_NO_UNITS)
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {
                                "ok": True, "models": ["worker-model", "orchestrator-model"],
                                "error": None})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {"ok": False, "backends": set(),
                                                         "error": "stubbed"})
        monkeypatch.setattr(health, "_router_drift_probe", lambda: dict(drift))
        return health, state_dir

    def _entry(self, state_dir):
        return json.loads((state_dir / "proxy.json").read_text())

    def test_healthy_stream_records_drift_warn(self, tmp_hfcc_dir, monkeypatch):
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "warn", "warn": True, "detail": "WARN: diverged"})
        assert health.check_proxy() is True          # health verdict unchanged
        entry = self._entry(state_dir)
        assert entry["ok"] is True
        assert entry["router_drift_warn"] is True    # but the stream speaks
        assert "WARN: diverged" in entry["router_drift"]

    def test_healthy_stream_records_no_drift(self, tmp_hfcc_dir, monkeypatch):
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "ok", "warn": False, "detail": "carries router_settings"})
        assert health.check_proxy() is True
        entry = self._entry(state_dir)
        assert entry["router_drift_warn"] is False
        assert "carries router_settings" in entry["router_drift"]

    def test_unhealthy_stream_records_drift_too(self, tmp_hfcc_dir, monkeypatch):
        """Ordering pin: drift must be computed BEFORE the healthy/unhealthy
        branch so the unhealthy write carries it too (a unit test of the
        drift function alone cannot catch the call site moving). Mirrors
        TestCheckProxy._unhealthy_setup: reachable alias missing -> repair
        path -> run_cmd refuses (rc 1 shape)."""
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "warn", "warn": True, "detail": "WARN: diverged"})
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {
                                "ok": True, "models": ["some-model"], "error": ""})
        from hscc_daemon import serving
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: {
            "version": 2, "units": [
                {"id": "w1", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.1"], "port": 8001}]})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": False})
        assert health.check_proxy() is False
        entry = self._entry(state_dir)
        assert entry["router_drift_warn"] is True

    def test_raising_probe_never_breaks_proxy_check(self, tmp_hfcc_dir, monkeypatch):
        def _boom():
            raise RuntimeError("probe exploded")
        health, state_dir = self._setup(tmp_hfcc_dir, monkeypatch, None)
        monkeypatch.setattr(health, "_router_drift_probe", _boom)
        assert health.check_proxy() is True
        entry = self._entry(state_dir)
        assert "probe raised" in entry["router_drift"]
        assert entry["router_drift_warn"] is False   # unverified never warns
