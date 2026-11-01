"""Validate layer for the v4 template declarations (t_401e3045).

Spec: docs/design/2026-10-15-template-declarations.md — §1 (endpoints), §2
(proxy), §3 (caps), §5 (validate), §7 (non-regression), and §8 (knob
enforcement table), which is LAW: nothing may validate as "works" that §8
marks INERT.

Fakes only: no live cluster, no live sparkrun, no network. Hosts are the
documented 10.0.0.x placeholders (scripts/address_guard.py).

The overreach guard (TestOverreachGuard) compares against
_v3_nonuniform_golden.json, captured from the PRE-CHANGE code at 9bc991e2 —
see TestOverreachGuard's docstring for why that provenance is the whole point.
"""

import copy
import json
import os

import pytest
import yaml

import cluster_template as ct
import template_intent as ti

COMPUTE = ["10.0.0.244", "10.0.0.245", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
EXISTS = lambda r: True


def _parse(d):
    """raw dict -> (raw, parsed). Parse first so from_dict errors surface."""
    return d, ti.ClusterTemplate.from_dict(d)


def _check(d, **kw):
    raw, tpl = _parse(d)
    kw.setdefault("recipe_exists", EXISTS)
    return ct._structural_validate(raw, tpl, cluster_ips=COMPUTE, **kw)


def _v4(**extra):
    """Minimal valid v4 template: orchestrator + one proxied coding family."""
    d = {"name": "x", "version": 4, "orchestrator": "o.yaml",
         "families": [{"name": "coding", "models": [{"recipe": "c.yaml"}],
                       "workers": 1, "proxy": True}]}
    d.update(extra)
    return d


def _v4_uniform(**extra):
    """A valid uniform_pool v4 template: the flag requires ONE recipe across
    every unit (the pre-existing structural rule at cluster_template.py:1348),
    so the orchestrator and the family share it — otherwise every test here
    would be drowning in an unrelated error."""
    d = {"name": "x", "version": 4, "uniform_pool": True,
         "orchestrator": {"recipe": "same.yaml"},
         "families": [{"name": "coding", "models": [{"recipe": "same.yaml"}],
                       "workers": 1, "proxy": True}]}
    d.update(extra)
    return d


# ── §1.6 + §5 last row: version recognition and the legacy-key refusal ───────

class TestVersionGate:
    def test_v4_is_recognised(self):
        assert ct.RECOGNISED_VERSIONS == (2, 3, 4)
        errs, warns = _check(_v4())
        assert errs == []
        assert warns == []

    def test_endpoints_on_v3_is_refused_as_legacy_key(self):
        d = _v4(); d["version"] = 3
        d["endpoints"] = {"chat": "family-coding"}
        try:
            ti.ClusterTemplate.from_dict(d)
        except ti.TemplateIntentError as e:
            assert "endpoints" in str(e)
            return
        raise AssertionError("v3 carrying endpoints: must be refused")

    def test_proxy_and_caps_on_v3_are_refused_too(self):
        for key in ("proxy", "caps"):
            d = _v4(); d["version"] = 3
            d[key] = {"families": {}} if key == "proxy" else {"max_in_progress": 6}
            try:
                ti.ClusterTemplate.from_dict(d)
            except ti.TemplateIntentError as e:
                assert key in str(e), f"{key}: not named in refusal {e}"
            else:
                raise AssertionError(f"v3 carrying {key}: must be refused")

    def test_v2_carrying_them_is_refused(self):
        d = _v4(); d["version"] = 2
        d["caps"] = {"max_in_progress": 6}
        try:
            ti.ClusterTemplate.from_dict(d)
        except ti.TemplateIntentError as e:
            assert "caps" in str(e)
            return
        raise AssertionError("v2 carrying caps: must be refused")


# ── §1 endpoints ─────────────────────────────────────────────────────────────

class TestEndpoints:
    def test_full_block_validates_clean(self):
        errs, warns = _check(_v4(endpoints={
            "mode": "proxy-alias", "chat": "family-coding",
            "delegation": "family-coding",
            "profiles": {"default": "family-coding", "*-orch": "family-coding"}}))
        assert errs == [], errs
        assert warns == []

    def test_address_target_under_uniform_pool_is_the_spec_error_line(self):
        """§7's example line, reproduced byte-for-byte."""
        errs, _ = _check(_v4_uniform(endpoints={"chat": "10.0.0.1:8000"}))
        assert ("endpoints.chat -> '10.0.0.1:8000': uniform_pool endpoints "
                "must name a family (family-<name>) or 'orchestrator', never "
                "an address") in errs

    def test_host_shaped_target_without_a_port_is_also_an_address(self):
        """§1.1: 'A target is never an address' covers a host-shaped string, not
        only host:port — a colon is sufficient but not necessary."""
        for bad in ("10.0.0.1", "node-1.example.com"):
            errs, _ = _check(_v4_uniform(endpoints={"chat": bad}))
            assert any("never an address" in e for e in errs), \
                f"{bad!r} accepted as a uniform_pool target"

    def test_orchestrator_target_stays_legal_under_uniform_pool(self):
        """'orchestrator' is a symbolic unit, not an address (§5 row 1 lists it
        as the fix, so it must be accepted)."""
        errs, _ = _check(_v4_uniform(endpoints={"chat": "orchestrator"}))
        assert errs == []

    def test_address_target_is_allowed_when_not_uniform_pool(self):
        """§1.3: under a role split an explicit pin is legal, so the guard must
        NOT fire there. This is the exemption, pinned."""
        errs, _ = _check(_v4(endpoints={"chat": "10.0.0.1:8000"}))
        assert not any("never an address" in e for e in errs), errs

    def test_dangling_family_target_reuses_the_routing_error_voice(self):
        errs, _ = _check(_v4(endpoints={"chat": "family-vision"}))
        assert any("endpoints.chat -> 'family-vision': no such family in this "
                   "template" in e for e in errs)

    def test_profiles_selector_is_not_resolved_but_its_value_is(self):
        """'*-orch' is a role-profile glob, not a unit name; its VALUE must
        still resolve."""
        errs, _ = _check(_v4(endpoints={"profiles": {"*-orch": "family-coding"}}))
        assert errs == []
        errs, _ = _check(_v4(endpoints={"profiles": {"*-orch": "family-nope"}}))
        assert any("endpoints.profiles.*-orch -> 'family-nope': no such family"
                   in e for e in errs)

    def test_unknown_key_and_bad_mode(self):
        errs, _ = _check(_v4(endpoints={"mode": "round-robin", "chats": "x"}))
        assert any("endpoints: unknown key 'chats'" in e for e in errs)
        assert any("endpoints.mode: expected one of proxy-alias, explicit" in e
                   for e in errs)

    def test_non_mapping_block(self):
        errs, _ = _check(_v4(endpoints=["family-coding"]))
        assert any("endpoints: expected a mapping, got list" in e for e in errs)

    def test_profiles_must_be_a_mapping(self):
        errs, _ = _check(_v4(endpoints={"profiles": ["family-coding"]}))
        assert any("endpoints.profiles: expected a mapping of selector -> "
                   "target, got list" in e for e in errs)

    def test_shadowing_routing_warns_and_names_both(self):
        """§1.4: endpoints wins, and the redundancy must be visible."""
        d = _v4(routing={"delegation": "family-coding"},
                endpoints={"delegation": "family-coding"})
        errs, warns = _check(d)
        assert errs == []
        assert any("endpoints.delegation shadows routing.delegation" in w
                   for w in warns), warns

    def test_no_shadow_warning_when_only_one_speaks(self):
        errs, warns = _check(_v4(routing={"delegation": "family-coding"}))
        assert warns == []

    def test_routing_address_target_under_uniform_pool_is_refused(self):
        """§5 row 1 names both blocks: `routing` is covered too, and only under
        uniform_pool."""
        errs, _ = _check(_v4_uniform(routing={"delegation": "10.0.0.1:8000"}))
        assert any("routing.delegation -> '10.0.0.1:8000': uniform_pool "
                   "endpoints must name a family" in e for e in errs)
        errs, _ = _check(_v4(routing={"delegation": "10.0.0.1:8000"}))
        assert not any("never an address" in e for e in errs)


# ── §2 proxy ─────────────────────────────────────────────────────────────────

class TestProxy:
    def test_declared_knobs_validate_clean(self):
        errs, warns = _check(_v4(proxy={"families": {"coding": {
            "routing": "least-in-flight"}}}))
        assert errs == [], errs
        assert warns == []

    def test_proxy_not_a_mapping_is_the_spec_error_line(self):
        errs, _ = _check(_v4(proxy=["coding"]))
        assert ("proxy: expected a mapping (generic pass-through dict), got "
                "list") in errs

    def test_families_not_a_mapping_of_mappings(self):
        errs, _ = _check(_v4(proxy={"families": ["coding"]}))
        assert any("proxy.families: expected a mapping of family -> knobs, "
                   "got list" in e for e in errs)
        errs, _ = _check(_v4(proxy={"families": {"coding": "least-in-flight"}}))
        assert any("proxy.families.coding: expected a mapping of knobs, got "
                   "str" in e for e in errs)

    def test_unknown_knob_is_the_spec_error_line(self):
        errs, _ = _check(_v4(proxy={"families": {"coding": {"routning": "x"}}}))
        assert ("family 'coding' proxy knob: unknown key 'routning' (known: "
                "routing, max_parallel_requests, weight, timeout, "
                "num_retries)") in errs

    def test_routing_strategy_is_the_spec_error_line(self):
        """§2.2/§5: the balancer is a CustomLogger overlay, never a LiteLLM
        strategy — and the four-member Literal is named in the fix."""
        errs, _ = _check(_v4(proxy={"families": {
            "coding": {"routing_strategy": "least-in-flight"}}}))
        assert len(errs) == 1, errs
        assert errs[0] == (
            "proxy knob 'routing_strategy': the balancer is not a LiteLLM "
            "routing strategy — LiteLLM 1.82.6 admits only simple-shuffle / "
            "least-busy / usage-based-routing / latency-based-routing, and "
            "least-busy is broken there (its in-flight counter is never "
            "incremented on the async path); use 'routing: least-in-flight'")

    def test_least_busy_is_rejected_on_its_own_merits(self):
        """§2.2: `least-busy` is rejected not merely as a Literal member but
        because its counter is never incremented on the async path."""
        errs, _ = _check(_v4(proxy={"families": {"coding": {
            "routing": "least-busy"}}}))
        assert len(errs) == 1, errs
        assert any("'least-busy' is broken on LiteLLM 1.82.6" in e for e in errs)
        assert any("use 'routing: least-in-flight'" in e for e in errs)

    def test_families_disagreeing_on_routing_is_the_spec_error_line(self):
        errs, _ = _check(_v4(
            families=[{"name": "a", "models": [{"recipe": "c.yaml"}],
                       "workers": 1, "proxy": True},
                      {"name": "b", "models": [{"recipe": "c.yaml"}],
                       "workers": 1, "proxy": True}],
            proxy={"families": {"a": {"routing": "least-in-flight"},
                                "b": {"routing": "simple-shuffle"}}}))
        assert ("proxy: balancing is global today — families disagree ('a': "
                "least-in-flight, 'b': simple-shuffle); declare one value") in errs

    def test_same_routing_in_two_families_is_not_a_disagreement(self):
        errs, _ = _check(_v4(
            families=[{"name": "a", "models": [{"recipe": "c.yaml"}],
                       "workers": 1, "proxy": True},
                      {"name": "b", "models": [{"recipe": "c.yaml"}],
                       "workers": 1, "proxy": True}],
            proxy={"families": {"a": {"routing": "least-in-flight"},
                                "b": {"routing": "least-in-flight"}}}))
        assert errs == []

    def test_simple_shuffle_stays_accepted_as_balancer_off(self):
        """§2.2: under least-in-flight the base strategy must STAY
        simple-shuffle, so declaring it (balancer off) is legitimate, not
        drift."""
        errs, _ = _check(_v4(proxy={"families": {
            "coding": {"routing": "simple-shuffle"}}}))
        assert errs == []

    def test_undefined_family_under_proxy_families_is_named(self):
        errs, _ = _check(_v4(proxy={"families": {"vision": {
            "routing": "least-in-flight"}}}))
        assert any("proxy.families.vision: no such family in this template"
                   in e for e in errs)

    def test_generic_passthrough_dict_keeps_unknown_future_keys_visible(self):
        """§2.1: pass-through is the wire format, not a licence to ignore
        typos — so an unknown key must never be silently dropped from parse."""
        d = _v4(proxy={"families": {"coding": {"routing": "least-in-flight",
                                               "some_future_knob": 3}}})
        _, tpl = _parse(d)
        assert tpl.proxy["families"]["coding"]["some_future_knob"] == 3


# ── §8 knob enforcement table is LAW ─────────────────────────────────────────

class TestKnobEnforcementTableIsLaw:
    """Nothing may validate as 'works' that §8 marks INERT."""

    def test_table_membership_matches_the_spec_rows(self):
        # §8 ENFORCED rows: `routing` (least-in-flight, shipped end-to-end) and
        # `routing_strategy` (enforced only as verbatim emit). Everything else
        # §8 lists is INERT.
        assert ct.PRO_ENFORCED_KNOBS == ("routing", "routing_strategy")
        assert set(ct.PRO_INERT_KNOBS) == {"max_parallel_requests", "timeout",
                                           "num_retries", "weight"}
        # What a template may actually DECLARE under proxy.families.<name> is
        # `routing` plus the four INERT knobs. `routing_strategy` is
        # deliberately NOT declarable: it is the name of the REJECTED knob
        # (§5 row 4), so it must never appear in the accepted-key list.
        assert set(ct.PROXY_KNOWN_KNOBS) == {"routing"} | set(ct.PRO_INERT_KNOBS)
        assert "routing_strategy" not in ct.PROXY_KNOWN_KNOBS
        assert set(ct.PROXY_KNOWN_KNOBS) | {"routing_strategy"} == \
            set(ct.PRO_ENFORCED_KNOBS) | set(ct.PRO_INERT_KNOBS)

    def test_every_inert_knob_is_accepted_and_labelled_inert(self):
        """A §8-INERT knob must (a) not error, and (b) never be reported
        without the INERT label — that asymmetry is the whole §8 contract."""
        for knob in ct.PRO_INERT_KNOBS:
            errs, warns = _check(_v4(proxy={"families": {
                "coding": {"routing": "least-in-flight", knob: 2}}}))
            assert errs == [], f"{knob}: rejected ({errs})"
            inert = [w for w in warns if f"proxy.families.coding.{knob}" in w]
            assert inert, f"{knob}: accepted with no INERT warning: {warns}"
            assert "INERT" in inert[0]

    def test_summary_never_promise_an_inert_knob(self):
        _, tpl = _parse(_v4(proxy={"families": {
            "coding": {"routing": "least-in-flight", "weight": 3,
                       "timeout": 600, "num_retries": 2,
                       "max_parallel_requests": 2}}}))
        summary = ct.declaration_summary(tpl)
        for knob in ct.PRO_INERT_KNOBS:
            assert knob not in summary, \
                f"summary advertises INERT knob {knob}: {summary}"
        assert "least-in-flight" in summary

    def test_weight_is_inert_even_though_it_looks_declared(self):
        """§8: `weight` is INERT even after the §4.3 hook — the balancer's
        pick() ignores it. Accepting it must not imply uneven-node weighting."""
        errs, warns = _check(_v4(proxy={"families": {
            "coding": {"routing": "least-in-flight", "weight": 5}}}))
        assert errs == []
        assert any("weight" in w and "INERT" in w for w in warns)


# ── §3 caps ──────────────────────────────────────────────────────────────────

class TestCaps:
    def test_declared_caps_validate_clean(self):
        errs, warns = _check(_v4(caps={"max_in_progress": 6,
                                       "max_in_progress_per_profile": 2,
                                       "expected_requests_per_card": 1}))
        assert errs == [], errs
        assert warns == []

    def test_non_int_cap_is_the_spec_error_line(self):
        errs, _ = _check(_v4(caps={"max_in_progress": "many"}))
        assert "caps.max_in_progress: expected an integer, got 'many'" in errs

    def test_bool_is_not_an_int_cap(self):
        """`max_in_progress: true` is a typo, not a 1."""
        errs, _ = _check(_v4(caps={"max_in_progress": True}))
        assert any("caps.max_in_progress: expected an integer" in e
                   for e in errs)

    def test_non_positive_divisor_rejected(self):
        for bad in (0, -1):
            errs, _ = _check(_v4(caps={"expected_requests_per_card": bad}))
            assert any("caps.expected_requests_per_card: must be > 0" in e
                       for e in errs), bad

    def test_fractional_divisor_is_accepted(self):
        errs, _ = _check(_v4(caps={"expected_requests_per_card": 1.5}))
        assert errs == []

    def test_unknown_caps_key(self):
        errs, _ = _check(_v4(caps={"max_in_progresss": 6}))
        assert any("caps: unknown key 'max_in_progresss'" in e for e in errs)

    def test_non_mapping_caps(self):
        errs, _ = _check(_v4(caps=[6]))
        assert any("caps: expected a mapping, got list" in e for e in errs)


class TestShippedTemplateCapsMatchSOT:
    """GAP-E-3 (audit E t_f7d94a76): template and operator SOT must not diverge.

    ``_apply_caps_declaration`` (cluster_template.py:4009) writes whatever the
    template DECLARES, so a shipped template whose caps differ from the
    operator's live config turns one ``hscc template apply --confirm`` into a
    silent rewrite of the live values. That was real: flash-next-all shipped
    declaring per-profile 2 (77be50d1, 2026-10-29) while the operator's live
    kanban caps had moved to 12/3 — apply would have stomped 3 back to 2.

    The SOT pair is pinned explicitly below — it is NOT CAPS_MIGRATION_DEFAULTS
    (30/10), which is the bootstrap fill-if-absent default (enable_plugins.py,
    own test above) and a different thing entirely. If the operator moves the
    live caps again, move BOTH the template's caps block and OPERATOR_CAPS_SOT
    together; this test is the tripwire that forces that to be a conscious,
    atomic change instead of a silent divergence.
    """

    OPERATOR_CAPS_SOT = {"max_in_progress": 12, "max_in_progress_per_profile": 3}

    def _shipped_caps(self):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(tests_dir),
                            "templates", "4node", "flash-next-all.yaml")
        return (yaml.safe_load(open(path)) or {}).get("caps") or {}

    def test_declared_caps_equal_the_operator_sot(self):
        caps = self._shipped_caps()
        assert {k: caps.get(k) for k in self.OPERATOR_CAPS_SOT} == \
            self.OPERATOR_CAPS_SOT, (
            "flash-next-all caps diverged from the operator SOT 12/3 — "
            "`hscc template apply` would rewrite the live config to the "
            "template's numbers (GAP-E-3 footgun)")

    def test_apply_against_live_sot_config_rewrites_nothing(self, tmp_path):
        """The footgun itself, made unmissable: apply the SHIPPED template
        against a config seeded with the live 12/3 and nothing may move."""
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({"kanban": dict(self.OPERATOR_CAPS_SOT),
                        "model": {"default": "m"}}, conf.open("w"))
        tpl = ct._load_intent("4node-flash-next-all")
        res = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert res["declared"] == self.OPERATOR_CAPS_SOT
        assert res["changed"] is False, (
            "template apply would have rewritten the live caps — template and "
            "SOT diverged (GAP-E-3)")
        on_disk = yaml.safe_load(conf.read_text())
        assert on_disk["kanban"] == self.OPERATOR_CAPS_SOT
        assert on_disk["model"] == {"default": "m"}  # siblings untouched


