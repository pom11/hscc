"""``onboard.py`` must obey the post-2.17.3 pause + intent-flag rules.

Every rule here was measured against source at the 2.17.3 merge base, and every
one of them was being broken by the shipped script. Nothing in this file touches
the fleet: hosts are TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space,
never this cluster), ``~/.hscc`` and ``~/.hermes`` are fabricated under
``tmp_path``, and the ``hscc`` door is a recorded argv, never executed.

THE RULES, and the source that states them

1. A node the pause overlay holds is never PICKED. ``node_pauses`` is durable
   overlay state whose whole purpose is that ``serving.json`` — which still
   lists the parked node's units — stays untouched, so a tool that reads only
   config.json + serving.json "repairs" a machine the operator parked on
   purpose. Skip seams ask ``covering``/``paused_nodes``, NEVER ``paused``
   (``node_pauses.py``, "WHAT A SKIP SEAM MUST ASK"): a tensor-parallel unit's
   ``span_nodes`` peers are just as parked.
2. Expiry does not release a node. ``paused()`` is clock-independent by design;
   only the daemon's ``expiry_pass``/``clear`` or ``hscc node resume`` end a
   pause. A seam that treated ``until < now`` as free relaunches the unit
   seconds before the daemon was about to.
3. verify reads a parked node as EXPECTED-ABSENT, not DOWN — the posture
   ``health._proxy_backend_drift_sets`` already takes (it drops the parked
   hosts from BOTH drift directions before deciding a proxy repair, because a
   pause that pages the operator about a proxy they caused is a 300 s cooldown
   on a live entrypoint, t_90958eb8).
4. The launch argv belongs to ``hscc_daemon.serving``, not to this script.
   sparkrun matches a workload by an INTENT HASH of recipe + --port /
   --served-model-name / --tp (t_8ad06a53: a stop that dropped a flag printed
   "No running workload matches intent" and stopped nothing; t_e1ff0e8e: a
   launch whose flags diverged made ``--ensure`` create a parallel duplicate).
   So ``phase_launch`` calls the ``hscc cluster down|up`` door and the module
   must contain no sparkrun argv at all — pinned structurally below.
5. A recorded ``serve_cmd`` must not survive a model swap unchanged. The
   node-resume path replays it VERBATIM (``_unit_run_cmd(prefer_serve_cmd=True)``)
   and it is the carrier of the intent flags, so a stale record is a stale
   intent.
"""

import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ``HSCC_ONBOARD_SRC`` redirects the loader at a COPY of the script. It exists
# for mutation probes — a guard that cannot be shown to bite is a decoration,
# and the probes must not edit the working tree to prove themselves.
ONBOARD_SRC = os.environ.get("HSCC_ONBOARD_SRC") or os.path.join(
    REPO, "install", "hscc-skills", "hscc-model-onboard", "scripts", "onboard.py")

GW = "192.0.2.10"      # TEST-NET-1: documentation space, never this fleet
W1 = "192.0.2.11"
W2 = "192.0.2.12"


