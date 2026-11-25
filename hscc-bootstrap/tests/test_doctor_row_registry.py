"""The doctor feature-row registry (t_32a06db9).

The hotspot this card removes: every feature row used to be wired into
``run_doctor()`` by editing the SAME three hunks — an adapter appended after the
previous one, a keyword appended to the signature, an entry appended to the check
list. PR #175 vs #173 collided in all three and needed the t_3f805c4f rebase. The
wiring is now ``doctor.FEATURE_CHECK_ROWS`` and a loop, so a new row is ONE edit
in ONE place.

What is pinned here, and why each one is the test that matters:

1. The registry is SELF-CONSISTENT — every hook name an entry names actually
   exists on the doctor module. That is the one thing a registry loses versus a
   direct call (a typo used to be a NameError at import; a string name is not
   checked until the row builds, i.e. until someone runs doctor on a live box).
2. A NEW row needs NO change to ``run_doctor()`` — appended to the list, it
   renders. Mutation-proven: with the loop replaced by the old four literal
   calls this test fails.
3. The seam map is VALIDATED — an unknown keyword raises instead of being
   ignored, so a typo'd seam can never silently downgrade a row to its refusal.
4. The generic build path keeps the three-source trust order, the pytest fence,
   the UNVERIFIABLE row, the ``hscc_dir`` pass-through and the five-field copy —
   the same contract each feature's own suite pins for its own row.
5. Order and multiplicity: rows render in registry order, each exactly once, in
   the same position the four hand-written entries had.

The feature rows' own verdicts and fences are NOT re-pinned here —
test_doctor_orch_waker.py, test_doctor_cron_watchers.py,
test_doctor_supervision.py, test_doctor_kanban_enforce.py and
test_doctor_budget_hook.py own that, and the point of this card is that they
keep passing UNCHANGED.
"""

import json
import sys

import doctor
import doctor_rows

CHECK_NAMES = [
    "orch-waker (orchestrator waker)",
    "cron watchers (dep_pr/escalate/alone_sweep)",
    "daemon supervision (service manager owns the pid)",
    "kanban enforce (paused-board hold)",
    "kanban current board (main orchestrator)",
    "main wakeups (daemon-owned schedule)",
    "worker budget hook ([budget] note)",
    "node pauses (operator overlay)",
    "daemon scheduling posture (launchd ProcessType=Standard)",
]

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    # sparkrun is a FATAL preflight check. A CI/dev box that has not installed
    # it must not sink this unrelated row's verdict; the probe itself is
    # covered by test_doctor.TestSparkrunCheck.
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
    return home


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


# ── the registry is self-consistent ────────────────────────────────────────

def test_registry_exists_and_covers_the_feature_rows():
    keys = [r.key for r in doctor.FEATURE_CHECK_ROWS]
    assert keys == ["orch_waker", "cron_watchers", "supervision",
                    "kanban_enforce", "main_board", "main_wakeups",
                    "budget_hook", "node_pauses",
                    "node_pause_expiry", "main_only_surface",
                    "orch_wake_subs", "process_posture"], \
        "registry order is row order"


def test_every_registry_entry_is_well_formed():
    """The property a string-named registry gives up: a bad hook name is not a
    NameError at import any more, so pin it here instead of at a live `doctor`
    run. Every name an entry names must exist on the doctor module."""
    seen_keys, seen_seams = set(), set()
    for spec in doctor.FEATURE_CHECK_ROWS:
        assert spec.key and spec.seam_kwarg.startswith("_")
        assert spec.key not in seen_keys, f"duplicate row key {spec.key}"
        assert spec.seam_kwarg not in seen_seams, \
            f"duplicate seam {spec.seam_kwarg}"
        seen_keys.add(spec.key)
        seen_seams.add(spec.seam_kwarg)
        for name in (spec.row_fn, spec.loader, spec.refusal):
            if name:
                assert callable(getattr(doctor, name)), \
                    f"{spec.key}: {name} is not callable on doctor"
        if spec.fallback_name:
            assert getattr(doctor, spec.fallback_name), \
                f"{spec.key}: fallback name {spec.fallback_name} is empty"


def test_registry_seam_names_match_the_run_doctor_signature():
    """The pre-registry seams are explicit keywords AND registry entries; if the
    two ever disagree, run_doctor() rejects a seam its own signature advertises
    (that is the mutation this pins). Rows added through the registry
    (``_main_board_check``) arrive via ``**feature_rows`` instead."""
    import inspect
    params = inspect.signature(doctor.run_doctor).parameters
    declared = {p for p in params if p.startswith("_")
                and p.endswith("_check")}
    registered = set(doctor._FEATURE_SEAM_NAMES)
    assert declared <= registered
    assert registered - declared == {"_main_board_check",
                                     "_main_wakeups_check",
                                     "_node_pauses_check",
                                     "_node_pause_expiry_check",
                                     "_main_only_surface_check",
                                     "_orch_wake_subs_check",
                                     "_process_posture_check"}
    assert "_main_board_check" not in declared, \
        "a new row must not add a keyword to run_doctor()'s signature"


