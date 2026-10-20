"""Positive-path apply tests for the v4 declarations (spec §1.2 / §2.3 / §3).

The overreach guard proves the OLD path is untouched. This file proves the NEW
path actually does something — the two are complementary and only the first one
existed after the first implementation pass, which is exactly how four mutation
probes survived it (a leg that ignores its declaration, a source that never
reports "declared", and an applied record that emits nothing were all invisible).

Fakes only: fake topology, injected _http_get, tmp state paths, injected write
seam. No live cluster, no live sparkrun, no live ~/.config/sparkrun, no network.
"""

import copy
import json
import os
import sys

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import cluster_template as ct              # noqa: E402
import template_intent as ti               # noqa: E402
import recipe_cost as rc                   # noqa: E402

from _capture_apply_golden import FakeNode, FakeTopo  # noqa: E402

ORCH = "10.0.0.1"
W = ["10.0.0.2", "10.0.0.3"]


def _cost(recipe):
    return rc.RecipeCost(recipe, per_gpu_total_gb=30, fits=True,
                         tensor_parallel=1)


def _serving(*ids):
    def get(url, api_key=None):
        return json.dumps({"data": [{"id": m} for m in ids]})
    return get


def _plan(d):
    tpl = ti.ClusterTemplate.from_dict(copy.deepcopy(d))
    topo = FakeTopo(FakeNode(ORCH), [FakeNode(w) for w in W])
    return tpl, ti.resolve(tpl, topo, _coster=_cost)


def _v4_uniform(**extra):
    d = {"name": "declared-uniform", "version": 4, "uniform_pool": True,
         "orchestrator": {"recipe": "same.yaml"},
         "families": [{"name": "coding",
                       "models": [{"recipe": "same.yaml"}],
                       "workers": "remaining", "proxy": True}]}
    d.update(extra)
    return d


def _v4_nonuniform(**extra):
    d = {"name": "declared-nonuniform", "version": 4,
         "orchestrator": {"recipe": "orch.yaml"},
         "families": [{"name": "coding",
                       "models": [{"recipe": "work.yaml"}],
                       "workers": 1, "proxy": True},
                      {"name": "reasoning",
                       "models": [{"recipe": "work.yaml"}],
                       "workers": "remaining", "proxy": True}]}
    d.update(extra)
    return d


# ── §1.2: endpoints is the ONE computation point ─────────────────────────────


