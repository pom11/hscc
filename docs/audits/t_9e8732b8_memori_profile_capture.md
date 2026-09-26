# Audit: memori_byodb still not capturing after t_57e5b3f7 "fix deployed" (t_9e8732b8)

- Task: t_9e8732b8 (assigned backend-engineer)
- Date: 2026-09-26
- Branch: wt/t_9e8732b8
- Status: root cause confirmed; fix in progress

## Question (from card)

Newest `memori_conversation_message.date_created` is still 2026-06-13 02:50:29
despite the t_57e5b3f7 fix (47e3ab2, direct-driver write in
`memori_byodb/local_augmentation.py`) being merged to main AND deployed. Why does
capture still write nothing?

## Measurement (read-only, reproduced)

```
sqlite3 ~/.hermes/memori_byodb.db \
  "SELECT id,role,datetime(date_created),substr(content,1,40) FROM memori_conversation_message ORDER BY date_created DESC LIMIT 5;"
```
→ newest = id 5/6, 2026-06-13 02:50:29, Twitter autosave cron. Counts:
memori_conversation=3, conversation_message=6, entity=3, entity_fact=25.
All capture-path rows frozen at 2026-06-13. One orphan `desac_hermes` entity
row (id 3, 2026-08-14) created before the SDK write failed.

## Root cause (TWO independent gaps)

The t_57e5b3f7 fix is correct in the repo AND present at
`~/.hermes/plugins/memori_byodb/local_augmentation.py` (10703 bytes, fixed
direct-driver write, verified identical to main). BUT Hermes multiplexed
profiles each load their memory provider from their OWN profile home:

  `user_plugins_dir()` = `get_hermes_home()/plugins`   (plugins/plugin_loader.py:28-32)
  `get_hermes_home()`  = `$HERMES_HOME` env (profile-scoped at runtime)

so the provider the orchestrator profile actually loads comes from
`~/.hermes/profiles/hscc-orch/plugins/memori_byodb/`, NOT
`~/.hermes/plugins/`. Probed directly (hermes venv, read-only):

```
HERMES_HOME=~/.hermes/profiles/hscc-orch find_provider_dir('memori_byodb')
  -> /Users/desac/.hermes/profiles/hscc-orch/plugins/memori_byodb
```
That dir's local_augmentation.py is the OLD 8942-byte buggy version (dated
Jun 12, the one raising `AttributeError: 'list' object has no attribute
'entity'`). Confirmed by `diff`: only local_augmentation.py differs between
the profile copy and the fixed default copy.

### Gap 1 — DEPLOY never reached the running profiles
`hscc-bootstrap/install_payload.py` installs to ONE plugins_dir, defaulting to
`~/.hermes/plugins` (the non-profile root home). It never writes into
`$HERMES_HOME/plugins` for any named profile. So the fixed code landed only
where no running session loads it from.

### Gap 2 — CONFIG lookup is profile-scoped but the store is shared
`memori_byodb/__init__.py` `_load_config(hermes_home)` looks for
`<hermes_home>/memori_byodb.json`. The single shared config lives at
`~/.hermes/memori_byodb.json` (default home) — there is exactly one
`memori_byodb.json` on disk and one shared DB
(`~/.hermes/memori_byodb.db`). For a profile-scoped home, `_load_config`
returns None → `initialize()` raises `RuntimeError("Memori BYODB is not
configured")` → provider never added → `_memory_manager` stays empty →
`sync_all` no-ops. Probed: `_load_config(get_hermes_home()) is None` for
hscc-orch. This is why there is zero "Memory provider 'memori_byodb'
activated" log activity since 2026-09-17 — the provider cannot even start.

## Evidence commands (all read-only)

```
# live DB newest rows + counts
sqlite3 ~/.hermes/memori_byodb.db "..."
# deployed fixed file present (default home)
diff -q ~/.hermes/plugins/memori_byodb/local_augmentation.py <(git show origin/main:memori_byodb/local_augmentation.py)
# provider the orchestrator profile actually loads (OLD, buggy)
ls -la ~/.hermes/profiles/hscc-orch/plugins/memori_byodb/local_augmentation.py  # 8942 bytes, Jun 12
# probe provider-dir + config resolution for a profile home
HERMES_HOME=~/.hermes/profiles/hscc-orch <hermes-venv>/python probe_home.py
```

## Fixes (in hscc repo)

- Fix A (memori_byodb/__init__.py): `_load_config` falls back to the default
  shared home (`~/.hermes/memori_byodb.json`) when the profile home has none —
  the store is shared by design (single DB, single global config).
- Fix B (hscc-bootstrap/install_payload.py): also deploy the payload into each
  existing named profile's `plugins/` dir so runtime code is current for every
  profile that loads it.

## Verify by execution

Real turn → row in a hermetic temp DB (hermetic test) + propagate to the live
profile plugin dirs + config visible, then confirm the provider can initialize.

## Constraints
- No write to live ~/.hermes/memori_byodb.db (hermetic tests use temp DB).
- Scrub LAN/tailnet addresses to 100.64.0.1; no secrets.
- Merge own branch to main + push + install_payload.py (now to profiles too).
- Suite green both interpreters.