# ── the anti-hotspot property ──────────────────────────────────────────────

class _ProbeFeature:
    """A stand-in for a feature module exposing doctor_check(hscc_dir=...)."""

    CHECK_NAME = "probe feature (registry test only)"

    @staticmethod
    def doctor_check(hscc_dir=None):
        return {"name": _ProbeFeature.CHECK_NAME, "ok": True, "fatal": False,
                "detail": f"probe ok (hscc_dir={hscc_dir})", "fix": "",
                "extra_field": "must be dropped by the adapter"}


class _ProbeHooks:
    """A stand-in for the doctor module, used to drive the non-pytest branch of
    ``build_row`` WITHOUT touching the real ``sys.modules``.

    ``build_row`` reads the fence off the module it is handed, so a fake with
    its own ``sys.modules`` is enough — whereas ``monkeypatch.delitem(sys.modules,
    "pytest")`` around a full ``run_doctor()`` call would un-fence every OTHER
    check in the list (the notifier-mask and GPU-clock-cap transports refuse
    only under pytest) and the suite would try to ssh the live fleet.
    """

    Check = doctor.Check

    class _NoPytest:
        modules: dict = {}          # "pytest" absent => human-CLI branch

    def __init__(self, loader=None):
        self.sys = self._NoPytest()
        self._loader = loader or (lambda: _ProbeFeature())

    def _load_probe_feature(self):
        return self._loader()

    def _refused_probe_check(self):
        return {"name": _ProbeFeature.CHECK_NAME, "ok": False, "fatal": False,
                "detail": "probe check refused inside a pytest process — "
                          "inject _probe_check",
                "fix": "pass _probe_check=lambda: {...}"}

    _PROBE_CHECK_NAME = _ProbeFeature.CHECK_NAME


def _probe_spec(**over):
    base = dict(key="probe", seam_kwarg="_probe_check",
                loader="_load_probe_feature",
                refusal="_refused_probe_check",
                fallback_name="_PROBE_CHECK_NAME",
                missing_detail="probe module not importable",
                missing_fix="re-run bootstrap")
    base.update(over)
    return doctor_rows.FeatureCheckRow(**base)


def _install_probe_feature(monkeypatch, loader=lambda: _ProbeFeature()):
    """Register a NEW row the way a feature would: append the entry, add its
    helpers to the doctor module. run_doctor() is NOT touched."""
    monkeypatch.setattr(doctor, "FEATURE_CHECK_ROWS",
                        list(doctor.FEATURE_CHECK_ROWS) + [_probe_spec()])
    monkeypatch.setattr(doctor, "_FEATURE_SEAM_NAMES",
                        doctor_rows.seam_names(doctor.FEATURE_CHECK_ROWS))
    monkeypatch.setattr(doctor, "_load_probe_feature", loader, raising=False)
    monkeypatch.setattr(doctor, "_refused_probe_check", lambda: {
        "name": _ProbeFeature.CHECK_NAME, "ok": False, "fatal": False,
        "detail": "probe check refused inside a pytest process — "
                  "inject _probe_check", "fix": "pass _probe_check=..."},
        raising=False)
    monkeypatch.setattr(doctor, "_PROBE_CHECK_NAME", _ProbeFeature.CHECK_NAME,
                        raising=False)


def _outside_pytest_branch(monkeypatch):
    """Close both channels ``doctor_rows.under_pytest()`` reads, for a direct
    ``build_row`` call.

    The registry fence used to be read off the module handed to ``build_row``,
    which is why a stand-in with an empty ``sys.modules`` was enough. It is one
    shared, child-visible predicate now (t_eb768c2a), so the process's own
    channels have to go — and they have to go from the test BODY: pytest
    re-exports ``PYTEST_CURRENT_TEST`` for the call phase after any setup
    fixture has run. ``monkeypatch`` restores both at teardown, and only this
    row is in scope, because these tests call ``build_row`` directly rather
    than un-fencing a whole ``run_doctor()``.
    """
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def test_new_row_needs_no_change_to_run_doctor(tmp_path, monkeypatch):
    """THE regression test for the hotspot. A row appended to the registry
    renders — no keyword, no check-list entry, no adapter added to run_doctor().
    If the loop ever regresses back to four literal calls, the probe row
    disappears and this fails."""
    _install_probe_feature(monkeypatch)
    res = _doctor(tmp_path, monkeypatch, _probe_check=lambda: {
        "name": _ProbeFeature.CHECK_NAME, "ok": True, "fatal": False,
        "detail": "injected probe", "fix": ""})
    row = {c["name"]: c for c in res["checks"]}[_ProbeFeature.CHECK_NAME]
    assert row["detail"] == "injected probe"
    assert row["fatal"] is False
    assert [c["name"] for c in res["checks"]].count(
        _ProbeFeature.CHECK_NAME) == 1