class TestEndpointsDecision:
    def test_a_declared_chat_reports_source_declared_not_default(self):
        """The `source` field is what the report and applied state show; a
        declaration that silently downgrades to "default" would be indistinguishable
        from an omission, and the whole §1.5 disclosure rests on telling them apart.
        """
        _, plan = _plan(_v4_uniform(endpoints={"chat": "orchestrator"}))
        d = ct._endpoints_decision(plan)
        assert d is not None and d["mode"] == "proxy-alias"
        assert d["chat"]["source"] == "declared", d["chat"]
        # proxy-alias doctrine: the declared orchestrator target is the POOL
        # front, never a node address.
        assert d["chat"]["url"] == "http://localhost:4000/v1"
        assert "10.0.0." not in d["chat"]["url"]

    def test_under_uniform_pool_an_omitted_delegation_keeps_the_pool(self):
        """§1.2: under uniform_pool EVERY hermes-facing consumer resolves to the
        family proxy, so an omitted delegation is source "default" pointing at
        the pool — which is what apply already did before this feature (the
        t_ac795424 worker re-aim). Pinning it as "default" rather than "declared"
        is what keeps it out of §1.5's change disclosure."""
        _, plan = _plan(_v4_uniform(endpoints={"chat": "orchestrator"}))
        d = ct._endpoints_decision(plan)
        assert d["delegation"]["source"] == "default"
        assert d["delegation"]["url"] == "http://localhost:4000/v1"
        assert d["delegation"]["target"] == "family-coding"

    def test_on_a_non_uniform_template_an_omitted_consumer_is_untouched(self):
        """§1.3: "omitting an endpoint means leave it as tuned, not route it."
        On a non-uniform template that DID declare a block, an omitted
        delegation is None (skipped by every leg), NOT silently re-aimed."""
        _, plan = _plan(_v4_nonuniform(endpoints={"chat": "family-coding"}))
        d = ct._endpoints_decision(plan)
        assert d["delegation"] is None
        assert "delegation" in d["untouched"]

    def test_declared_delegation_overrides_the_pool_default(self):
        """A declared delegation to a DIFFERENT family must win — if the leg
        ignored the block and used the default family, the URL would be the same
        as chat's and the test would pass for the wrong reason. Two distinct
        proxied families make the difference observable."""
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding", "delegation": "family-reasoning"}))
        d = ct._endpoints_decision(plan)
        assert d["delegation"]["source"] == "declared"
        assert d["delegation"]["url"] != d["chat"]["url"], (
            "delegation resolved to chat's front — the block was ignored")
        assert d["delegation"]["target"] == "family-reasoning"

    def test_a_v3_template_gets_no_decision_at_all(self):
        """The §1.3 exemption door. Widening it (returning a dict for a v3
        template) is the single change that would make every byte-identity pin
        meaningless, so it is pinned on the way IN as well as on the golden."""
        d = {"name": "v3-plain", "version": 3,
             "orchestrator": {"recipe": "orch.yaml"},
             "families": [{"name": "coding",
                           "models": [{"recipe": "work.yaml"}],
                           "workers": 1, "proxy": True}]}
        _, plan = _plan(d)
        assert ct._endpoints_decision(plan) is None

    def test_declared_chat_lands_the_same_value_in_model_and_custom(self):
        """§1.2 row 2: model.base_url and custom.base_url must be LITERALLY the
        same value, not two separately crafted URLs. A leg that computed the two
        independently is precisely the bug this feature exists to prevent."""
        _, plan = _plan(_v4_uniform(endpoints={"chat": "orchestrator"}))
        d = ct._endpoints_decision(plan)
        cfg = ct._update_hermes_config({}, plan, _http_get=_serving(
            "orchestrator-model", "worker-model"), decision=d)
        custom = [p for p in cfg["providers"] if p["name"] == "custom"]
        assert custom, "no custom provider written"
        assert cfg["model"]["base_url"] == custom[0]["base_url"]
        assert cfg["model"]["base_url"] == "http://localhost:4000/v1"

    def test_explicit_mode_targets_stay_symbolic_and_land_on_the_node(self):
        """§1.1: "A target is never an address" — `explicit` does NOT widen the
        vocabulary, it changes what apply WRITES for a resolved target (a
        concrete endpoint instead of the proxy front). `orchestrator` under
        explicit therefore resolves to the orchestrator's own URL, and an
        address-valued target must be refused rather than honoured."""
        _, plan = _plan(_v4_nonuniform(endpoints={
            "mode": "explicit", "chat": "orchestrator"}))
        d = ct._endpoints_decision(plan)
        assert d["mode"] == "explicit"
        assert d["chat"]["source"] == "declared"
        assert d["chat"]["url"] == f"http://{ORCH}:8000/v1"

    def test_an_address_valued_target_is_refused_not_written(self):
        """§1.1 again, from the other side: a host-shaped value never becomes a
        route. Node addresses appear only in apply's OUTPUT."""
        _, plan = _plan(_v4_nonuniform(endpoints={
            "mode": "explicit", "chat": f"{ORCH}:8000"}))
        d = ct._endpoints_decision(plan)
        assert d["chat"]["url"] is None and d["chat"]["reason"]

    def test_unresolvable_declared_target_is_refused_not_written(self):
        """A declared target with no live endpoint must REFUSE the write
        (nothing written) rather than pin a URL that 404s."""
        _, plan = _plan(_v4_uniform(endpoints={"chat": "family-nope"}))
        d = ct._endpoints_decision(plan)
        assert d["chat"]["url"] is None and d["chat"]["reason"]
        cfg = ct._update_hermes_config({}, plan, _http_get=_serving(),
                                       decision=d)
        # model.base_url must NOT have been set to a dead front.
        assert cfg.get("model", {}).get("base_url") is None


# ── §2.3: the declared proxy region reaches sparkrun's write path ────────────