class TestCapsDerivation:
    """§3.1 — the derivation is a starting point the operator can see, and it
    is ADVISORY while its only input (max_parallel_requests) is INERT."""

    def _proxy(self, *per_backend):
        fams = []
        for i, n in enumerate(per_backend):
            fams.append({"name": f"f{i}", "models": [{"recipe": "c.yaml"}],
                         "workers": 1, "proxy": True})
        knobs = {f"f{i}": {"max_parallel_requests": n}
                 for i, n in enumerate(per_backend)}
        return fams, {"families": knobs}

    def test_floor_of_sum_over_divisor(self):
        fams, prox = self._proxy(2, 2)
        _, tpl = _parse(_v4(families=fams, proxy=prox,
                            caps={"expected_requests_per_card": 1}))
        assert ct.derived_max_in_progress(tpl) == (4, "2 declared backends")

    def test_divisor_is_a_template_field_not_a_constant(self):
        fams, prox = self._proxy(3, 3)
        _, tpl = _parse(_v4(families=fams, proxy=prox,
                            caps={"expected_requests_per_card": 2}))
        value, basis = ct.derived_max_in_progress(tpl)
        assert value == 3 and "2 requests per card" in basis

    def test_floor_rounds_down(self):
        fams, prox = self._proxy(2, 1)
        _, tpl = _parse(_v4(families=fams, proxy=prox,
                            caps={"expected_requests_per_card": 2}))
        assert ct.derived_max_in_progress(tpl)[0] == 1

    def test_explicit_wins_and_the_shadowing_is_warned(self):
        fams, prox = self._proxy(2, 2)
        errs, warns = _check(_v4(families=fams, proxy=prox,
                                 caps={"max_in_progress": 4}))
        assert errs == []
        assert any("declared 4 wins over derived 4" in w for w in warns), warns
        assert any("ADVISORY" in w for w in warns), warns

    def test_derivation_reported_when_cap_absent(self):
        fams, prox = self._proxy(2, 2)
        errs, warns = _check(_v4(families=fams, proxy=prox,
                                 caps={"max_in_progress_per_profile": 2}))
        assert errs == []
        assert any("caps.max_in_progress not declared: derived 4" in w
                   for w in warns), warns

    def test_nothing_to_derive_from_stays_silent(self):
        errs, warns = _check(_v4(caps={"max_in_progress": 6}))
        assert errs == [] and warns == []

    def test_neither_block_means_migration_defaults_not_zero(self):
        """§3: no proxy: and no caps: must stay today's static fill — 30/10 —
        never a derived 0 that would stall the board."""
        assert ct.CAPS_MIGRATION_DEFAULTS == {"max_in_progress": 30,
                                              "max_in_progress_per_profile": 10}
        _, tpl = _parse(_v4())
        assert ct.derived_max_in_progress(tpl) is None


