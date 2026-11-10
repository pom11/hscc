kind: Added
task: t_2ab0eda1

- **A check that fails while `memori/` shadows the installed `memori` SDK.**
  `hscc/memori/` — the cloud Hermes provider — is a **package named `memori`**,
  which is also the import name of the third-party SDK both providers themselves
  need (`memori/client.py:30` and `memori_byodb/_client.py:90` each run
  `from memori import Memori`). Any `sys.path` entry holding `memori/` therefore
  wins the name and the SDK becomes **unreachable, not degraded**: the SDK's own
  `__init__` performs absolute intra-package imports and **95** of its files do
  (`grep -rl '^\(from\|import\) memori[. ]'` over
  `site-packages/memori/`), so even `import memori` followed by
  `from memori import Memori` dies with
  `ImportError: cannot import name 'Memori' from 'memori' (…/hscc/memori/__init__.py)`.
  Hermes never hits it — `hermes_cli/plugins_loader.py:808` loads a directory
  plugin as `hermes_plugins.<slug>` via `spec_from_file_location`, so
  `sys.modules["memori"]` stays the SDK — which is exactly why the hazard stayed
  invisible while `memori` ships in `install_payload.DEFAULT_PAYLOAD`
  (`hscc-bootstrap/install_payload.py:597`) to `~/.hermes/plugins/memori/`.

  **It is already realised, not latent, in the test suite.** Measured:
  `scripts/run_tests.sh:247` runs `"$PY" -m pytest` with the repo root as cwd,
  and `python -m` prepends the cwd — so `sys.path[0]` is the repo root and
  **every one of the 12 legs** runs in the shadowed shape today. Reproduced with
  a probe module living outside the repo (no tree litter), cwd varied:
  `cwd=repo → syspath_head[0] = <repo>, resolved = <repo>/memori/__init__.py,
  Memori MISSING`; `cwd=/tmp → resolved = site-packages/memori, Memori OK`.
  The deployed dir shadows identically (`PYTHONPATH=~/.hermes/plugins →
  resolved = ~/.hermes/plugins/memori/__init__.py`), and a clean process is the
  control (`resolved = site-packages/memori/__init__.py`, both symbols import).

  `scripts/verify_memori_sdk_shadowing.py` is that measurement as an executable,
  on the same `VERDICT PASS|FAIL` contract as `verify_fastembed_cache_dir.py`:
  one child per path-root, each reporting where `memori` resolved and whether two
  public SDK symbols import. **It exits 1 today** — that is this card's
  acceptance criterion stated as a runnable. A `control` case runs first and a
  `UNDECIDED` exit 2 is reported when the SDK is not importable even there, so a
  host without the SDK cannot be misread as a host without the bug.
  `scripts/tests/test_memori_sdk_shadowing.py` (9 cases) proves the check can
  tell the two situations apart on tmp fixtures — a fake provider package vs a
  fake SDK root, both directions — keeps the one-symlink shim defence
  (`_provider_shim_dir`) pinned so nobody "simplifies" a provider child back onto
  the repo root, and holds the repo/deploy state itself as two
  `xfail(strict=True)` cases: the suite stays green while the layout decision is
  open, and the day option A or C lands they XPASS and turn the leg **red**.
  That red means *delete the marker, keep the test*.
---
kind: Verified
order: 1

- `scripts/tests/test_memori_sdk_shadowing.py`: **9 cases** (7 passed, 2 xfailed)
  from the repo root, the shape the `scripts` leg runs in.
  `scripts/tests/test_readme_lint.py` + `test_changelog_fragments.py`:
  **88 passed** (the new audit doc needs a `docs/README.md` entry or DOC-UNLISTED
  breaks the leg — same trap t_317a0f30 hit).
  `scripts/verify_memori_sdk_shadowing.py`: `VERDICT FAIL`, **exit 1**, with
  `leg-shape`, `repo-root` and `deployed-plugins` all failing and `control`
  passing.
  Nothing here was run against a real embedding model and nothing writes outside a
  tmp dir; no provider is imported in process by the guard.
---
kind: Docs

- `docs/audits/memori-sdk-shadowing-t_2ab0eda1.md` — the measurement log, the
  four options with what each one actually fixes, and two ruled out **on
  evidence**: "load the SDK under a private name" (the spec is found correctly at
  `site-packages`, then `exec_module` dies with `ModuleNotFoundError: No module
  named 'memori._config'` because the absolute intra-package imports still resolve
  through the provider that owns `sys.modules["memori"]`), and a bare rename
  (`install_payload` rmtree's only the destination it is installing and sweeps only
  `.bak-` entries, so a renamed dir leaves `~/.hermes/plugins/memori/` deployed
  side-by-side — shadowing and double-discovered — until someone deletes it).
  Also records the measured blast radius, that `memori_byodb` **must not** be
  renamed (`memory.provider: memori_byodb` pins that key in live config) while the
  cloud dir is pinned nowhere, and that the card's "renamed before" lead comes up
  empty: `git log --all --find-renames --diff-filter=R` has no `memori` rename.