class TestProxyLegWritesWhatWasDeclared:
    def _call(self, tpl, plan, outcome=None, calls=None):
        calls = calls if calls is not None else []

        def _write(regions):
            calls.append(regions)
            return outcome or {"ok": True, "error": None, "mode": "direct",
                               "changed": True}
        return ct._apply_proxy_declaration(tpl, plan, _write=_write), calls

    def test_declared_routing_lands_the_exact_region_sparkrun_reads(self):
        tpl, plan = _plan(_v4_uniform(proxy={"families": {
            "coding": {"routing": "least-in-flight"}}}))
        res, calls = self._call(tpl, plan)
        assert calls == [{"gateways": {"litellm":
                                       {"balancing": "least-in-flight"}}}]
        assert res["source"] == "declared" and res["written"] is True
        # §2.3 restart rule: this card never restarts, it reports.
        assert res["restart_required"] is True
        assert "cutover" in res["note"]

    def test_declared_policy_beats_the_uniform_pool_default(self):
        tpl, plan = _plan(_v4_uniform(proxy={"families": {
            "coding": {"routing": "least-busy"}}}))
        res, calls = self._call(tpl, plan)
        assert res["policy"] == "least-busy" and res["source"] == "declared"
        assert calls[0]["gateways"]["litellm"]["balancing"] == "least-busy"

    def test_undeclared_uniform_pool_apply_still_gets_the_default(self):
        """§2.2's operator decision: uniform_pool without a declaration gets
        least-in-flight, and the source says so — a reader must be able to tell
        the default from a declaration."""
        tpl, plan = _plan(_v4_uniform())
        res, calls = self._call(tpl, plan)
        assert res["source"] == "uniform_pool-default"
        assert calls[0]["gateways"]["litellm"]["balancing"] == "least-in-flight"

    def test_write_failure_is_reported_and_nothing_is_hand_written(self):
        tpl, plan = _plan(_v4_uniform(proxy={"families": {
            "coding": {"routing": "least-in-flight"}}}))
        res, _ = self._call(tpl, plan, outcome={"ok": False,
                                                "error": "sparkrun unreachable"})
        assert res["written"] is False and "unreachable" in res["error"]
        assert "nothing was hand-written" in res["note"]

    def test_inert_knobs_are_recorded_never_sent_to_sparkrun(self):
        """§8 is law: an INERT knob may not be smuggled into proxy.yaml as if
        it worked. It is recorded as pending the §4.3 hook, and the write
        payload carries ONLY the live balancing region."""
        tpl, plan = _plan(_v4_uniform(proxy={"families": {
            "coding": {"routing": "least-in-flight",
                       "max_parallel_requests": 2, "timeout": 600}}}))
        res, calls = self._call(tpl, plan)
        assert calls[0] == {"gateways": {"litellm":
                                         {"balancing": "least-in-flight"}}}
        inert = res["inert_recorded"]
        assert inert and inert[0]["family"] == "coding"
        assert set(inert[0]["knobs"]) == {"max_parallel_requests", "timeout"}
        assert "INERT" in inert[0]["status"]


# ── §3: declared caps reach the consumer that owns them ─────────────────────