# ── §7 summary line ──────────────────────────────────────────────────────────

class TestDeclarationSummary:
    def _tpl(self):
        fams = [{"name": "coding", "models": [{"recipe": "c.yaml"}],
                 "workers": 1, "proxy": True},
                {"name": "fast", "models": [{"recipe": "c.yaml"}],
                 "workers": 1, "proxy": True}]
        return _parse(_v4(
            families=fams,
            proxy={"families": {"coding": {"routing": "least-in-flight",
                                           "max_parallel_requests": 3},
                                "fast": {"routing": "least-in-flight",
                                         "max_parallel_requests": 3}}},
            endpoints={"mode": "proxy-alias", "chat": "family-coding",
                       "delegation": "family-coding",
                       "profiles": {"default": "family-coding"}},
            caps={"max_in_progress": 6, "expected_requests_per_card": 1}))

    def test_summary_reports_all_three_blocks(self):
        """§7's example line is illustrative prose, so the pin is on
        content + shape: three segments, each naming what it declares."""
        _, tpl = self._tpl()
        s = ct.declaration_summary(tpl)
        assert s.count("; ") >= 2, s
        assert "proxy: 2 families, balancing=least-in-flight (global)" in s
        assert "endpoints: chat family-coding, delegation family-coding" in s
        assert "profiles default->family-coding" in s
        assert "caps: max_in_progress=6 (declared" in s
        assert "derived 6 from 2 declared backends" in s

    def test_summary_of_a_template_with_no_blocks_is_empty(self):
        _, tpl = _parse(_v4())
        assert ct.declaration_summary(tpl) == ""

    def test_summary_omits_a_block_that_is_absent(self):
        _, tpl = _parse(_v4(caps={"max_in_progress": 6}))
        s = ct.declaration_summary(tpl)
        assert "caps:" in s and "proxy:" not in s and "endpoints:" not in s


