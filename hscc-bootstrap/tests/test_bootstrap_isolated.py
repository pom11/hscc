"""Run the REAL installer into a throwaway home and assert it stays there.

Why this exists: three releases in a row shipped bugs that only a real run
could find — `bootstrap.sh` did not parse at all (2.5.0-2.5.2), and several
helpers hardcoded `~/.hermes` / `~/.hscc`, so an install aimed at another home
silently read and wrote the OPERATOR's live config, triggers and autodown
state. The Python suite was green throughout, because nothing executed the
installer.

Isolation is HERMES_HOME / HSCC_DIR / HERMES_SCRIPTS pointed into tmp.

HERMES_BIN IS sealed, at a stub that refuses; so is `sparkrun`, at a stub that answers
with a fabricated cluster; and so is the project registry, at a fabricated
`registry.yaml` (`HSCC_REGISTRY`). All three are on the child's lookup path for the same
reason: the run must not inherit the operator's machine.

`HSCC_REGISTRY` is the newest of the three (t_5b560adf, third layer). `orchestrators.py`
resolves the project list from `$HSCC_REGISTRY` or `~/.flightdeck/registry.yaml`, and
`bootstrap.sh`'s `Install: per-project orchestrators` stage provisions one orchestrator
per registry project — so a fresh isolated install used to create `*-orch` profiles for
whatever the OPERATOR had registered. Sealed, the list is empty and the orchestrator
review case had nothing to review. That module's own docstring already documents the
shape (t_4b70e8d4: a resolver test that "passed" on the operator's host by reading the
live registry and went red on CI); the seal-list test below now asks the module, in a
child with the installer's env, which projects it believes exist.

`sparkrun` is the second of the three (t_5b560adf). `doctor._sparkrun_cluster_ok`
is FATAL and the installer's preflight runs before any stage, so the isolated run used
to succeed only because the child inherited `PATH` and found the *operator's* real
sparkrun with a cluster configured (`~/.config/sparkrun`). That is a home path: the
roster-wide HOME seal (PR #606) removes it, and the four `@needs_runtime` cases here
went red on `✗ FAILED: sparkrun cluster` the moment the leg stopped skipping them. The
stub answers `cluster list` with TEST-NET placeholder hosts (the same shape
`test_bootstrap_dry_run.py::_make_home` already uses), so preflight sees a configured
cluster on every host and `--skip-node-recovery` no longer has to be the only thing
standing between a pytest run and the live fleet.

`HERMES_BIN` is the older one, and why that seal is load-bearing for
``test_fresh_home_says_not_wired_instead_of_claiming_success`` is worth keeping in
mind, because it is not obvious: `Install: main orchestrator board` shells out to the
real Hermes CLI (`main_board.find_hermes_bin` → `hermes kanban boards create`), and
that CLI initialises HERMES_HOME on first run: measured on this host, the stage alone
wrote `<sandbox>/.hermes/config.yaml` (109 bytes: `_config_version` + the browser
block) plus `kanban/`, SOUL.md and the cache dirs. Six stages later the wiring stage
would then find a config.yaml that exists, wire it and print "config wired (...)" —
true of that home, but not the fresh, never-installed home this test is named for. So
the CLI is sealed off for the isolated run and the home stays config-less until the
wiring stage looks at it. What the stub gives up is nothing this file asserts: the
board stage's own contract — create-then-switch, idempotence, the HERMES_BIN/venv
lookup order, and a real fake-CLI subprocess run — is pinned in test_main_board.py.

Skipped steps and why:
  --skip-cli             installs a console script INTO the Hermes venv (shared, real)
  --skip-patches         patches the hermes-agent git checkout (shared, real)
  --skip-daemon          installs launchd/systemd units and restarts the live daemon
  --skip-node-recovery   SSHes the LIVE fleet to install panic-to-reboot sysctls
                         (t_1fc3fe83: a pytest run must never arm reboot-on-panic on
                         the actual nodes) and to arm the systemd hardware watchdog
                         (t_ebbe38d1: same reason — a sandboxed test must never
                         escalate to root over ssh on the operator's nodes). Since
                         t_5b560adf the child's `sparkrun` is a sandbox stub, so this
                         flag is defence in depth rather than the only seal — the
                         stage no longer has the real cluster to ssh to.
  --skip-orchpad-package pip-installs orchpad INTO the Hermes venv (shared, real).
                         The sandbox's `hermes-agent` is a symlink to the live
                         tree, so without the flag the stage installed into the
                         LIVE venv with console-script shebangs pointing into this
                         test's tmp dir; once pytest deleted it, the live
                         `bin/orchpad` + `orchpad-hermes` were dead and the 2.17.8
                         bootstrap's orchpad stage failed on every profile. The
                         plugin step still runs (from an orchpad already in the
                         venv) and only writes into the sandbox profiles; the test
                         below asserts the live venv's orchpad files are untouched.
Those five are the only steps that intentionally write outside the home.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_tree

REPO = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO / "hscc-bootstrap" / "bootstrap.sh"

#: What the sealed `sparkrun` stub reports for `cluster list`: a cluster that exists
#: only inside this sandbox. TEST-NET addresses (RFC 5737), never a real host — the
#: same shape `test_bootstrap_dry_run.py::FAKE_CLUSTER` uses, so the two files cannot
#: drift into one pinning a routable host and the other a placeholder.
FAKE_CLUSTER = json.dumps([
    {"name": "fabricated", "hosts": ["192.0.2.1"], "user": "op",
     "cache_dir": "", "default": True}])

#: What this file needs from the machine, in one place, so the collection-time
#: gate and the call-time resolvers below state the SAME thing (hermes_tree).
CAPABILITY = ("test_bootstrap_isolated — running the isolated installer with the "
              "real Hermes venv interpreter")
needs_runtime = pytest.mark.skipif(
    not hermes_tree.runtime_available(),
    reason=hermes_tree.runtime_skip_reason(CAPABILITY))


def _md5(p: Path):
    return hashlib.md5(p.read_bytes()).hexdigest() if p.is_file() else None


def _sealed_hermes_bin(home: Path) -> Path:
    """An executable Hermes-CLI stand-in that refuses, inside the sandbox.

    `main_board.find_hermes_bin` honours ``HERMES_BIN`` ahead of the venv, so
    pointing it here keeps the real Hermes CLI out of the isolated run. Why that
    is load-bearing for ``test_fresh_home_says_not_wired_instead_of_claiming_success``:
    see the module docstring. It refuses (rc 1 + a stderr naming the seal) rather
    than reporting a board it never created, so the stage warns visibly instead
    of claiming success, and any *new* stage that grew a dependency on the real
    CLI would surface as a warning too, not as a silent fake.
    """
    stub = home / "bin" / "hermes"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/sh\n"
        "# Sealed for the hscc-bootstrap isolated-install tests (t_ea59a6ba):\n"
        "# the real Hermes CLI would initialise HERMES_HOME (and config.yaml)\n"
        "# here, which is exactly the state the fresh-home test must not have.\n"
        "echo 'hermes: sealed by the isolated-install test (no real CLI ran)' >&2\n"
        "exit 1\n",
        encoding="utf-8")
    stub.chmod(0o755)
    return stub


#: A registry of projects that exist only inside this sandbox. `orchestrators.py`
#: resolves the project list from `$HSCC_REGISTRY` or `~/.flightdeck/registry.yaml`
#: (a HOME path), so `bootstrap.sh`'s `Install: per-project orchestrators` stage used to
#: provision whatever the OPERATOR has registered — which is why
#: `test_fresh_bootstrap_leaves_every_orchestrator_reviewable` was green here and red
#: everywhere the home was sealed (t_5b560adf measured exactly that: `bootstrap
#: provisioned no *-orch profiles — nothing to check`). Same failure shape, same module,
#: as its own docstring's t_4b70e8d4 note: a test "passing" on the operator's host by
#: reading the live registry. Two fabricated projects, so the fan-out is a fan-out.
FAKE_REGISTRY = """\
projects:
  - name: sealedalpha
    board: sealedalpha
  - name: sealbett
    board: sealbett