class TestCapsLeg:
    def test_declared_caps_are_written_to_hermes_kanban(self, tmp_path):
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({"model": {"default": "m"}}, conf.open("w"))
        tpl, _ = _plan(_v4_uniform(caps={"max_in_progress": 6,
                                         "max_in_progress_per_profile": 2}))
        res = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert res["written"] is True and res["changed"] is True
        assert res["keys"] == ["kanban.max_in_progress",
                               "kanban.max_in_progress_per_profile"]
        on_disk = yaml.safe_load(conf.read_text())
        assert on_disk["kanban"] == {"max_in_progress": 6,
                                     "max_in_progress_per_profile": 2}
        # Untouched sibling keys survive the read-modify-write.
        assert on_disk["model"] == {"default": "m"}

    def test_write_is_idempotent(self, tmp_path):
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({}, conf.open("w"))
        tpl, _ = _plan(_v4_uniform(caps={"max_in_progress": 6}))
        assert ct._apply_caps_declaration(tpl, config_yaml=conf)["changed"]
        again = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert again["changed"] is False, "second apply rewrote identical caps"

    def test_declared_beats_derived_and_says_so(self, tmp_path):
        """§3.1 precedence, made visible in the note rather than left to
        inference. The derivation needs at least one family declaring
        `max_parallel_requests` (§3.1's only input), so the fixture declares it."""
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({}, conf.open("w"))
        tpl, _ = _plan(_v4_uniform(
            caps={"max_in_progress": 6, "expected_requests_per_card": 2},
            proxy={"families": {"coding": {"routing": "least-in-flight",
                                           "max_parallel_requests": 4}}}))
        res = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert res["declared"] == {"max_in_progress": 6}
        assert res["derived"]["value"] == 2 and "declared wins" in res["note"]
        assert yaml.safe_load(conf.read_text())["kanban"]["max_in_progress"] == 6

    def test_derived_only_is_reported_never_written(self, tmp_path):
        """§3.1 + §8: the derivation's only input is INERT, so a derived cap is
        advisory. Writing it would present an unenforced number as a ceiling."""
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({}, conf.open("w"))
        tpl, _ = _plan(_v4_uniform(
            caps={"expected_requests_per_card": 2},
            proxy={"families": {"coding": {"routing": "least-in-flight",
                                           "max_parallel_requests": 4}}}))
        res = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert res["written"] is False and res["derived"]["value"] == 2
        assert "NOT written" in res["note"]
        assert "kanban" not in yaml.safe_load(conf.read_text())

    def test_undeclared_caps_write_nothing_at_all(self, tmp_path):
        """§3.1/§7: the 30/10 migration default is bootstrap's (enable_plugins),
        not apply's. Two owners of one key is the failure mode here."""
        conf = tmp_path / "config.yaml"
        yaml.safe_dump({}, conf.open("w"))
        tpl, _ = _plan(_v4_uniform())
        res = ct._apply_caps_declaration(tpl, config_yaml=conf)
        assert res["written"] is False
        assert yaml.safe_load(conf.read_text()) == {}
        assert "migration default stands" in res["note"]


# ── §7: the applied record tells doctor what was declared ────────────────────


class TestAppliedDeclarationRecord:
    def test_a_declared_apply_records_declared_and_resolved(self):
        tpl, plan = _plan(_v4_uniform(endpoints={"chat": "orchestrator"},
                                      caps={"max_in_progress": 6},
                                      proxy={"families": {
                                          "coding": {"routing":
                                                     "least-in-flight"}}}))
        d = ct._endpoints_decision(plan)
        ep = {"resolved": {"chat": {"url": "http://localhost:4000/v1"}}}
        px = {"policy": "least-in-flight", "source": "declared",
              "written": True, "restart_required": True, "inert_recorded": []}
        cp = {"keys": ["kanban.max_in_progress"], "derived": None}
        rec = ct._applied_declaration_record(tpl, plan, d, ep, px, cp)
        assert set(rec) == {"endpoints", "proxy", "caps"}
        assert rec["endpoints"]["declared"] == tpl.endpoints
        assert rec["endpoints"]["resolved"] == ep["resolved"]
        assert rec["proxy"]["balancing_source"] == "declared"
        assert rec["caps"]["written_keys"] == ["kanban.max_in_progress"]

    def test_an_undeclared_apply_records_nothing(self):
        tpl, plan = _plan({"name": "v3-plain", "version": 3,
                           "orchestrator": {"recipe": "orch.yaml"},
                           "families": [{"name": "coding",
                                         "models": [{"recipe": "work.yaml"}],
                                         "workers": 1, "proxy": True}]})
        assert ct._applied_declaration_record(tpl, plan, None, {}, {}, {}) == {}

    def test_a_default_only_uniform_apply_records_nothing(self):
        """§2.2's default is an operator preference the template never stated;
        recording it as a declaration would make the applied state lie about
        what the operator wrote. doctor reads `uniform_pool` for that case."""
        tpl, plan = _plan(_v4_uniform())
        px = {"policy": "least-in-flight", "source": "uniform_pool-default",
              "written": True, "restart_required": False, "inert_recorded": []}
        assert ct._applied_declaration_record(tpl, plan, None, {}, px, {}) == {}