# ── §5 plan-level leg: declared target must have a proxy ─────────────────────

class TestPlanLevelEndpointLeg:
    """validate_resolved_plan gains: under uniform_pool every declared endpoint
    target must resolve to a family that HAS a proxy."""

    @pytest.fixture(autouse=True)
    def _recipes_exist(self, monkeypatch):
        """The plan layer checks recipe existence against disk; these fixtures
        name fictional recipes, so stub the predicate (the recipe-existence leg
        has its own coverage in test_apply.py / test_validate.py)."""
        import recipe_cost as rc
        monkeypatch.setattr(rc, "recipe_exists", lambda r: True)

    def _plan(self, proxy_flag, endpoints):
        d = {"name": "p", "version": 4, "uniform_pool": True,
             "orchestrator": {"recipe": "same.yaml"},
             "families": [{"name": "coding", "models": [{"recipe": "same.yaml"}],
                           "workers": 2, "proxy": proxy_flag}],
             "endpoints": endpoints}
        topo = _FakeTopo(_FakeNode("10.0.0.1"),
                         [_FakeNode("10.0.0.2"), _FakeNode("10.0.0.3")])
        return ti.resolve(ti.ClusterTemplate.from_dict(d), topo,
                          _coster=lambda r: _rc_cost(r))

    def test_target_family_without_a_proxy_is_a_plan_error(self):
        plan = self._plan(False, {"chat": "family-coding"})
        errs = ct.validate_resolved_plan(plan)
        assert any("endpoints.chat -> 'family-coding'" in e and
                   "has no proxy" in e for e in errs), errs

    def test_target_family_with_a_proxy_passes(self):
        plan = self._plan(True, {"chat": "family-coding",
                                 "delegation": "family-coding"})
        assert ct.validate_resolved_plan(plan) == []

    def test_profiles_targets_are_checked_too(self):
        plan = self._plan(False, {"profiles": {"default": "family-coding"}})
        errs = ct.validate_resolved_plan(plan)
        assert any("endpoints.profiles.default" in e for e in errs), errs

    def test_leg_does_not_fire_for_a_non_uniform_plan(self):
        """§5 scopes this to uniform_pool; §1.3 exempts a role-split plan."""
        d = {"name": "p", "version": 4,
             "orchestrator": {"recipe": "same.yaml"},
             "families": [{"name": "coding", "models": [{"recipe": "same.yaml"}],
                           "workers": 2, "proxy": False}],
             "endpoints": {"chat": "family-coding"}}
        topo = _FakeTopo(_FakeNode("10.0.0.1"),
                         [_FakeNode("10.0.0.2"), _FakeNode("10.0.0.3")])
        plan = ti.resolve(ti.ClusterTemplate.from_dict(d), topo,
                          _coster=lambda r: _rc_cost(r))
        assert ct.validate_resolved_plan(plan) == []

    def test_plan_without_the_block_is_unaffected(self):
        plan = self._plan(True, {})
        assert ct.validate_resolved_plan(plan) == []