def load_onboard(name="onboard_pause_under_test"):
    spec = importlib.util.spec_from_file_location(name, ONBOARD_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def onboard(monkeypatch, tmp_path):
    """A fresh module whose ``~/.hscc`` is a fabricated tmp tree.

    ``HSCC_HOME`` is patched to a directory under ``tmp_path`` AND ``HOME`` is
    moved there, so a read that forgets to honour the override lands on nothing
    rather than on the operator's real overlay. ``_seal_is_real`` below is the
    guard-assert that the seal is actually in force before any case writes.
    """
    home = tmp_path / "fake-home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HSCC_HOME", raising=False)
    monkeypatch.delenv("HSCC_HERMES_ROOT", raising=False)
    monkeypatch.delenv("HSCC_BIN", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    mod = load_onboard()
    mod.HSCC_HOME = str(hscc)
    # Guard-assert the seal BEFORE anything can write. ``startswith(tmp_path)``
    # is the whole proof and the only safe form: comparing against
    # ``expanduser("~/.hscc")`` here would resolve through the HOME we just
    # patched and so "prove" the seal by construction instead of checking it.
    assert str(mod.HSCC_HOME).startswith(str(tmp_path)), mod.HSCC_HOME
    return mod


def mk_config(onboard, workers=(W1, W2)):
    """Fabricate ``~/.hscc/config.json`` with a gateway + the named workers."""
    cfg = {"gateway_ip": GW, "workers": list(workers), "nas_ip": "192.0.2.20",
           "ssh_user": "spark", "vllm_port": 8000}
    with open(os.path.join(onboard.HSCC_HOME, "config.json"), "w") as f:
        json.dump(cfg, f)
    return cfg


def mk_overlay(onboard, pauses):
    """Write ``~/.hscc/node_pauses.json`` in the overlay's own shape."""
    data = {"pauses": pauses}
    path = os.path.join(onboard.HSCC_HOME, "node_pauses.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


# ── 1. the skip seam asks the overlay the documented way ──────────────────── #

def test_parked_nodes_includes_span_peers_not_just_the_keyed_host(onboard):
    """``span_nodes`` peers are parked too — the half a per-host test misses.

    A TP unit across two hosts stops as ONE thing and the pause record is keyed
    by the host the operator named. Reading only that key (what ``paused()``
    answers) leaves the peer looking free, and the whole reason
    ``node_pause_exec``/``health``/``autodown`` were each fixed to use
    ``covering``/``paused_nodes`` is that a repair path made exactly this
    mistake and relaunched the peer.
    """
    mk_overlay(onboard, {
        W1: {"reason": "training", "since": "", "until": "", "units": ["m"],
             "span_nodes": [W2], "stopped": ["m"]},
    })
    parked, readable = onboard.parked_nodes()
    assert readable is True
    assert parked == {W1, W2}, parked


def test_expired_pause_still_holds_the_node(onboard):
    """A passed ``until`` is a pause whose resume has not run yet, not freedom."""
    mk_overlay(onboard, {
        W1: {"reason": "training", "since": "2026-01-01T00:00:00+00:00",
             "until": "2026-01-02T00:00:00+00:00", "units": [], "span_nodes": [],
             "stopped": []},
    })
    parked, _ = onboard.parked_nodes()
    assert W1 in parked, "the seam read an expired record as free and would relaunch"


def test_unreadable_overlay_fails_open_but_says_so(onboard, capsys):
    """Corrupt overlay: nothing is treated as parked, and that is reported.

    Matching the CLI's documented fail-open (a corrupt overlay must not strand a
    cutover) while refusing the other half of the old silence — a skip nobody
    printed is indistinguishable from a node nobody remembered.
    """
    path = mk_overlay(onboard, {})
    with open(path, "w") as f:
        f.write("{ this is not json")
    parked, readable = onboard.parked_nodes()
    assert parked == set()
    assert readable is False
    mk_config(onboard)
    topo = onboard.load_topology()
    onboard.note_parked(topo)
    out = capsys.readouterr().out
    assert "unreadable" in out


def test_missing_overlay_means_nothing_parked_and_reads_clean(onboard):
    assert onboard.parked_nodes() == (set(), True)


def test_fallback_read_equals_the_shared_reader(onboard):
    """Drift pin: the standalone fallback returns what ``node_pauses`` returns.

    Same discipline as ``test_ladder_drift`` for the Hermes-home ladder — a
    fallback that quietly disagrees is the bug, not the fallback.
    """
    from hscc_daemon import node_pauses
    cases = [
        {},
        {W1: {"span_nodes": []}},
        {W1: {"span_nodes": [W2]}, W2: {"span_nodes": []}},
        {W1: "not a dict"},                      # unusable record drops the KEY too
        {W1: {"span_nodes": "not-a-list"}},      # garbage span degrades to empty
        {"": {"span_nodes": [W2]}},              # blank key drops the record
        {W1: {"span_nodes": ["", W2, 7, None]}},  # blank/non-string peers dropped
        {"pauses-instead": [W1]},                 # wrong top-level shape
    ]
    for pauses in cases:
        mk_overlay(onboard, pauses)
        assert onboard._fallback_parked_nodes(onboard.HSCC_HOME) == \
            set(node_pauses.paused_nodes(onboard.HSCC_HOME)), pauses


def test_overlay_read_is_sealed_to_hscc_home(onboard, monkeypatch):
    """The read must honour ``HSCC_HOME``, never the module-level ``~/.hscc``.

    ``node_pauses.HSCC_DIR`` is captured at import as ``~/.hscc``. A bare
    ``paused_nodes()`` would therefore read the OPERATOR'S overlay even from a
    sandboxed run — the same ambient-read class this suite exists to close.
    """
    from hscc_daemon import node_pauses
    mk_overlay(onboard, {W1: {"span_nodes": []}})
    # Point the shared module's own default at a DIFFERENT parked node. If the
    # script ever passes no directory, this is what it reads and the assert
    # below goes red for the right reason.
    decoy = os.path.join(onboard.HSCC_HOME, "decoy")
    os.makedirs(decoy)
    mk_pauses_at(decoy, {"198.51.100.7": {"span_nodes": []}})
    monkeypatch.setattr(node_pauses, "HSCC_DIR", decoy, raising=False)
    parked, _ = onboard.parked_nodes()
    assert parked == {W1}, parked
    assert "198.51.100.7" not in parked


def mk_pauses_at(directory, pauses):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "node_pauses.json"), "w") as f:
        json.dump({"pauses": pauses}, f)


