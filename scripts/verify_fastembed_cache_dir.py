#!/usr/bin/env python3
"""Prove that the fastembed cache knob EXISTS and is HONOURED (t_317a0f30).

A reviewer could not reach the writer from ``~/.hermes/plugins`` — correctly: the
writer is not there. It is ``memori_python.abi3.so``, the Rust ``fastembed`` crate
vendored into the memori SDK inside the **hermes venv's site-packages**::

    ~/.hermes/hermes-agent/venv/lib/python3.*/site-packages/memori_python.abi3.so

Reproduce the scan that finds it (both literals, the relative default and the
override the crate reads)::

    strings -a <that .so> | grep -o '\\.fastembed_cache\\|FASTEMBED_CACHE_DIR\\|HF_HOME' | sort -u

Because the writer is compiled Rust and its default is a *relative* path, the
claim "we export FASTEMBED_CACHE_DIR" proves nothing on its own: exporting a name
the library ignores would pass an env-var check and change nothing. So this script
does not check the environment — it checks where the 87 MB lands, three ways:

  RED     nothing pins the cache, the SDK embeds from a scratch cwd.
          EXPECT the model tree to appear at ``<cwd>/.fastembed_cache`` and
          nowhere else. This is the bug, and it is also the proof that a
          writer exists at all.
  GREEN   ``FASTEMBED_CACHE_DIR`` points at a FRESH, NON-EXISTENT directory.
          EXPECT the model tree to appear *inside that directory* — created by
          the library — and the scratch cwd to stay empty. This is the knob
          being honoured, which is the thing no other check on this card can
          establish.
  PLUGIN  import ``memori_byodb`` the way Hermes does, with ``$HOME`` redirected
          at a tmp dir and the variable left UNSET in the environment.
          EXPECT the model tree under ``$HOME/.cache/fastembed`` and the cwd to
          stay empty: the shipped fix, end to end, in the process shape of a
          real agent.

Each case runs in its own child process with its own scratch cwd, and every path
it can touch lives under one probe root that this script creates and (unless
``--keep``) removes. It never reads, writes, or deletes any of the pre-existing
``.fastembed_cache`` roots on the host — the count of those before and after is
printed so a run that leaked would be visible in its own output.

Cold cases download the 87 MB model. To keep that to one download when you are
re-running, ``--seed`` copies an existing cache into the target first (which then
proves only the *directory* claim, not the download claim).

Usage::

    python3 scripts/verify_fastembed_cache_dir.py            # red, green, plugin
    python3 scripts/verify_fastembed_cache_dir.py --cases green,plugin
    python3 scripts/verify_fastembed_cache_dir.py --keep     # leave the trees
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The directory name fastembed writes when nothing pins it.
CWD_CACHE_DIRNAME = ".fastembed_cache"
#: What the model repo looks like inside a cache dir.
MODEL_DIR_GLOB = "models--*"
#: The SDK's configured embedding model (memori/_config.py: Embeddings.model).
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
#: How the plugin names the knob it sets. Kept as a literal, not an import, so
#: the green/red cases cannot accidentally inherit the helper's value.
CACHE_ENV_VAR = "FASTEMBED_CACHE_DIR"

#: Child program. Embeds once and reports what the filesystem looks like. The
#: provider import is env-gated so the plugin case can run the *same* child:
#: only `memori_byodb` differs between `plugin` and `green`, which is what
#: makes the pair a controlled comparison rather than two different programs.
_CHILD = """
import json, os
from pathlib import Path

CACHE_DIRNAME = ".fastembed_cache"
before = os.environ.get("FASTEMBED_CACHE_DIR")

if os.environ.get("PROBE_IMPORT_PROVIDER") == "1":
    import memori_byodb  # noqa: F401  <- the import Hermes performs

from memori import embed_texts

