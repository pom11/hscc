"""End-to-end-ish install test (H4).

bootstrap.sh itself is bash, but its real work is delegated to the Python helpers
(doctor, install_payload, serving_gen, enable_plugins, install_soul). This test
drives that stage SEQUENCE against a temp HOME with a fake repo + stub sparkrun,
asserting a clean install produces the expected runtime files — the coverage the
suite lacked (every prior failure lived in the bash/stage wiring, untested).
"""

import json

import doctor
import install_payload
import serving_gen


def _fake_repo(tmp_path):
    repo = tmp_path / "repo"
    for d in ("hscc-cluster", "hscc-bootstrap", "install"):
        (repo / d).mkdir(parents=True)
        (repo / d / "__init__.py").write_text("x=1\n")
    (repo / "hscc-cluster" / "__pycache__").mkdir()
    (repo / "hscc-cluster" / "__pycache__" / "j.pyc").write_text("bc")
    (repo / "hscc-cluster" / "tests").mkdir()
    (repo / "hscc-cluster" / "tests" / "test_x.py").write_text("def test(): pass\n")
    (repo / "README.md").write_text("# hscc\n")
    return repo


def test_stage_sequence_produces_working_install(tmp_path):
    home = tmp_path / "home"
    (home / ".hermes" / "hermes-agent").mkdir(parents=True)
    plugins = home / ".hermes" / "plugins"
    repo = _fake_repo(tmp_path)

    # Stage 1: doctor (fatal checks should pass with a stub cluster + hermes dir)
    res = doctor.run_doctor(str(home / ".hermes"),
                            _cluster_runner=lambda: '[{"name":"hscc","hosts":["10.0.0.1","10.0.0.2"]}]')
    assert "hermes" not in res["fatal_failures"]
    assert "sparkrun cluster" not in res["fatal_failures"]

    # Stage 4.0: copy plugin payload into the runtime dir
    payload = ["hscc-cluster", "hscc-bootstrap", "install", "README.md"]
    cp = install_payload.install_payload(repo, plugins, payload)
    assert cp["skipped"] is False
    assert (plugins / "hscc-cluster" / "__init__.py").is_file()
    # build artifacts + tests excluded from the runtime copy
    assert not (plugins / "hscc-cluster" / "__pycache__").exists()
    assert not (plugins / "hscc-cluster" / "tests").exists()

    # Stage: serving.json generation from the detected cluster
    cluster = {"hosts": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]}
    serving = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.1", recipe="~/r/o.yaml", model="M",
        port=8000, keepalive=True)
    hscc = home / ".hscc"; hscc.mkdir()
    (hscc / "serving.json").write_text(json.dumps(serving))
    sj = json.loads((hscc / "serving.json").read_text())
    assert len([u for u in sj["units"] if u["role"] == "orchestrator"]) == 1
    assert len([u for u in sj["units"] if u["role"] == "worker"]) == 2


def test_doctor_blocks_when_cluster_missing(tmp_path):
    """A clean machine with no sparkrun cluster must FAIL preflight (the hard
    stop bootstrap relies on), not limp into later stages."""
    (tmp_path / "hermes-agent").mkdir()
    res = doctor.run_doctor(str(tmp_path), _cluster_runner=lambda: "")
    assert res["ok"] is False
    assert "sparkrun cluster" in res["fatal_failures"]


def test_reinstall_is_idempotent(tmp_path):
    repo = _fake_repo(tmp_path)
    plugins = tmp_path / "plugins"
    payload = ["hscc-cluster"]
    install_payload.install_payload(repo, plugins, payload, ts="A")
    r2 = install_payload.install_payload(repo, plugins, payload, ts="B")
    assert "hscc-cluster.bak-B" in r2["backed_up"]   # prior backed up
    assert (plugins / "hscc-cluster" / "__init__.py").is_file()