def test_new_row_appears_after_the_models_served_row(tmp_path, monkeypatch):
    """Position is part of the contract: feature rows sit after models-served
    and before the fleet block, so the ○/● rendering and `doctor --json` order
    do not shift when a row is added."""
    _install_probe_feature(monkeypatch)
    res = _doctor(tmp_path, monkeypatch)
    names = [c["name"] for c in res["checks"]]
    assert names.index(_ProbeFeature.CHECK_NAME) > names.index(
        "models served"), "a registered row must not jump ahead of the core rows"


def test_unknown_seam_keyword_is_rejected(tmp_path, monkeypatch):
    """A typo'd seam must fail loudly. Silently ignoring it would downgrade the
    row to its refusal verdict — inside a suite that looks like a correct
    fence, and on a live box it looks like a real failure."""
    import pytest as _pytest
    with _pytest.raises(TypeError) as exc:
        _doctor(tmp_path, monkeypatch, _orch_waker_chekr=lambda: {})
    assert "_orch_waker_chekr" in str(exc.value)


def test_registry_entry_without_hooks_is_rejected():
    """A half-written entry (no row_fn, no loader/refusal/fallback) would build
    a broken row at runtime on the operator's box; reject it at construction."""
    import pytest as _pytest
    with _pytest.raises(ValueError):
        doctor_rows.FeatureCheckRow(key="half", seam_kwarg="_half_check")


# ── the generic build path: same fence, same honesty ───────────────────────