model = os.environ.get("PROBE_MODEL", "all-MiniLM-L6-v2")
vectors = embed_texts(["fastembed cache knob probe"], model)
cwd = Path(os.getcwd())
print(json.dumps({
    "before": before,
    "after": os.environ.get("FASTEMBED_CACHE_DIR"),
    "dim": len(vectors[0]) if vectors and vectors[0] else 0,
    "cwd": str(cwd),
    "cwd_cache": (cwd / CACHE_DIRNAME).is_dir(),
    "cwd_entries": sorted(p.name for p in cwd.iterdir()),
}))
"""


def _scan_existing_caches(home: Path) -> list[Path]:
    """Pre-existing `.fastembed_cache` roots under $HOME (depth-bounded, read-only)."""
    out: list[Path] = []
    for dirpath, dirnames, _files in os.walk(home):
        depth = len(Path(dirpath).relative_to(home).parts)
        if depth >= 8:
            dirnames[:] = []
            continue
        if CWD_CACHE_DIRNAME in dirnames and Path(dirpath).name != CWD_CACHE_DIRNAME:
            out.append(Path(dirpath) / CWD_CACHE_DIRNAME)
            dirnames.remove(CWD_CACHE_DIRNAME)
    return sorted(out)


def _tree_report(root: Path) -> dict:
    """What a cache dir actually holds — the evidence, not a boolean."""
    if not root.exists():
        return {"exists": False, "bytes": 0, "models": []}
    models = sorted(p.name for p in root.glob(MODEL_DIR_GLOB))
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "exists": True,
        "bytes": sum(p.stat().st_size for p in files),
        "files": len(files),
        "models": models,
    }


def _provider_shim_dir(probe_root: Path, repo_root: Path) -> str:
    """A sys.path entry that exposes ``memori_byodb`` WITHOUT the repo root.

    Measured, not theorised: the first run of the ``plugin`` case put the repo
    root on ``PYTHONPATH`` and died with::

        ImportError: cannot import name 'embed_texts' from 'memori'
          (.../hscc/memori/__init__.py)

    because the repo's then-named ``memori/`` — the CLOUD memory provider —
    shadowed the installed ``memori`` SDK for anything that puts a directory
    containing it on ``sys.path``. That dir is now ``memori_cloud/``
    (t_2ab0eda1), but the shim stays: the honest way to reproduce a real agent
    process is a path entry holding only the plugin it needs, not the whole repo
    root (Hermes loads plugins by file location for the same reason).
    """
    shim = probe_root / "provider-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "memori_byodb"
    if not link.exists():
        link.symlink_to(repo_root / "memori_byodb", target_is_directory=True)
    return str(shim)


def _run_case(name: str, probe_root: Path, *, env_extra: dict | None,
              pythonpath: list[str] | None, home: Path, seed_from: Path | None,
              import_provider: bool = False) -> dict:
    """One child process: scratch cwd, its own $HOME, one embed call.

    The pinned target is deliberately left NON-EXISTENT unless ``--seed`` asked
    for it: "the library created the directory we named" is part of what is
    being proven.
    """
    case_root = probe_root / name
    cwd = case_root / "cwd"
    cwd.mkdir(parents=True)
    home_dir = case_root / "home"
    home_dir.mkdir()
    target = case_root / "pinned-cache"            # what green names
    home_cache = home_dir / ".cache" / "fastembed"  # what the plugin default names

    env = {
        "HOME": str(home_dir),
        "USERPROFILE": str(home_dir),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        # No FASTEMBED_CACHE_DIR unless the case puts it there.
        "PROBE_MODEL": EMBEDDING_MODEL,
        "PROBE_IMPORT_PROVIDER": "1" if import_provider else "0",
    }
    for var in ("VIRTUAL_ENV", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if var in os.environ:
            env[var] = os.environ[var]
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env.update(env_extra or {})

    if seed_from is not None:  # --seed: the weaker/faster mode, see the docstring
        for landing in (target, home_cache):
            shutil.copytree(seed_from, landing, dirs_exist_ok=True)

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=1800,
    )
    child = {}
    if proc.returncode == 0 and (proc.stdout or "").strip():
        child = json.loads(proc.stdout.strip().splitlines()[-1])

    return {
        "case": name,
        "rc": proc.returncode,
        "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:],
        "child": child,
        "cwd": str(cwd),
        "cwd_cache": _tree_report(cwd / CWD_CACHE_DIRNAME),
        "target": str(target),
        "target_cache": _tree_report(target),
        "home_default_cache": _tree_report(home_cache),
    }


def _find_existing_cache_source() -> Path | None:
    """A warm cache to seed from, if this host has one (used only with --seed)."""
    for cand in (Path.home() / ".cache" / "fastembed",
                 Path.home() / CWD_CACHE_DIRNAME):
        if cand.is_dir() and any(cand.glob(MODEL_DIR_GLOB)):
            return cand
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default="red,green,plugin",
                    help="comma list from red,green,plugin (default: all three)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the probe trees instead of deleting them")
    ap.add_argument("--seed", action="store_true",
                    help="copy an existing cache into each target first (one download cheaper; "
                         "proves the directory claim, not the download claim)")
    ap.add_argument("--json", dest="as_json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in ("red", "green", "plugin")]
    if unknown:
        ap.error(f"unknown case(s): {unknown}")

    repo_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    before = _scan_existing_caches(home)
    seed = _find_existing_cache_source() if args.seed else None
    if args.seed and seed is None:
        print(f"--seed asked for but no warm cache found under {home}", file=sys.stderr)
        return 2

    probe_root = Path(tempfile.mkdtemp(prefix="fastembed-verify-"))
    results: list[dict] = []
    try:
        if "red" in wanted:
            results.append(_run_case(
                "red", probe_root, env_extra=None, pythonpath=None, home=home,
                seed_from=None))
        if "green" in wanted:
            results.append(_run_case(
                "green", probe_root,
                env_extra={CACHE_ENV_VAR: str(probe_root / "green" / "pinned-cache")},
                pythonpath=None, home=home, seed_from=seed))
        if "plugin" in wanted:
            results.append(_run_case(
                "plugin", probe_root, env_extra=None,
                pythonpath=[_provider_shim_dir(probe_root, repo_root)], home=home,
                seed_from=seed, import_provider=True))

        # Delete only what this run created — the probe root is a mkdtemp dir.
        if not args.keep:
            shutil.rmtree(probe_root, ignore_errors=True)
    finally:
        after = _scan_existing_caches(home)

    failures = _judge(results, probe_root)
    if not args.as_json:
        _print(results, before, after, probe_root, failures)
    else:
        print(json.dumps({"results": results, "existing_before": [str(p) for p in before],
                          "existing_after": [str(p) for p in after],
                          "failures": failures}, indent=2))
    return 1 if failures else 0


def _judge(results: list[dict], probe_root: Path) -> list[str]:
    """The assertions, kept separate from the plumbing so they are readable."""
    fails: list[str] = []
    for r in results:
        case, child = r["case"], r["child"]
        if r["rc"] != 0:
            fails.append(f"{case}: child exited {r['rc']}: {r['stderr_tail']}")
            continue
        if not child.get("dim"):
            fails.append(f"{case}: embed returned no vector ({child})")
        cwd_cache, target = r["cwd_cache"], r["target_cache"]

        if case == "red":
            # The bug must reproduce, or the test proves nothing.
            if not (cwd_cache["exists"] and cwd_cache["bytes"] > 10_000_000):
                fails.append(f"red: expected the model under <cwd>/{CWD_CACHE_DIRNAME}, "
                             f"got {cwd_cache}")
            if target["exists"]:
                fails.append(f"red: nothing pinned a target yet it was written: {target}")
        elif case == "green":
            if not (target["exists"] and target["bytes"] > 10_000_000):
                fails.append(f"green: FASTEMBED_CACHE_DIR was IGNORED — the model did not "
                             f"appear in the pinned dir: {target}")
            if cwd_cache["exists"]:
                fails.append(f"green: pinned dir set but the cwd still gained "
                             f"{CWD_CACHE_DIRNAME}: {cwd_cache}")
            if r["child"].get("before") is None:
                fails.append("green: child had no FASTEMBED_CACHE_DIR at all — vacuous")
        elif case == "plugin":
            home_cache = r["home_default_cache"]
            if not (home_cache["exists"] and home_cache["bytes"] > 10_000_000):
                fails.append(f"plugin: importing memori_byodb did not pin the cache to "
                             f"$HOME/.cache/fastembed: {home_cache}")
            if r["child"].get("before") is not None:
                fails.append(f"plugin: the child inherited {CACHE_ENV_VAR} from the "
                             f"environment — the provider pin is untested: {r['child']}")
            if cwd_cache["exists"]:
                fails.append(f"plugin: cwd gained {CWD_CACHE_DIRNAME} anyway: {cwd_cache}")
    return fails


def _print(results: list[dict], before: list[Path], after: list[Path],
           probe_root: Path, failures: list[str]) -> None:
    for r in results:
        child = r["child"]
        print(f"━━━ {r['case']} ━━━")
        print(f"  child: rc={r['rc']} env_before={child.get('before')!r} "
              f"env_after={child.get('after')!r} dim={child.get('dim')}")
        for label in ("cwd_cache", "target_cache", "home_default_cache"):
            rep = r[label]
            where = r["cwd"] if label == "cwd_cache" else r["target"] if label == "target_cache" else "$HOME/.cache/fastembed"
            print(f"  {label:17s} {where}")
            print(f"  {'':17s} {rep}")
    print(f"━━━ pre-existing caches: {len(before)} before, {len(after)} after ━━━")
    for p in before:
        print(f"  {p}")
    for f in failures:
        print(f"FAIL {f}")
    print("probe root (deleted unless --keep):", probe_root)
    print("VERDICT:", "FAIL" if failures else "PASS")


if __name__ == "__main__":
    sys.exit(main())