# ── round-trip hygiene for the new blocks ────────────────────────────────────

class TestV4RoundTrip:
    def test_declarations_round_trip(self):
        raw = _v4(endpoints={"chat": "family-coding"},
                  proxy={"families": {"coding": {"routing": "least-in-flight"}}},
                  caps={"max_in_progress": 6})
        tpl = ti.ClusterTemplate.from_dict(raw)
        out = tpl.to_dict()
        assert out["endpoints"] == {"chat": "family-coding"}
        assert out["proxy"] == {"families": {"coding": {
            "routing": "least-in-flight"}}}
        assert out["caps"] == {"max_in_progress": 6}

    def test_absent_blocks_are_omitted_not_emitted_as_null(self):
        out = ti.ClusterTemplate.from_dict(_v4()).to_dict()
        assert "endpoints" not in out and "proxy" not in out and "caps" not in out

    def test_to_dict_does_not_share_nested_structure(self):
        """Mutating a caller's copy must not edit the template object —
        otherwise the next render silently differs."""
        tpl = ti.ClusterTemplate.from_dict(
            _v4(proxy={"families": {"coding": {"routing": "least-in-flight"}}}))
        out = tpl.to_dict()
        out["proxy"]["families"]["coding"]["routing"] = "mutated"
        assert tpl.proxy["families"]["coding"]["routing"] == "least-in-flight"
        assert tpl.to_dict()["proxy"]["families"]["coding"]["routing"] == \
            "least-in-flight"

    def test_declarations_never_reach_serving_json(self):
        """serving.json is a fleet artifact consumed by hscc-roles/bootstrap.
        Carrying a declaration into it would change what downstream readers
        parse — C2's write path owns that, not the schema."""
        d = _v4_uniform(endpoints={"chat": "family-coding"},
                        proxy={"families": {"coding": {
                            "routing": "least-in-flight"}}},
                        caps={"max_in_progress": 6})
        topo = _FakeTopo(_FakeNode("10.0.0.1"),
                         [_FakeNode("10.0.0.2"), _FakeNode("10.0.0.3")])
        plan = ti.resolve(ti.ClusterTemplate.from_dict(d), topo,
                          _coster=lambda r: _rc_cost(r))
        text = json.dumps(ti.to_serving_json(plan))
        assert "endpoints" not in text and "least-in-flight" not in text