# ── 2. a parked node is never picked by cache ─────────────────────────────── #

def test_cache_does_not_rsync_onto_a_parked_node(onboard, monkeypatch, capsys):
    """No multi-gigabyte push AND no probe at a machine the operator parked.

    The pause says nothing about WHY. Rsync here is additive and would usually
    succeed, which is exactly why the tool reached for it — and "usually
    succeeds" is not a licence to write to a node an operator took out of the
    fleet for a training run. The readiness probe SSHes too, so the skip has to
    come before it, not after; the ``probed`` assert is what pins that order,
    because a check placed after the probe passes an rsync-only test.
    """
    mk_config(onboard)
    mk_overlay(onboard, {W1: {"span_nodes": []}})
    monkeypatch.setattr(onboard, "nas_ref", lambda t, m: "abc123abc123")
    monkeypatch.setattr(onboard, "nas_snapshot_files", lambda t, m, r: 5)
    probed, touched = [], []
    monkeypatch.setattr(onboard, "node_cache_ready",
                        lambda t, h, m, r, n: (probed.append(h), False)[1])
    monkeypatch.setattr(onboard, "rsync_nas_to_node",
                        lambda t, h, m: (touched.append(h), (True, ""))[1])
    topo = onboard.load_topology()
    onboard.phase_cache(topo, "new/model-B", None)
    assert W1 not in touched, touched
    assert W1 not in probed, "parked node was SSH-probed before the skip fired"
    # The gateway is itself a serving node here (it hosts the orchestrator unit),
    # so it is probed and synced like any other unparked host.
    assert set(touched) == {GW, W2}, touched
    out = capsys.readouterr().out
    assert "PARKED" in out and W1 in out


# ── 3. verify reads a parked node as expected-absent ──────────────────────── #

def test_verify_reports_paused_not_down(onboard, monkeypatch, capsys):
    """DOWN for a node the operator parked is a false alarm on their own action.

    The proxy seam already drops parked hosts from both drift directions
    (``_proxy_backend_drift_sets``); verify disagreeing on the same tick is how
    an operator learns to ignore the report.
    """
    mk_config(onboard)
    mk_overlay(onboard, {W1: {"span_nodes": []}})
    monkeypatch.setattr(onboard, "_health", lambda h, p, timeout=5: False)
    monkeypatch.setattr(onboard, "_completion",
                        lambda h, p, m, timeout=40: "ERR refused")
    topo = onboard.load_topology()
    onboard.phase_verify(topo, "new/model-B", wait_secs=0)
    out = capsys.readouterr().out
    parked_lines = [ln for ln in out.splitlines() if W1 in ln]
    assert parked_lines and "PAUSED" in parked_lines[0], parked_lines
    assert not any("DOWN" in ln and W1 in ln for ln in out.splitlines())
    # The node nobody parked must still be allowed to fail loudly.
    assert any("DOWN" in ln and W2 in ln for ln in out.splitlines())


