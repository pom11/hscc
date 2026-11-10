# `memori/` (the cloud provider) shadows the installed `memori` SDK — audit + options

Card: t_2ab0eda1 · repo: pom11/hscc · measured at `origin/main` = `b87ad6b5`
(interpreter used for every measurement: `~/.hermes/hermes-agent/venv/bin/python`,
Python 3.14.10, memori SDK 3.3.6 at
`~/.hermes/hermes-agent/venv/lib/python3.14/site-packages/memori/`).

Status of this card: **the check ships here; the layout fix is a decision this
card explicitly does not own** (card body: "Not decided here … belongs with
whoever owns the plugin layout"). The decision is carded for the layout owner and
the costs below are measured, not estimated.

---

## 1. The mechanism, in one line

The repo root holds TWO top-level provider packages, `memori/` (cloud) and
`memori_byodb/` (BYODB). `memori/` is a *package named `memori`*, which is also
the import name of the third-party SDK. `sys.path` entries from `PYTHONPATH` are
prepended ahead of site-packages, so for any process whose path contains a
directory holding `memori/`, the name `memori` resolves to the provider and the
SDK is unreachable — not "partly wrong", **unreachable** (§4).

Both call sites that need the SDK use the absolute form, so both are hit:

| file:line | statement |
|---|---|
| `memori/client.py:30` | `from memori import Memori` |
| `memori_byodb/_client.py:90` | `from memori import Memori` |
| `memori_byodb/_client.py:106-107,124` | `from memori.storage.drivers.sqlite._driver import …`, `from memori.storage._registry import …`, `from memori.memory.augmentation.… import …` |

`memori_byodb` — the provider this fleet actually runs
(`~/.hermes/config.yaml`: `memory.provider: memori_byodb`) — is the victim, and
the thing that shadows it is its sibling.

## 2. Measurements

### M1 — the shadow fires for both roots, and only for those roots

Child process (`cwd=/tmp`), program:
`import memori` → report `memori.__file__`; then `from memori import embed_texts`
and `from memori import Memori`.

| `PYTHONPATH` | `memori.__file__` resolved | `from memori import embed_texts` |
|---|---|---|
| `<repo root>` | `<repo root>/memori/__init__.py` | `ImportError: cannot import name 'embed_texts' from 'memori'` |
| `~/.hermes/plugins` | `~/.hermes/plugins/memori/__init__.py` | `ImportError: cannot import name 'embed_texts' from 'memori'` |
| *(unset — control)* | `…/site-packages/memori/__init__.py` | **OK** (`Memori` also OK) |

The same `ImportError` shape for `Memori`, the symbol the providers actually
import. The **deployed** plugins dir shadows exactly like the repo root does:
this is not a checkout-only curiosity, because `memori` is in
`install_payload.DEFAULT_PAYLOAD` (`hscc-bootstrap/install_payload.py:597`) so the
dir ships to `~/.hermes/plugins/memori/`.

### M2 — Hermes itself cannot hit it, and why

`hermes_cli/plugins_loader.py::_load_directory_module` (line 808; source checkout
at `~/.hermes/hermes-agent/hermes_cli/plugins_loader.py`) imports a
directory plugin with
`importlib.util.spec_from_file_location(module_name, __init__.py)`, where
`module_name = hermes_plugins.<slug>` (`_NS_PARENT = "hermes_plugins"`, line 36;
`_directory_module_name`, line 789). So the provider lives in
`sys.modules["hermes_plugins.memori"]`, `sys.modules["memori"]` stays the SDK, and
the provider's `from memori import Memori` resolves correctly. The hazard needs a
*path* entry, which Hermes never creates. That is precisely why it stayed invisible.

### M3 — who DOES put a shadowing root on `sys.path` (measured in this repo)

| site | root inserted | reaches the SDK today? |
|---|---|---|
| `scripts/dep_pr_watcher.py:49-50` (the **installed cron poller**) | `sys.path.insert(0, ~/.hermes/plugins)` | No — imports `hscc_daemon.cron_watchers` → `hscc_daemon.state` only. **Latent**: the process is already one import away. |
| `scripts/install_dep_watcher.sh` (two `python3 - <<'PY'` heredocs, the uninstall marker at ~38-44 and the install marker at ~72-77) | `sys.path.insert(0, ~/.hermes/plugins)` | No — `from hscc_daemon import cron_watchers`. Latent, same reason. |
| `hscc-cluster/fleet.py:8-19` `_daemon_mod()` | `sys.path.insert(0, <plugins dir>)`, popped in `finally` | No, and the window is narrow (one `importlib.import_module` call). Latent. |
| `memori_byodb/tests/conftest.py:35` | `sys.path.insert(0, <memori_byodb dir>)` — the **plugin dir itself**, not the repo root | No. This is the *correct* shape and is why the memori_byodb leg is unaffected. |
| `run_tests.sh` legs | nothing: no root `conftest.py`, and no `__init__.py` in any `*/tests` dir (measured), so pytest's prepend mode inserts each `tests` dir, never the repo root | No. §5 pins this, because adding `memori_byodb/tests/__init__.py` would silently flip every leg into the shadowed shape. |

Answer to the question the card asks about the watcher: the watcher's process
**does** put the shadowing root on `sys.path` today; it does not currently reach
the SDK, so nothing breaks yet. Any future import of `memori*` inside that daily
cron pass fails with the M1 error and the failure would look like "the memori SDK
is broken on this host", not like a path problem.

### M4 — "load the SDK under a private name" is dead, measured

Idea: keep the dir, and have the providers load the SDK via
`importlib.machinery.PathFinder.find_spec("memori", <path minus the shadow>)` +
`module_from_spec` under a private name. Result of actually running it
(`~/.hermes/hermes-agent/venv/bin/python`, repo root on `sys.path`, provider
already imported as `memori`):

```
who_owns_memori : <repo root>/memori/__init__.py
sdk_spec_origin : …/site-packages/memori/__init__.py   <- found, correctly
sdk_load        : ModuleNotFoundError: No module named 'memori._config'
submodule       : ModuleNotFoundError: No module named 'memori.storage'
driver          : ModuleNotFoundError: No module named 'memori.storage'
```

The SDK's *own* `__init__` performs absolute intra-package imports, which resolve
through `sys.modules["memori"]` — still the provider. Static count of files doing
that inside the installed SDK: **95**. So a private-name load cannot even finish
importing the SDK, let alone run. **Rule out this option.** (In-repo precedent for
private-name loading does exist — `memori/install.py:28-36` loads `_paths.py` as
`memori_hermes._paths` — but it works there only because `_paths.py` has no
intra-package imports of its own.)

### M5 — what a rename would actually cost (measured blast radius)

- Single source of truth: `install_payload.DEFAULT_PAYLOAD`
  (`hscc-bootstrap/install_payload.py:597`, entry `"memori"`).
  `hscc_daemon/verify.py::check_plugin_payload` (line 1847+) loads *that list* by
  file path rather than re-globbing, so the deploy-drift check follows a rename
  automatically — no second place to edit.
- Live config references the cloud provider **nowhere**: `~/.hermes/config.yaml`
  has `plugins.enabled: [hscc-cluster, hscc-commands, sparkrun-hermes]` and
  `memory.provider: memori_byodb`. So the plugin key `memori` is not pinned by
  config on this host. `memori_byodb`'s key IS (`memory.provider`), so
  **`memori_byodb/` must not be renamed** — only the cloud dir is a candidate.
- Hardcoded deployed-name sites inside the plugin: `memori/_paths.py`
  (`PLUGIN_NAME = "memori"`, `plugin_target_dir()` = `<home>/plugins/<PLUGIN_NAME>`)
  and `memori/install.py` (uses those helpers).
- **The trap that makes this the layout owner's call, not mine:**
  `install_payload` rmtree's only the destination of the entry it is installing
  (`install_payload.py:300`) and sweeps only `.bak-` entries
  (`_sweep_old_backups`). It never prunes a deployed dir that *left* the payload.
  So after a rename, `~/.hermes/plugins/memori/` survives the next bootstrap
  side-by-side with the renamed dir: still shadowing the SDK for every M3 process,
  and still discovered as a second plugin by Hermes' scanner. A rename without an
  operator migration step therefore **fixes the repo shape and leaves the deployed
  shape fully in place**.
- No rename precedent for these dirs exists: `git log --all --find-renames
  --diff-filter=R` has no `memori` rename (the dirs were created in place). The
  card's "the two providers were renamed elsewhere-in-repo before" lead comes up
  empty for `memori`; the nearest structural precedents are the *relocation* of
  flightdeck → `hscc-project` and the *staged-path ≠ deployed-path* mechanism
  `PAYLOAD_EXTRA_DESTS` already implements for `video_gen/openai_compat`.

## 3. Options, with what each one fixes

| option | fixes repo-root shape | fixes deployed shape | needs operator/migration | risk |
|---|---|---|---|---|
| **A. Rename the cloud provider dir** (e.g. `memori_cloud/`, or stage under `install/hscc-plugins/`) | ✅ | ✅ *only after the stale `~/.hermes/plugins/memori/` is deleted* | yes — a deployed-dir deletion + a plugin-key change | low in config, **medium in deploy**: the stale-dir trap (M5) makes a half-done rename worse than none |
| **B. Load the SDK under a private name** | ❌ measured dead (M4) | ❌ | — | n/a — rule it out |
| **C. Stop putting shadowing roots on `sys.path`** at the three M3 sites (shim dir with one symlink, the technique `memori_byodb/tests/test_cache_env.py::_provider_shim_dir` already uses) | ✅ for those processes | ✅ for those processes | no | low, but it is **whack-a-mole**: any future `sys.path.insert(0, <plugins>)` re-opens it, which is why the guard below exists |
| **D. Do nothing, keep the guard visible** | ❌ | ❌ | — | the failure stays misdiagnosable as "the SDK is broken" |

A and C are complementary, not alternatives: A removes the landmine, C stops
anyone from standing on it while A is undecided.

## 4. The check that ships with this card

- `scripts/verify_memori_sdk_shadowing.py` — a standalone check, same
  `VERDICT PASS|FAIL` + non-zero-exit contract as `scripts/verify_fastembed_cache_dir.py`.
  It spawns a child per path-root and asserts the child resolves `memori` to the
  **SDK**, not to the root it was given. **It exits 1 today**, before any fix:
  that is the acceptance criterion of this card, stated as an executable.
- `scripts/tests/test_memori_sdk_shadowing.py` —
  - green and hermetic: the mechanism reproduced in a tmp fixture (a fake shadow
    + a fake SDK), the probe's own detection proven on both fixtures, and the
    leg-shape pin from §5;
  - `xfail(strict=True)`: the two repo-state cases (repo root, deployed plugins).
    Strict-xfail is deliberate — the suite stays green while the layout decision is
    open, and the day A or C lands the case XPASSes and the leg goes **red**, which
    is the signal to promote the marker into a plain assertion. Do not "fix" that
    red by deleting the test; delete the marker.

## 5. Leg-shape pin (green, and cheap insurance)

`run_tests.sh` runs one pytest process per plugin dir. No leg is shadowed today
for a specific structural reason: there is no repo-root `conftest.py`, and no
`*/tests/__init__.py` anywhere — so pytest's default prepend import mode inserts
each `tests` directory, never the repo root. Add `memori_byodb/tests/__init__.py`
and the basedir walks up to the repo root, `sys.path[0]` becomes the repo root, and
*every* SDK-touching test in *every* leg flips into the M1 error at once, with a
traceback that names the SDK. The guard pins that structure.

## 6. Left for the layout owner

One card, `hscc-orch` (the board's layout/gate owner), carrying §3's table and
M4/M5 as the evidence, to choose A (with the stale-dir migration named in the
card) or C (with the shim recipe referenced), or both.
