# A credential minted by a read: `hscc api status` writes `~/.hscc/api-token`

Card `t_cc7b6023` (found while auditing `t_88abe739` / PR #721, note §4).
Base `78be7fe3` (origin/main, includes PR #716).

## Verdict: VIOLATION. The mint moves behind `hscc api start`; readers get an error, not a credential.

The code contradicts its **own** documented contract, so this is not a wording
problem. `hscc api status` advertises a read in three places, and the phrase
that settles it is in its own help:

All line numbers are `origin/main` `78be7fe3` (the branch base).

| where | what it promises |
|---|---|
| `hscc_daemon/api_cli.py:148` (verb help) | "**Read-only report**: running / not running and the configured host:port." |
| `hscc_daemon/api_cli.py:151-152` (verb help) | "NEVER prints the auth token. Shows the scannable connection QR **when a token exists**" |
| `hscc_daemon/api_cli.py:1183` (docstring) | "Prints a scannable connection QR ... **when a token exists** — if the token is **missing**/unreadable, **says so and exits cleanly**." |

"When a token exists" is a read of existing state. The implementation calls a
function whose contract is the opposite — `hscc-api/api_server.py:95`
`load_token()`, "**Return the auth token, generating it on first run**".

So the fix is not to re-word the help. The help is already correct; the call is
wrong. And the help's own escape hatch ("if the token is missing ... says so and
exits cleanly") is the behaviour that was always intended and never implemented.

## The mint is ONE site, reached by two read paths

There is exactly one write of `api-token` in production code:
`hscc-api/api_server.py:124-140` inside `load_token()` (secrets.token_urlsafe(32),
mkdir, O_WRONLY|O_CREAT|O_EXCL 0600, tmp + os.replace). It has three production
callers, and only one of them is a read:

| caller | verb | verdict |
|---|---|---|
| `hscc_daemon/api_cli.py:946` in `_handle_start` | `hscc api start` | **legitimate** — an operator action that is about to bind a server |
| `hscc-api/api_server.py:1136` in `create_servers` | the serve boot path | **legitimate** — the server cannot answer auth without a token |
| `hscc_daemon/api_cli.py:1275` in `_handle_status` | `hscc api status` | **the violation** — inside the QR block at `:1273-1280` |

`hscc verify --json` does not mint anything itself. It reaches the same line
through a subprocess hop:

```
hscc verify --json
└─ verify.py:1521  check_api_routes
   └─ verify.py:1596  subprocess: <py> api_route_sweep.py --json
      └─ api_route_sweep.py:206  subprocess: hscc api status   <- when api.json has no host
         └─ api_cli.py:1275  api.load_token()                  <- MINT
            └─ api_server.py:124  secrets.token_urlsafe(32) + write 0600
```

`api_route_sweep.py:216 token()` and `verify_chat_roundtrip.py:112 api_token()`
are both plain `open().read()` — they never create. The same `hscc api status`
hop is in `verify_chat_roundtrip.py:101`, so `hscc verify`'s chat-roundtrip row
is a third door into the same line.

## Proof (this base, sandboxed `$HOME`, fleet sealed)

`HOME=<sandbox>` with an empty `~/.hscc`, the repo's own `hscc.py` as the CLI:

| command | rc | `~/.hscc/api-token` created? |
|---|---|---|
| `hscc api status` | 0 | **YES** — 43 chars, mode `0600` |
| `hscc api status --no-qr` | 0 | no |
| `hscc verify --json` | 1 (unrelated row) | **YES** — 43 chars, mode `0600` |
| `hscc verify --json`, `hscc` on PATH shimmed to a no-op | 1 | **no** |

The `--no-qr` row isolates the mint to the QR block (nothing else in `status`
writes). The shimmed-`hscc` row closes the verify chain from the other side:
when the `hscc api status` subprocess answers harmlessly instead of running the
real verb, verify leaves the sandbox untouched — so verify's token is made by
`api_cli.py:1275` and nowhere else.

## Why a mint-on-read is a security problem, not a tidiness problem

1. **No operator intent.** `secrets.token_urlsafe(32)` is the bearer credential
   for the whole API (`api_server.py:142 token_valid`, fail-closed auth). It
   comes into existence because something *asked for a status line*.
2. **Its existence silently changes other verbs.** `hscc notify list`
   (`notify_cli.py:291 read_token`) branches on whether the file exists: without
   it, rc non-zero with "no API token found under the hscc root — start the API
   once with `hscc api start`"; with it, notify proceeds. A read flipped that
   branch.
3. **Nothing asked for it, and nothing prints it.** The QR block prints a QR
   *encoding the credential to stdout* — under `status`, on a path that
   advertises "NEVER prints the auth token". A token minted by `hscc verify
   --json` in cron or CI is a live credential created by a machine, in a home
   directory nobody is looking at.
4. **`load_token`'s errors are startup errors.** Its fail-closed raises say
   "refusing to start" — prose for a verb that starts something. In `status`
   they are caught and re-routed to a QR notice (`api_cli.py:1276-1279`), so the
   fail-closed design is already being *read* as "no token", which is exactly
   what a non-minting reader expresses directly.
5. **The rest of the repo already assumes `start` mints.** `notify_cli.py:334`
   tells the operator to run `hscc api start` to create the file, and
   `verify.py:1605-1606` says the same. No code or text anywhere says "run a
   status read to create the token". The mint-on-read is incidental, not
   load-bearing.

## Fix shape (same commit as the guard)

- `hscc-api/api_server.py`: add **`peek_token(hscc_dir=None)`** next to
  `load_token` — the existing file's value, or `None` when there is no token.
  Never creates, never raises for a missing file. `load_token` keeps
  generate-on-first-run for the server path.
- `hscc_daemon/api_cli.py` `_handle_status`: the QR block calls `peek_token()`.
  No token → the message the docstring already promises, naming
  `hscc api start` as the verb that creates it. The resolution is
  `getattr(api, "peek_token", None)` and an absent reader means **no QR** — it
  must never fall back to `load_token`, or an old installed `hscc-api` payload
  would re-open the mint through the fallback.
- `_handle_start` (`api_cli.py:946`) keeps `load_token()` — minting behind an
  explicit verb is the design. `create_servers` (`api_server.py:1136`) keeps it
  too, for the same reason.
- Guard: `hscc_daemon/tests/test_api_cli_read_only.py`, the doctor-tool recipe
  (`hscc-cluster/tests/test_doctor_tool.py::_seal_door_env` /
  `_the_seal_actually_took` / `_snapshot`) — snapshot the sealed sandbox around a
  real `hscc api status` **and** a real `hscc verify --json` spawn, assert the
  tree is byte-identical, and prove the snapshot bites with a canary. The guard
  lives in the **surface's own suite**, per the parent's cross-directory lesson.