# ── THE OVERREACH GUARD ──────────────────────────────────────────────────────
#
# §7: "A v3 non-uniform template's rendered output must not change a single
# byte; pin with an overreach guard test as t_ac795424 pinned its non-uniform
# branch."
#
# Provenance is the point: _v3_nonuniform_golden.json was captured by
# capture_v3_golden.py running against the PRE-CHANGE tree (commit 9bc991e2,
# parent 42924ce8, before a line of implementation landed). A golden generated
# after the change and then compared to the post-change code proves nothing —
# this one cannot be quietly re-baselined, because re-capturing it would be
# visible in git as a diff to this file.

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_v3_nonuniform_golden.json")

from dataclasses import dataclass


@dataclass
class _FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class _FakeTopo:
    orchestrator: _FakeNode
    workers: list


def _rc_cost(recipe):
    import recipe_cost as rc
    return rc.RecipeCost(recipe, per_gpu_total_gb=30, fits=True,
                         tensor_parallel=1)


V3_NON_UNIFORM = {
    "name": "v3-nonuniform-guard",
    "version": 3,
    "description": "overreach-guard fixture",
    "orchestrator": {"recipe": "orch.yaml"},
    "families": [
        {"name": "coding", "models": [{"recipe": "work.yaml"}],
         "workers": 1, "proxy": True},
        {"name": "reasoning", "models": [{"recipe": "work.yaml"}],
         "workers": "remaining", "proxy": False},
    ],
    "routing": {"delegation": "family-coding",
                "compaction": "orchestrator",
                "auxiliaries": "family-reasoning"},
}