"""


def _make_sandbox(tmp_path, *, need_tree: bool) -> dict:
    """A throwaway HERMES_HOME/HSCC_DIR, with the real venv reachable on demand."""
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hscc = home / ".hscc"
    for d in (hermes, hscc):
        d.mkdir(parents=True)
    if need_tree:
        # Symlink the DIRECTORY, not the python binary: symlinking the binary
        # relocates sys.prefix and the venv's packages (rich, yaml) disappear.
        (hermes / "hermes-agent").symlink_to(hermes_tree.agent_dir_or_skip(CAPABILITY))
    registry = home / "flightdeck" / "registry.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(FAKE_REGISTRY, encoding="utf-8")
    return {"home": home, "hermes": hermes, "hscc": hscc,
            "hermes_bin": _sealed_hermes_bin(home),
            "sparkrun_bin": _sealed_sparkrun_bin(home),
            "registry": registry}


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway HERMES_HOME/HSCC_DIR with the real venv reachable."""
    return _make_sandbox(tmp_path, need_tree=True)


@pytest.fixture
def sandbox_seal_view(tmp_path):
    """The same sandbox WITHOUT the venv symlink.

    For ``test_the_sandbox_seals_every_input_the_stages_shell_out_to``, which asserts the
    child's ENV and the two stubs and never executes the venv. Before t_5b560adf it asked
    for the tree it did not need and got a *dangling* symlink under a sealed home
    (``symlink_to`` does not check its target), so it reported a pass against a venv that
    was not there. Refusing to run without a tree would be honest but would retire a case
    that needs nothing; taking the tree out of its fixture keeps it running on every host.
    """
    return _make_sandbox(tmp_path, need_tree=False)