def test_all_parked_fleet_is_not_reported_as_a_pass(onboard, monkeypatch, capsys):
    mk_config(onboard, workers=())
    mk_overlay(onboard, {GW: {"span_nodes": []}})
    called = []
    monkeypatch.setattr(onboard, "_health",
                        lambda h, p, timeout=5: called.append(h) or True)
    topo = onboard.load_topology()
    onboard.phase_verify(topo, "new/model-B", wait_secs=0)
    out = capsys.readouterr().out
    assert called == [], called
    assert "not a pass" in out


# ── 4. launch is a door call, never a hand-built sparkrun argv ────────────── #

class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))

        class R:
            returncode = 0
        return R()


def mk_serving(onboard, recipe_path):
    units = [{"id": "orch", "role": "orchestrator", "nodes": [GW], "port": 8000,
              "recipe": recipe_path, "model": "old/model-A",
              "serve_cmd": ["sparkrun", "run", recipe_path, "--cluster", "hscc",
                            "--hosts", GW, "--port", "8000", "--no-follow",
                            "--ensure", "--served-model-name", "old/model-A "
                            "orchestrator-model", "--tp", "1"]}]
    with open(os.path.join(onboard.HSCC_HOME, "serving.json"), "w") as f:
        json.dump({"units": units}, f)
    return units


def test_launch_calls_the_door_and_never_names_sparkrun(onboard, monkeypatch,
                                                        capsys):
    rec = _Rec()
    monkeypatch.setattr(onboard.subprocess, "run", rec)
    monkeypatch.setenv("HSCC_BIN", "/fake/hscc")
    recipe = os.path.join(str(onboard.HSCC_HOME), "r.yaml")
    mk_config(onboard)
    mk_serving(onboard, recipe)
    topo = onboard.load_topology()

    assert onboard.phase_launch(topo, recipe, yes=True) is True
    verbs = [" ".join(a[1:]) for a in rec.calls]
    assert verbs == ["cluster down", "cluster up"], verbs
    assert not any("sparkrun" in " ".join(a) for a in rec.calls), rec.calls


def test_launch_without_yes_previews_and_stops_nothing(onboard, monkeypatch,
                                                       capsys):
    rec = _Rec()
    monkeypatch.setattr(onboard.subprocess, "run", rec)
    monkeypatch.setenv("HSCC_BIN", "/fake/hscc")
    recipe = os.path.join(str(onboard.HSCC_HOME), "r.yaml")
    mk_config(onboard)
    mk_serving(onboard, recipe)
    topo = onboard.load_topology()

    onboard.phase_launch(topo, recipe, yes=False)
    verbs = [" ".join(a[1:]) for a in rec.calls]
    assert verbs == ["cluster up --dry-run"], verbs
    assert "down" not in " ".join(verbs), "a preview must not stop the fleet"


def test_launch_refuses_when_the_door_is_unreachable(onboard, monkeypatch):
    """No fallback to a hand-rolled launch — that IS the bug being removed."""
    monkeypatch.delenv("HSCC_BIN", raising=False)
    monkeypatch.setattr(onboard.shutil, "which", lambda name: None)
    recipe = os.path.join(str(onboard.HSCC_HOME), "r.yaml")
    mk_config(onboard)
    mk_serving(onboard, recipe)
    topo = onboard.load_topology()
    with pytest.raises(SystemExit) as exc:
        onboard.phase_launch(topo, recipe, yes=True)
    assert exc.value.code == 1