def test_generic_row_refuses_inside_pytest_without_a_seam(tmp_path,
                                                          monkeypatch):
    """The fence a new row gets for free: no seam inside pytest ⇒ ERROR naming
    the seam, never a silent pass and never a read of the operator's state."""
    _install_probe_feature(monkeypatch)
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[_ProbeFeature.CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "refused" in row["detail"] and "_probe_check" in row["detail"]
    assert res["ok"] is True, "a refusal is never fatal"


def test_generic_row_is_unverifiable_when_module_missing(monkeypatch):
    """A deployment where the feature module is not next to bootstrap: the row
    says it CANNOT verify, never vanishes and never claims ok.

    Driven through ``build_row`` with the pytest branch forced off, NOT through
    ``run_doctor``: ``delitem(sys.modules, "pytest")`` around a whole
    run_doctor() call would also un-fence the notifier-mask and GPU-clock-cap
    blocks (their default transports refuse only under pytest) and the suite
    would ssh the live fleet. The fence tests that DO go through run_doctor()
    keep pytest in sys.modules on purpose.
    """
    # The fence is the shared, child-visible predicate now, so the view of
    # a module's ``sys.modules`` no longer decides the branch: close BOTH
    # channels here, in the body (a setup fixture is too early — pytest
    # re-exports the marker for the call phase after fixtures run).
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkey = _ProbeHooks(loader=lambda: None)
    row = doctor_rows.build_row(monkey, _probe_spec(), None)
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
    assert "re-run bootstrap" in row.fix


def test_generic_row_forwards_hscc_dir_outside_pytest(monkeypatch):
    """The human-CLI path for a generic row: the feature's own
    doctor_check(hscc_dir=...) gets the sandbox root. Bootstrap keeps HOME real
    and relocates only HSCC_DIR, so a row that dropped this would read the
    OPERATOR's ~/.hscc from a sandboxed Stage-1 run. Same reasoning as above
    for calling build_row directly rather than run_doctor."""
    # The fence is the shared, child-visible predicate now, so the view of
    # a module's ``sys.modules`` no longer decides the branch: close BOTH
    # channels here, in the body (a setup fixture is too early — pytest
    # re-exports the marker for the call phase after fixtures run).
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    row = doctor_rows.build_row(_ProbeHooks(), _probe_spec(), None,
                                hscc_dir="/sandbox/.hscc")
    assert row.detail == "probe ok (hscc_dir=/sandbox/.hscc)"


def test_generic_row_exposes_only_the_five_check_fields(monkeypatch):
    """`_ProbeFeature.doctor_check` returns a sixth field on purpose: the
    adapter must copy exactly the five, or `doctor --json` grows a shape the
    renderer does not know about."""
    # The fence is the shared, child-visible predicate now, so the view of
    # a module's ``sys.modules`` no longer decides the branch: close BOTH
    # channels here, in the body (a setup fixture is too early — pytest
    # re-exports the marker for the call phase after fixtures run).
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    row = doctor_rows.build_row(_ProbeHooks(), _probe_spec(), None)
    assert {f for f in row.__dataclass_fields__} == {"name", "ok", "detail",
                                                     "fix", "fatal"}
    assert set(vars(row)) == {"name", "ok", "detail", "fix", "fatal"}


def test_generic_row_resolves_the_fallback_name_constant(monkeypatch):
    """``fallback_name`` is the NAME OF A MODULE CONSTANT, not the name itself
    (the `_BUDGET_HOOK_CHECK_NAME` convention), so a feature verdict dict with no
    ``name`` must print the constant's VALUE — not the literal string
    "_PROBE_CHECK_NAME" to a human.

    Found by the non-pytest smoke run of this registry: the generic path handed
    the attribute name straight to the five-field adapter, and every test here
    used a feature that always returned its own name, so nothing covered the
    fallback. That is the exact class of bug a registry invites — a name that
    used to be a Python identifier in hand-written code is now data.
    """
    class _NamelessFeature:
        @staticmethod
        def doctor_check(hscc_dir=None):
            # A feature dict that forgot `name` — adapt() must not print "None"
            # nor the constant's attribute name.
            return {"ok": True, "detail": "no name here", "fix": "",
                    "fatal": False}

    # The fence is the shared, child-visible predicate now, so the view of
    # a module's ``sys.modules`` no longer decides the branch: close BOTH
    # channels here, in the body (a setup fixture is too early — pytest
    # re-exports the marker for the call phase after fixtures run).
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    hooks = _ProbeHooks(loader=lambda: _NamelessFeature())
    row = doctor_rows.build_row(hooks, _probe_spec(), None)
    assert row.name == _ProbeFeature.CHECK_NAME, row.name
    assert not row.name.startswith("_"), \
        f"the constant NAME leaked into the row: {row.name!r}"
    assert row.detail == "no name here"


# ── the four existing rows through the loop ────────────────────────────────

def test_all_feature_rows_render_exactly_once(tmp_path, monkeypatch):
    """Multiplicity is now produced by a loop instead of five literal entries:
    a duplicated registry entry would double-count a verdict and confuse the
    ○/● rendering. Also pins that no row silently vanished during the move."""
    res = _doctor(tmp_path, monkeypatch)
    names = [c["name"] for c in res["checks"]]
    for name in CHECK_NAMES:
        assert names.count(name) == 1, f"{name!r} must appear exactly once"


def test_feature_rows_keep_their_relative_order(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch)
    names = [c["name"] for c in res["checks"]]
    positions = [names.index(n) for n in CHECK_NAMES]
    assert positions == sorted(positions), \
        "the rows must keep the order they had as literal list entries"


def test_row_fn_path_resolves_the_hook_per_call(tmp_path, monkeypatch):
    """The compatibility path must look the row function up on the module each
    call: test_doctor_kanban_enforce.py swaps doctor._kanban_enforce_check_row
    to pin what reaches the row at the call site, and a registry that captured
    function objects at import time would make that patch a no-op."""
    seen = {}

    def builder(injected=None, **ctx):
        # ``**ctx``: record what the wiring actually handed this row, not the
        # builder's own parameter defaults.
        seen.update(ctx)
        seen["injected"] = injected
        return doctor.Check("kanban enforce (paused-board hold)", True,
                            detail="swapped row", fix="", fatal=False)

    monkeypatch.setattr(doctor, "_kanban_enforce_check_row", builder)
    seam = lambda: {"name": "kanban enforce (paused-board hold)", "ok": True,
                    "fatal": False, "detail": "seam", "fix": ""}
    res = _doctor(tmp_path, monkeypatch, _kanban_enforce_check=seam)
    assert seen["injected"] is seam
    # t_be690204: this row declares row_kwargs=(), so the per-call resolution
    # must deliver the seam WITHOUT a root (it used to assert the opposite).
    assert "hscc_dir" not in seen
    row = {c["name"]: c for c in res["checks"]}[
        "kanban enforce (paused-board hold)"]
    assert row["detail"] == "swapped row"


def test_the_daemon_state_rows_declare_no_root_context():
    """The wiring half of t_be690204, stated as a registry fact.

    Every writer of what these rows read resolves from HOME — the waker's
    config, the ``orch_waker``/``kanban_enforce``/``node_pause_expiry`` stream
    stamps through ``state.STATE_DIR``, the paused-policy document through
    ``kanban_pause.STATE_PATH``, the node-pause overlay and the expiry stamp's
    root through their own modules' HOME-derived constants — and none of
    them reads ``HSCC_DIR``. A root taken from that variable is therefore not a
    relocation of this state; it is a read of a directory nothing writes to, and
    the row reported absence as a fact about a live host (a pid file at that root
    even made it a fatal RED).

    ``row_kwargs=()`` is the difference between "unwired" and "un-wireable":
    ``render_rows`` filters the context by declaration, so run_doctor cannot hand
    these rows a root even by accident. t_adb66608 did exactly this for the
    cron-watcher ledger row; the call-site half lives in
    test_doctor_orch_waker.py / test_doctor_kanban_enforce.py.
    """
    by_key = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}
    for key in ("orch_waker", "kanban_enforce", "node_pauses",
                "node_pause_expiry"):
        assert by_key[key].row_kwargs == (), (
            f"the {key} row must declare no per-row context: a handed root "
            "splits the reader from writers that resolve from HOME (t_be690204)")