def _sealed_sparkrun_bin(home: Path) -> Path:
    """An executable `sparkrun` stand-in that answers with a fabricated cluster.

    `_env` puts this directory FIRST on the child's PATH, so the installer's
    preflight consults it instead of the operator's sparkrun. Why that is not
    optional any more: `_sparkrun_cluster_ok` is FATAL, and a cluster lives under
    `~/.config/sparkrun` — a home path — so an isolated run that inherited PATH was
    only green on a machine that happened to have a cluster configured. The roster-wide
    HOME seal (PR #606) makes every leg such a machine, and these four cases went red on
    `✗ FAILED: sparkrun cluster`. The stub keeps the check *running* (it is a real
    check, and a real install with no cluster must still fail it) while removing the
    dependency on the operator's config. It answers only `cluster list`, like the
    dry-run stub it is copied from; every other sparkrun call exits 0 with no output,
    which doctor reads as "nothing to compare" and reports, not as a failure.
    """
    stub = home / "bin" / "sparkrun"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/sh\n"
        "# Sealed for the hscc-bootstrap isolated-install tests (t_5b560adf):\n"
        "# the real sparkrun resolves the OPERATOR's cluster config under the home,\n"
        "# so preflight would pass or fail on the machine rather than on the code.\n"
        'if [ "$1" = cluster ] && [ "$2" = list ]; then\n'
        f"  printf '%s' '{FAKE_CLUSTER}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _env(sandbox):
    """The installer child's environment — every home-ish input pointed at the
    sandbox, and the real Hermes CLI and sparkrun both sealed (module docstring).

    Split out of ``_run`` so ``test_the_sandbox_seals_every_input_the_stages_shell_out_to``
    can assert the seal list without paying for an installer run.
    """
    env = dict(os.environ)
    env.update({
        # NOTE: HOME is intentionally left alone — see the module docstring.
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["hermes"] / "scripts"),
        "HERMES_BIN": str(sandbox["hermes_bin"]),
        # The project registry: `orchestrators._registry_path` reads this env and
        # otherwise `~/.flightdeck/registry.yaml`. Left unsealed, the
        # `Install: per-project orchestrators` stage provisions whatever the operator
        # has registered — a home path again, and the reason the orchestrator-review
        # case was green on this host and red under a seal (t_5b560adf).
        "HSCC_REGISTRY": str(sandbox["registry"]),
        # Sandbox bin FIRST: `sparkrun` resolves to the sealed stub, everything else
        # (bash, git, uv, brew, tmux…) still resolves as it always has. Prepending
        # rather than narrowing keeps the stages that legitimately need system tools
        # working — the dry-run file narrows PATH because it fabricates a whole tree,
        # this one runs the repo's real installer.
        "PATH": f'{sandbox["home"] / "bin"}{os.pathsep}{env.get("PATH", "")}',
    })
    return env