# ── §1.2 row 7: declared role profiles actually get written ─────────────────


class TestDeclaredProfileWrites:
    """§1.1: `endpoints.profiles` keys are PROFILE-NAME selectors (a selector
    matching the directory name under `profiles/`), with `default` as the
    fallback — not role names. The declared leg is gated on
    `source == "declared"`, so a selector that never matches silently writes
    nothing: no error, no note, no changed key. That is the most expensive
    failure mode in this feature (the operator declared a route and apply
    quietly did not apply it), and it is invisible unless the WRITTEN FILE is
    inspected — hence asserting on file contents, not on the decision dict."""

    def _profiles(self, tmp_path, names):
        pd = tmp_path / "profiles"
        for n in names:
            d = pd / n
            d.mkdir(parents=True)
            yaml.safe_dump({"model": {"base_url": "http://old:1/v1",
                                      "default": "stale-id"}},
                           (d / "config.yaml").open("w"))
        return pd

    def _written(self, pd, name):
        return yaml.safe_load((pd / name / "config.yaml").read_text())

    def test_a_glob_selector_rewrites_the_matching_profile(self, tmp_path):
        """`*-orch` names the orchestrator-role profiles by directory name; the
        file must come out pointing at the declared target with a probe-confirmed
        id (both keys move together — a proxy URL plus a stale id 404s)."""
        pd = self._profiles(tmp_path, ["worker", "reviewer-orch"])
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding", "profiles": {"*-orch": "family-reasoning"}}))
        d = ct._endpoints_decision(plan, profiles_dir=pd)
        assert d["profiles"]["reviewer-orch"]["source"] == "declared"
        assert d["profiles"]["reviewer-orch"]["selector"] == "*-orch"
        res = ct._apply_endpoints(plan, d, profiles_dir=pd,
                                  _http_get=_serving("orchestrator-model",
                                                     "worker-model"))
        orch = self._written(pd, "reviewer-orch")
        # reasoning is the plan's SECOND family, so its proxy is :4001 — pinning
        # the exact port is what proves the selector routed to the DECLARED
        # family and not to chat's (coding, :4000).
        assert orch["model"]["base_url"] == "http://localhost:4001/v1"
        assert orch["model"]["default"] != "stale-id"
        assert "profiles/reviewer-orch/config.yaml:model" in res["keys_written"]

    def test_a_selector_that_matches_no_profile_is_reported_not_silent(
            self, tmp_path):
        """A typo'd selector (`woker`) matches no directory. Apply must not claim
        it routed anything — and the profile keeps its tuned values."""
        pd = self._profiles(tmp_path, ["worker"])
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding", "profiles": {"woker": "family-reasoning"}}))
        d = ct._endpoints_decision(plan, profiles_dir=pd)
        res = ct._apply_endpoints(plan, d, profiles_dir=pd,
                                  _http_get=_serving("worker-model"))
        assert not [k for k in res["keys_written"] if k.startswith("profiles/")], (
            f"nothing matched the selector yet {res['keys_written']} claimed a write")
        assert self._written(pd, "worker")["model"]["default"] == "stale-id"

    def test_the_default_selector_governs_every_unlisted_profile(
            self, tmp_path):
        """§1.1: `default` is the fallback for any profile whose selector is not
        listed — so a profile named by nothing else still gets the declared
        target, and one named by a glob does not get double-routed."""
        pd = self._profiles(tmp_path, ["worker", "reviewer-orch"])
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding",
            "profiles": {"default": "family-coding",
                         "*-orch": "family-reasoning"}}))
        d = ct._endpoints_decision(plan, profiles_dir=pd)
        assert d["profiles"]["worker"]["selector"] == "default"
        assert d["profiles"]["reviewer-orch"]["selector"] == "*-orch"
        # The two selectors point at different families, so the two profiles
        # resolve differently — a `default` that swallowed everything would not.
        assert (d["profiles"]["worker"]["target"]
                != d["profiles"]["reviewer-orch"]["target"])

    def test_exact_selector_wins_over_a_matching_glob(self, tmp_path):
        """Both selectors match `reviewer-orch`; §1.1's precedence says the exact
        one governs, so the two targets are not interchangeable."""
        pd = self._profiles(tmp_path, ["reviewer-orch"])
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding",
            "profiles": {"*-orch": "family-reasoning",
                         "reviewer-orch": "family-coding"}}))
        d = ct._endpoints_decision(plan, profiles_dir=pd)
        assert d["profiles"]["reviewer-orch"]["selector"] == "reviewer-orch"
        assert d["profiles"]["reviewer-orch"]["target"] == "family-coding"

    def test_a_profile_target_that_cannot_confirm_an_id_writes_nothing(
            self, tmp_path):
        """Probe-before-write applies to profiles exactly as it does to the
        config consumers: an endpoint that cannot confirm the id is REFUSED and
        the profile keeps its previous (good) values."""
        pd = self._profiles(tmp_path, ["reviewer-orch"])
        before = (pd / "reviewer-orch" / "config.yaml").read_text()
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding",
            "profiles": {"reviewer-orch": "family-reasoning"}}))
        d = ct._endpoints_decision(plan, profiles_dir=pd)
        res = ct._apply_endpoints(plan, d, profiles_dir=pd,
                                  _http_get=lambda url, api_key=None: None)
        assert (pd / "reviewer-orch" / "config.yaml").read_text() == before
        entry = res["resolved"]["profiles.reviewer-orch"]
        # The contract is "no id confirmed ⇒ nothing written"; the status
        # string is the probe's own vocabulary (error/unreachable), so assert
        # the refusal happened, not which word the probe used.
        assert entry["model"] is None and entry["probe"] != "ok"
        assert not [k for k in res["keys_written"] if k.startswith("profiles/")]

    def test_a_profile_the_declaration_does_not_name_keeps_the_legacy_path(
            self, tmp_path):
        """§1.3: "omitting an endpoint means leave it as tuned, not route it" —
        but "tuned" here is what apply has ALWAYS done, not "frozen": a
        worker-tier profile is still re-aimed at the pool by the pre-existing
        intent heuristics. The decision says so explicitly (`source: legacy`)
        instead of implying the profile was left alone."""
        pd = self._profiles(tmp_path, ["worker"])
        _, plan = _plan(_v4_nonuniform(endpoints={
            "chat": "family-coding", "profiles": {"reviewer-orch": "family-coding"}}))
        d = ct._endpoints_decision(plan)
        assert d["profiles"]["worker"]["source"] == "legacy"
        # The declared leg itself writes nothing for a legacy profile: run it
        # with a profiles dir where only `worker` exists and no selector names
        # it, and no profile key may appear in the report.
        res = ct._apply_endpoints(plan, d, profiles_dir=pd,
                                  _http_get=_serving("worker-model"))
        assert not [k for k in res["keys_written"] if k.startswith("profiles/")]


# ── §1.5: preview must not drift from apply ─────────────────────────────────


def test_preview_discloses_the_declared_endpoints():
    """§1.5: preview and apply must not drift — the rows preview shows are built
    by the SAME helpers `_apply_endpoints` uses. Only DECLARED consumers appear:
    a defaulted or omitted one is not a change the operator is about to make."""
    tpl, plan = _plan(_v4_uniform(endpoints={"chat": "orchestrator"}))
    d = ct._endpoints_decision(plan)
    rows = ct._preview_endpoints(plan, d, _http_get=_serving(
        "orchestrator-model", "worker-model"))
    assert [r["consumer"] for r in rows] == ["chat"], rows
    assert rows[0]["url"] == "http://localhost:4000/v1"
    assert rows[0]["model"] == "orchestrator-model"


def test_preview_shows_nothing_when_nothing_is_declared():
    """An untouched template must not be shown a routing change it is not
    making — preview's disclosure is what the operator approves."""
    tpl, plan = _plan(_v4_uniform())
    assert ct._preview_endpoints(plan, ct._endpoints_decision(plan),
                                 _http_get=_serving()) == []