def test_budget_row_gets_hermes_home_and_no_hscc_dir(tmp_path, monkeypatch):
    """Per-row context is registry-declared, not one-size-fits-all: the
    budget-hook row reads the DEPLOYED plugin tree through ``hermes_home`` and
    takes no ``hscc_dir`` at all. Mutation-proven the other way too — drop
    ``hermes_home=home`` from the render_rows call and this fails, which is the
    failure mode that matters (the row would fall back to the operator's live
    plugin tree from a sandboxed Stage-1 run)."""
    seen = {}

    def builder(injected=None, hermes_home=None):
        seen["hermes_home"] = hermes_home
        seen["injected"] = injected
        return doctor.Check("worker budget hook ([budget] note)", True,
                            detail="swapped budget row", fix="", fatal=False)

    monkeypatch.setattr(doctor, "_budget_hook_check_row", builder)
    seam = lambda: {"name": "worker budget hook ([budget] note)", "ok": True,
                    "fatal": False, "detail": "seam", "fix": ""}
    _doctor(tmp_path, monkeypatch, _budget_hook_check=seam)
    assert seen["injected"] is seam
    assert seen["hermes_home"] == str(tmp_path / ".hermes")
    assert "hscc_dir" not in seen, "the budget row must not receive hscc_dir"


def test_rows_receive_only_the_context_they_declare():
    """A row that declares only hscc_dir must never be handed hermes_home (its
    function would TypeError at bootstrap time, not at test time)."""
    calls = []

    class _Hooks:
        Check = doctor.Check

        def _row_taking_one_thing(self, injected=None, hscc_dir=None):
            calls.append(hscc_dir)
            return doctor.Check("x", True, detail=str(hscc_dir), fatal=False)

    spec = doctor_rows.FeatureCheckRow(
        key="one", seam_kwarg="_one_check", row_fn="_row_taking_one_thing")
    doctor_rows.render_rows(_Hooks(), [spec], {}, hscc_dir="/sandbox/.hscc",
                            hermes_home="/sandbox/.hermes")
    assert calls == ["/sandbox/.hscc"]


def test_main_only_row_declares_both_roots_and_says_why():
    """The exception that proves the t_be690204 rule, pinned as a registry fact.

    The daemon-state rows above must declare NO context because their writers
    resolve from HOME alone, so a root lifted from ``HSCC_DIR`` is a directory
    nothing writes to. This row's two roots each have a writer that honours them:
    ``hermes_home`` is the tree the installers write when bootstrap is pointed at
    a named ``--home``/``HERMES_HOME`` (the ``budget_hook`` precedent), and
    ``hscc_dir`` is ``hscc-roles/main_orch``'s OWN documented relocation seam for
    ``main-orchestrator.json`` — the document that decides which profile home is
    ``<main>``. Dropping either keyword does not merely change a signature: drop
    ``hermes_home`` and the row reads the operator's live ``~/.hermes`` from a
    sandboxed Stage-1 run; drop ``hscc_dir`` and a sandboxed bootstrap asks about
    the OPERATOR's identity instead of the one it is about to store.
    """
    by_key = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}
    spec = by_key["main_only_surface"]
    assert spec.row_kwargs == ("hermes_home", "hscc_dir")
    assert spec.row_fn == "", "the generic builder; no run_doctor() edit"
    assert spec.key not in ("orch_waker", "cron_watchers"), "sanity"