#: Every step that writes outside the sandbox home (module docstring).
SKIP_ARGS = ("--skip-cli", "--skip-daemon", "--skip-patches",
             "--skip-node-recovery", "--skip-orchpad-package")


def _run(sandbox, extra=()):
    return subprocess.run(
        ["bash", str(BOOTSTRAP), "--yes", *SKIP_ARGS, *extra],
        capture_output=True, text=True, env=_env(sandbox), timeout=900)


def _live_orchpad_files(sandbox) -> dict:
    """md5 of every orchpad file in the REAL venv the sandbox symlinks to.

    Console scripts (``bin/orchpad*``) carry the shebang that went stale, and
    the dist-info ``RECORD`` changes on any reinstall, so together they show
    whether the run wrote into the shared venv.
    """
    venv = (sandbox["hermes"] / "hermes-agent" / "venv").resolve()
    found = {}
    for path in sorted((venv / "bin").glob("orchpad*")):
        found[str(path)] = _md5(path)
    for path in sorted(venv.glob("lib/python*/site-packages/orchpad-*.dist-info/RECORD")):
        found[str(path)] = _md5(path)
    return found


def test_every_shared_write_step_is_skipped():
    """The orchpad package step joined the list after it leaked into the live venv."""
    assert "--skip-orchpad-package" in SKIP_ARGS
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for flag in SKIP_ARGS:
        assert f"{flag})" in text, f"bootstrap.sh does not parse {flag}"