def test_launch_refuses_a_recipe_the_fleet_does_not_declare(onboard, monkeypatch):
    """``launch`` uppers the DECLARED units; an undeclared recipe means wire
    never ran, and quietly upping the old declared recipe is the wrong answer."""
    rec = _Rec()
    monkeypatch.setattr(onboard.subprocess, "run", rec)
    monkeypatch.setenv("HSCC_BIN", "/fake/hscc")
    mk_config(onboard)
    mk_serving(onboard, "/somebody/else/recipe.yaml")
    topo = onboard.load_topology()
    with pytest.raises(SystemExit):
        onboard.phase_launch(topo, "/ours/recipe.yaml", yes=True)
    assert rec.calls == []


def test_module_contains_no_sparkrun_argv():
    """Structural guard: this script may not build a sparkrun command line.

    The flags that feed sparkrun's intent hash have one builder
    (``serving._unit_run_cmd``/``unit_stop_cmd``). Any ``sparkrun`` token in a
    list literal here is a second builder allowed to disagree with it, which is
    the t_8ad06a53 / t_e1ff0e8e class. Prose in docstrings is allowed; argv is
    not. Proven to bite by the same AST shape the other guards in this repo use.
    """
    import ast
    tree = ast.parse(open(ONBOARD_SRC).read())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                        and elt.value.strip().startswith("sparkrun"):
                    offenders.append((getattr(node, "lineno", 0), elt.value))
    assert not offenders, (
        f"onboard.py builds a sparkrun argv at line(s) "
        f"{sorted({ln for ln, _ in offenders})} — the launch/stop argv belongs "
        f"to hscc_daemon.serving")


def test_launch_phase_does_not_reimplement_pause_skipping():
    """The door owns the pause partition; a second partition would drift.

    ``cmd_cluster_up`` asks the overlay once, for every unit including span
    peers, and reports the skips under ``paused``/``all-paused``. A second
    answer here is a second seam allowed to disagree with it — and the one that
    wins would be whichever ran last. So the pause lives in the door and this
    function must not name the seam at all.

    Identifier scan, not substring: the function's own prose explains why it
    must not call the seam, and a guard that reds on the word is a guard nobody
    can keep.
    """
    import ast
    tree = ast.parse(open(ONBOARD_SRC).read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "phase_launch")
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)          # string CONSTANTS, not the docstring
    offenders = {n for n in names if n in {"parked_nodes", "node_pauses",
                                           "paused_nodes", "covering"}}
    assert not offenders, (
        f"phase_launch re-derives the parked set via {sorted(offenders)} — "
        f"cmd_cluster_up already partitions parked nodes, and two seams are "
        f"free to disagree")


# ── 5. a model swap must not leave serve_cmd describing the old launch ────── #

def test_wire_serving_refreshes_the_recorded_serve_cmd(onboard, capsys):
    """``serve_cmd`` is the argv node-resume replays verbatim, and it carries
    ``--served-model-name``/``--tp`` — the intent flags. After a model swap the
    old record is a stale intent, and sparkrun answers a stale intent by
    matching nothing while the caller logs a clean stop."""
    old = "/old/recipe.yaml"
    new = os.path.join(str(onboard.HSCC_HOME), "new.yaml")
    units = mk_serving(onboard, old)
    with open(new, "w") as f:
        f.write("model: new/model-B\n")
    onboard.wire_serving("new/model-B", new)
    got = json.load(open(os.path.join(onboard.HSCC_HOME, "serving.json")))
    u = got["units"][0]
    assert u["model"] == "new/model-B" and u["recipe"] == new
    assert u["serve_cmd"], "the record was dropped without being re-rendered"
    assert "new/model-B" in " ".join(u["serve_cmd"]), u["serve_cmd"]
    assert "--served-model-name" in u["serve_cmd"] and "--tp" in u["serve_cmd"]
    assert old not in " ".join(u["serve_cmd"]), u["serve_cmd"]
    # Placement the verb does not own stays byte-identical.
    assert u["nodes"] == units[0]["nodes"] and "tp" not in u
    assert u["port"] == 8000