def _render_v3_non_uniform():
    tpl = ti.ClusterTemplate.from_dict(copy.deepcopy(V3_NON_UNIFORM))
    plan = ti.resolve(tpl, _FakeTopo(_FakeNode("10.0.0.1"),
                                     [_FakeNode("10.0.0.2"),
                                      _FakeNode("10.0.0.3")]),
                      _coster=_rc_cost)

    def serving(url, api_key=None):
        return json.dumps({"data": [{"id": m} for m in
                                    ("orchestrator-model", "worker-model")]})

    return {
        "template_to_dict": tpl.to_dict(),
        "serving_json": ti.to_serving_json(plan),
        "hermes_config": ct._update_hermes_config({}, plan, _http_get=serving),
        "routing_preview": ct._preview_routing(tpl, plan, _http_get=serving),
    }


class TestOverreachGuard:
    def test_v3_non_uniform_renders_byte_identical_to_pre_change(self):
        """Every byte of what a v3 NON-uniform template renders — parsed
        template, serving.json, the hermes config writer, and the routing
        preview — is unchanged by the endpoints/proxy/caps work."""
        with open(_GOLDEN) as fh:
            golden = json.load(fh)
        rendered = _render_v3_non_uniform()
        # Compare the SERIALIZED form, not the dicts: key ORDER is part of what
        # a downstream reader sees, and dict equality would hide a reorder.
        assert (json.dumps(rendered, indent=2, sort_keys=False)
                == json.dumps(golden, indent=2, sort_keys=False))

    def test_the_new_fields_are_absent_from_a_v3_render_by_construction(self):
        """Not one of the three blocks may appear anywhere in the v3 output —
        including as an explicit null, which is what a careless `out[k] =
        self.k` would have emitted.

        Checked on the PARSED template dict rather than by substring-scanning
        the whole render: a v3 family legitimately carries `proxy: false` (the
        v2 boolean for "this family has no proxy"), which has nothing to do with
        the v4 `proxy:` block and must not be flagged as a leak.
        """
        rendered = _render_v3_non_uniform()
        out = rendered["template_to_dict"]
        for key in ("endpoints", "proxy", "caps"):
            assert key not in out, f"{key!r} leaked into a v3 template render"
        text = json.dumps(rendered)
        for value in ("least-in-flight", "max_in_progress",
                      "expected_requests_per_card", "max_parallel_requests"):
            assert value not in text, f"{value!r} leaked into a v3 render"

    def test_v3_non_uniform_still_validates_with_no_new_warnings(self):
        """The declaration validators must be no-ops when the blocks are
        absent — a warning on an untouched template is its own overreach."""
        errs, warns = _check(copy.deepcopy(V3_NON_UNIFORM))
        assert errs == [] and warns == []

    def test_the_captured_golden_is_still_the_thing_being_pinned(self):
        """Meta-pin: the guard would pass vacuously if the golden stopped
        containing the interesting shapes. Assert the fixture actually carries
        a non-uniform routing render + a direct-node custom provider."""
        with open(_GOLDEN) as fh:
            golden = json.load(fh)
        assert golden["template_to_dict"]["version"] == 3
        assert "uniform_pool" not in golden["template_to_dict"]
        assert golden["hermes_config"]["providers"][0]["name"] == "custom"
        # The pre-change non-uniform custom entry names the orchestrator node
        # directly — exactly what t_ac795424 left alone, and what a v4
        # overreach would have rewritten.
        assert golden["hermes_config"]["providers"][0]["base_url"] == \
            "http://10.0.0.1:8000/v1"
        assert len(golden["routing_preview"]["routing"]) == 3