@needs_runtime
def test_installer_completes_and_is_self_contained(sandbox):
    live_cfg = Path.home() / ".hermes" / "config.yaml"
    live_trig = Path.home() / ".hscc" / "triggers.json"
    before = (_md5(live_cfg), _md5(live_trig))
    live_orchpad = _live_orchpad_files(sandbox)

    r = _run(sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"installer failed:\n{out[-3000:]}"
    assert "Done" in out, f"installer did not reach its summary:\n{out[-2000:]}"

    # Payload landed in the sandbox.
    for rel in ("plugins", "profiles", "skills", "scripts", "SOUL.md"):
        assert (sandbox["hermes"] / rel).exists(), f"missing {rel} in HERMES_HOME"
    for rel in ("serving.json", "triggers.json", "autodown.json", "autonomy"):
        assert (sandbox["hscc"] / rel).exists(), f"missing {rel} in HSCC_DIR"

    # t_b312580e: a FRESH install (no target_profiles.json) must reach the main
    # orchestrator's profile home, which is created after the first payload
    # pass. QA F1 measured <main> with zero hscc plugins before this fix.
    main = json.loads(
        (sandbox["hscc"] / "main-orchestrator.json").read_text())["name"]
    main_plugins = sandbox["hermes"] / "profiles" / main / "plugins"
    for plugin in ("hscc-cluster", "hscc-commands", "sparkrun-hermes"):
        assert (main_plugins / plugin / "__init__.py").is_file(), \
            f"fresh install left <main> without {plugin}"

    # And nothing reached the operator's live state.
    assert (_md5(live_cfg), _md5(live_trig)) == before, \
        "the isolated install modified LIVE state"
    assert _live_orchpad_files(sandbox) == live_orchpad, \
        "the isolated install wrote orchpad into the LIVE Hermes venv"


@needs_runtime
def test_fresh_home_says_not_wired_instead_of_claiming_success(sandbox):
    """A fresh home has no config.yaml, so wiring is a deliberate no-op.

    It must SAY so. Reporting "config wired" for a machine where nothing was
    wired is the silent gap this step exists to close.
    """
    out = _run(sandbox).stdout
    assert "NOT WIRED" in out, f"fresh install did not flag unwired config:\n{out[-2000:]}"
    assert "config wired (" not in out, "claimed success with no config.yaml"


@needs_runtime
def test_status_lines_are_not_polluted_by_rich_panels(sandbox):
    """Helpers print a PLAIN line when captured.

    bootstrap.sh embeds a helper's stdout into its own status line, so a Rich
    panel there arrived as box-drawing characters pasted mid-sentence.
    """
    out = _run(sandbox).stdout
    for line in out.splitlines():
        if line.lstrip().startswith(("✓", "⚠")) and "─" in line:
            pytest.fail(f"status line contains panel art: {line[:160]}")


@needs_runtime
def test_rerun_is_idempotent(sandbox):
    """Installing twice must not fail or double-apply triggers."""
    first = _run(sandbox)
    assert first.returncode == 0
    second = _run(sandbox)
    assert second.returncode == 0, f"second run failed:\n{(second.stdout + second.stderr)[-2000:]}"
    assert "added 0" in second.stdout, \
        "second run re-added trigger rules (not idempotent)"


@needs_runtime
def test_fresh_bootstrap_leaves_every_orchestrator_reviewable(sandbox):
    """B1 (round-2 review): the guarded review-skill fan-out must run AFTER
    role/orchestrator provisioning, so a SINGLE fresh bootstrap leaves every
    `*-orch` it just created able to resolve the dispatcher-injected
    `sdlc-review` exactly once.

    The first cut ordered the stage before provisioning: the fan-out saw zero
    profiles, installed only the root home, and the orchestrators created
    afterwards had EMPTY skills trees — a review-lane spawn to one died with
    the card's error string while doctor read GREEN, and nothing forced the
    second bootstrap that would have fixed it. This drives the REAL installer
    (shared helper with the e2e test above, so the suite pays for one run) and
    checks the end state per created profile via the resolver mirror — the
    same rule cross-validated against the live resolver in
    test_skill_resolver_xval.py."""
    r = _run(sandbox)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    _assert_all_orch_profiles_reviewable(sandbox)


def _assert_all_orch_profiles_reviewable(sandbox):
    profiles = sandbox["hermes"] / "profiles"
    orchs = [p for p in profiles.iterdir()
             if p.is_dir() and not p.name.startswith(".")
             and p.name.endswith("-orch")]
    assert orchs, "bootstrap provisioned no *-orch profiles — nothing to check"
    import importlib
    import sys as _sys
    sys_path_hack = str(REPO / "hscc-bootstrap")
    if sys_path_hack not in _sys.path:
        _sys.path.insert(0, sys_path_hack)
    skill_guard = importlib.import_module("skill_guard")
    broken = []
    for p in orchs:
        res = skill_guard.resolution(
            skill_guard.profile_skill_roots(p), skill_guard.REVIEW_SKILL)
        if res["state"] != "ok":
            broken.append(f"{p.name}: {res['state']}")
    assert not broken, f"profiles created by THIS run cannot review: {broken}"


def test_the_sandbox_seals_every_input_the_stages_shell_out_to(sandbox_seal_view):
    """The isolated run's child env must not reach the operator's install.

    Guards the premise of every `@needs_runtime` case here: a stage that shells
    out to a home-resolving tool is only contained if the env points that tool
    at the sandbox. `HERMES_BIN` is the one this file lost a green leg over
    (t_ea59a6ba) — with it unsealed, `Install: main orchestrator board` ran the
    real Hermes CLI, which wrote the sandbox's `config.yaml` on first-run init
    and the wiring stage then legitimately reported "config wired" on a run the
    fresh-home test calls fresh.

    HOME is asserted UNSEALED on purpose (module docstring): pinning it here
    would be a lie about the one input the run deliberately shares.

    PATH is asserted with the sandbox bin FIRST (t_5b560adf): `sparkrun` was the
    second input this file lost a green leg over — its cluster config is a home path,
    so with PATH inherited the preflight's FATAL `sparkrun cluster` row passed or
    failed on the operator's machine. The stub answers with a fabricated cluster, so
    the check runs everywhere and passes nowhere by accident.
    """
    # The no-tree sandbox: this case asserts the child env and the two stubs, and never
    # executes the venv (fixture docstring), so it must not retire when none is declared.
    sandbox = sandbox_seal_view
    env = _env(sandbox)
    for key in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS", "HERMES_BIN",
                "HSCC_REGISTRY"):
        assert key in env, f"{key} is not sealed — the installer could reach live state"
        assert str(env[key]).startswith(str(sandbox["home"])), \
            f"{key}={env[key]!r} points outside the sandbox {sandbox['home']!r}"
    # The registry seal must be honoured by the RESOLVER, not just exported. This is
    # the exact bug `orchestrators._registry_path`'s docstring records (t_4b70e8d4):
    # a test that "passes" on the operator's host because the resolver fell back to
    # the live ~/.flightdeck/registry.yaml. Ask the module itself, in a child with the
    # installer's env, which projects it thinks exist.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import orchestrators;"
         "print(orchestrators.list_registry_projects())",
         str(REPO / "hscc-roles")],
        capture_output=True, text=True, timeout=120, env=env)
    assert probe.returncode == 0, probe.stderr[-400:]
    assert probe.stdout.strip() == "['sealedalpha', 'sealbett']", \
        "the resolver did not answer from the sealed registry — it read the machine: " \
        f"{probe.stdout.strip()[:200]!r}"
    stub = Path(env["HERMES_BIN"])
    assert stub.is_file() and stub.stat().st_mode & 0o111, \
        "HERMES_BIN must be an executable stub, else find_hermes_bin falls back "
    # The seal refuses; it never impersonates a board that was not created.
    r = subprocess.run([str(stub), "kanban", "boards", "create", "general"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "the sealed Hermes CLI claimed success it did not earn"
    assert "sealed" in (r.stderr or ""), r.stderr
    # sparkrun: first on PATH, inside the sandbox, and it is the stub that answers.
    first = Path(str(env["PATH"]).split(os.pathsep)[0])
    assert first == sandbox["home"] / "bin", \
        f"PATH does not lead with the sandbox bin: {first}"
    spark = first / "sparkrun"
    assert spark.is_file() and spark.stat().st_mode & 0o111, \
        "no executable sparkrun stub — preflight would resolve the operator's cluster"
    r = subprocess.run([str(spark), "cluster", "list"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"the sealed sparkrun failed cluster list: {r.stderr[:200]!r}"
    assert json.loads(r.stdout) == json.loads(FAKE_CLUSTER), \
        "the sealed sparkrun must answer with EXACTLY the fabricated cluster, no " \
        f"real host in it: {r.stdout[:200]!r}"
    # And the stub is what the child's shell finds, i.e. the real one is shadowed.
    which = subprocess.run(["sh", "-c", "command -v sparkrun"],
                           capture_output=True, text=True, timeout=60, env=env)
    assert which.stdout.strip() == str(spark), \
        f"the child would resolve sparkrun outside the sandbox: {which.stdout.strip()!r}"
    assert env.get("HOME") == os.environ.get("HOME"), \
        "HOME must stay exactly as inherited — sealing it is the deliberate choice " \
        "this file's docstring argues against"


def test_helpers_do_not_hardcode_the_home_paths():
    """Static guard, runs without the runtime.

    Every bootstrap helper must resolve HERMES_HOME / HSCC_DIR from the
    environment rather than baking `~/.hermes` or `~/.hscc`, or an install into
    another home reaches into the operator's live state. Checked statically so
    a regression is caught even where the real installer cannot run.
    """
    offenders = []
    for py in sorted((REPO / "hscc-bootstrap").glob("*.py")):
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if "expanduser(" not in line or line.lstrip().startswith("#"):
                continue
            if '"~/.hermes' not in line and '"~/.hscc' not in line:
                continue
            # Acceptable: used only as the FALLBACK for an env lookup.
            if ("environ.get" in line or "or os.path.expanduser" in line
                    or "hermes_home or" in line):
                continue
            offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert not offenders, (
        "bootstrap helpers hardcode a home path instead of reading the env:\n  "
        + "\n  ".join(offenders))