def test_wire_serving_refresh_derives_from_fields_not_the_record(onboard):
    """The record must not be able to author its own replacement.

    ``fleet_up_plan`` derives every argv from the unit's FIELDS; replaying the
    record is reserved for the node-resume path (``prefer_serve_cmd=True``,
    t_09708c17) precisely because a resume must reproduce the exact argv a unit
    was started with. ``wire`` is not that path: it has just rewritten the
    fields, so the only honest argv is the derived one. Here the stale record's
    PLACEMENT already matches (same recipe/hosts/port) and its ``--tp`` says 2
    while the unit's field says 1 — so this test goes red the moment anyone
    routes the refresh through the record instead of the fields. ``wire`` keeps
    writing the derived ``--tp 1``, which is also what the next resume's
    placement check will accept.
    """
    recipe = os.path.join(str(onboard.HSCC_HOME), "same.yaml")
    with open(recipe, "w") as f:
        f.write("model: new/model-B\n")
    # Placement (recipe/hosts/port) ALREADY matches the unit; the intent flags do
    # not. This is the shape a stale record actually has after a re-quant.
    stale = ["sparkrun", "run", recipe, "--cluster", "hscc", "--hosts", GW,
             "--port", "8000", "--no-follow", "--ensure",
             "--served-model-name", "old/model-A orchestrator-model", "--tp", "2"]
    d = {"units": [{"id": "orch", "role": "orchestrator", "nodes": [GW],
                    "port": 8000, "recipe": recipe, "model": "old/model-A",
                    "tp": 1, "serve_cmd": stale}]}
    with open(os.path.join(onboard.HSCC_HOME, "serving.json"), "w") as f:
        json.dump(d, f)

    onboard.wire_serving("new/model-B", recipe)
    u = json.load(open(os.path.join(onboard.HSCC_HOME, "serving.json")))["units"][0]
    got = " ".join(u["serve_cmd"])
    assert "old/model-A" not in got, u["serve_cmd"]
    assert "--served-model-name new/model-B orchestrator-model" in got, u["serve_cmd"]
    # The flag the placement check does NOT cover: without stripping the record
    # this stays "--tp 2" from the stale argv while the unit says tp: 1.
    assert "--tp 1" in got, f"replayed the stale record instead of deriving: {got}"


def test_wire_serving_clears_a_record_it_cannot_re_render(onboard, monkeypatch,
                                                          capsys):
    """Fail honest, never fail into a stale record.

    Without ``hscc_daemon.serving`` the sanctioned argv cannot be derived, and
    writing one by hand is the bug. The record is removed — which sends
    ``_unit_run_cmd`` down its derived path — and the reason plus the repair
    ('hscc cluster template apply') is printed.
    """
    monkeypatch.setattr(onboard, "_load_serving_module", lambda: None)
    old = os.path.join(str(onboard.HSCC_HOME), "r.yaml")
    mk_serving(onboard, old)
    onboard.wire_serving("new/model-B", old)
    got = json.load(open(os.path.join(onboard.HSCC_HOME, "serving.json")))
    assert "serve_cmd" not in got["units"][0], got["units"][0]
    assert "CLEARED" in capsys.readouterr().out


def test_wire_serving_survives_a_raising_plan_builder(onboard, monkeypatch):
    """A raising shared builder degrades to "cleared", not to a traceback."""
    class Boom:
        @staticmethod
        def fleet_up_plan(_d):
            raise RuntimeError("sparkrun topology unavailable")
    monkeypatch.setattr(onboard, "_load_serving_module", lambda: Boom)
    old = os.path.join(str(onboard.HSCC_HOME), "r.yaml")
    mk_serving(onboard, old)
    onboard.wire_serving("new/model-B", old)
    got = json.load(open(os.path.join(onboard.HSCC_HOME, "serving.json")))
    assert "serve_cmd" not in got["units"][0]
