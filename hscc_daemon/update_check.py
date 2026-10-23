"""`hscc update --check` — is a newer HSCC release out, and how do I get it?

Read-only by construction. This module talks to exactly one URL
(:data:`LATEST_RELEASE_API`), writes exactly one file (:data:`CACHE_FILE`
under ``~/.hscc``), and NEVER starts, stops, installs or upgrades anything.
The operator upgrades by running the printed command themselves.

Three surfaces read this module, and they differ deliberately in how LOUD
they are (the body of card t_a4055572 asks for exactly that split; the tests
in ``hscc_daemon/tests/test_update_check.py`` are named after these rows):

  * ``hscc update --check`` (the explicit ask) — always fetches, always
    prints what it compared, and on a hard failure says so on stderr and
    exits 1. A user who asked to be told must never be told nothing.
  * ``hscc status`` (ambient) — reads the cache ONLY, never fetches. Prints
    one INFO line when the cache says "newer" and is fresher than
    :data:`CACHE_TTL_SECONDS`; prints nothing in every other case, and can
    never change status' exit code (spec: "never makes status fail").
  * the daemon (ambient) — :func:`daemon_tick`, called from the existing
    300 s liveness loop: fetches at most once per :data:`CACHE_TTL_SECONDS`
    because the cache is the throttle, logs one INFO/WARN line when a newer
    release is out, and swallows every error (a failed check must not be an
    ERROR-level daemon event).

A FAILED check does not poison the cache: the last good answer stays (so
``status`` keeps showing the notice until it ages out) and the next tick or
verb retries immediately — see :func:`record_check_failure`.

Version truth: the installed version is what ``hscc --version`` prints, i.e.
the repo-root ``VERSION`` file read through :func:`hscc_daemon.hscc._get_version`
(no pyproject at root). Latest truth: the GitHub releases API, which is the
right source for a public repo whose users install from a release tarball
(the Homebrew formula's own livecheck is :strategy:`github_latest` — the
same source, so ``brew`` and ``hscc update --check`` can never disagree).

No third-party dependency (urllib only) so the check works in a bare brew
venv where only PyYAML/rich/psutil are guaranteed installed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The repo whose releases we are the installed copy of.
GITHUB_REPO = "pom11/hscc"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Short by design (spec: 5 s). The check must never be the reason a status
# call or a daemon tick stalls.
FETCH_TIMEOUT_SECONDS = 5

# How long a cached answer stays usable, and how old it must be before a
# "once a day" surface fetches again. Both meanings are the same number:
# "cache age <= 24h" is the freshness window the body specifies for the
# status notice, and re-fetching only when the cache leaves that window is
# what makes the daemon's tick at most once/day.
CACHE_TTL_SECONDS = 24 * 3600

#: The cache location — a ``~``-relative template, expanded at CALL time by
#: :func:`_cache_path` (never at import). This is the convention conftest's
#: autouse HOME-redirect depends on for a NEW module constant: the fixture
#: patches ``os.path.expanduser`` at runtime and rebinds each enumerated
#: module constant, so an import-time ``expanduser`` here would bake the
#: operator's real home in and every test would write the live ``~/.hscc``
#: — the leak class conftest.py documents three times as a release blocker.
#: (conftest.py::_module_attrs also rebinds CACHE_FILE to the per-test dir.)
CACHE_FILE = "~/.hscc/update-check.json"

# gh is a nicer transport (authed -> 5000 req/h instead of 60, and no second
# rate-limit bucket when the operator's CI is hammering the anonymous API),
# but it is strictly optional: the urllib path must work without it, because
# public CI and a fresh brew install have no gh.
GH_BIN = "gh"

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


class UpdateCheckError(Exception):
    """A check could not be completed (network, rate limit, bad payload).

    Raised by :func:`fetch_latest_release`; the surfaces decide what to do
    with it (verb: loud, status/daemon: silent). Never a crash: every caller
    catches it.
    """


class UnknownVersion(Exception):
    """A tag or a VERSION string this module cannot order against another.

    Split from :class:`UpdateCheckError` on purpose: an unparseable VERSION
    means "I could not compare", NOT "the network failed" — so it never
    writes ``error`` into the cache (the cached answer is still good) and
    never counts against the fetch throttle.
    """


# ── installed version ─────────────────────────────────────────────────────

def _version_key(text):
    """Sortable digit tuple for ``v2.13.0`` / ``2.13.0``.

    Digits, not strings: ``"2.9.0" > "2.10.0"`` lexically, which is the exact
    trap t_bc5a3fee hit when picking the newest ``libexec/hscc-<ver>``.
    Raises :class:`UnknownVersion` when there is no dotted number to order.
    """
    m = _VERSION_RE.search(str(text or ""))
    if not m:
        raise UnknownVersion(f"not a version I can order: {text!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def display_tag(tag):
    """``v2.13.0`` / ``2.13.0`` -> ``v2.13.0`` (exactly one leading v).

    GitHub tags in this repo carry the ``v``, VERSION does not — every
    surface formats versions as ``vX.Y.Z``, so all three normalise through
    here instead of prefixing ad hoc (a bare ``f"v{tag}"`` on an already-v'd
    tag prints ``vv2.13.0``).
    """
    s = str(tag or "").strip()
    return s if s.startswith("v") else f"v{s}"


def installed_version():
    """The version this running code reports as itself.

    Delegates to :func:`hscc_daemon.hscc._get_version` — the one function
    ``hscc --version`` already uses — so the check can never compare a
    different string than the banner prints.
    """
    from hscc_daemon.hscc import _get_version
    return _get_version()


def version_compare(installed, latest):
    """``-1`` installed is older, ``0`` equal, ``1`` newer.

    Padded to a common length so ``2.13`` and ``2.13.0`` compare equal — a
    tag is not allowed to look newer than the release it names.
    Raises :class:`UnknownVersion` if either side is not orderable.
    """
    a, b = _version_key(installed), _version_key(latest)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


# ── install mode ──────────────────────────────────────────────────────────
#
# Detection ORDER matters: a brew tree IS a release tree with no .git, so
# "dev" is a fallback, not a first guess (see detect_install_mode).

#: :func:`detect_install_mode` — the marker the tap drops inside libexec/hscc.
MODE_BREW = "brew"
#: :func:`detect_install_mode` — a git checkout of the repo.
MODE_DEV = "dev"
#: :func:`detect_install_mode` — neither signal matched.
MODE_UNKNOWN = "unknown"


def _source_root():
    """The repo-root-equivalent this running code was loaded from.

    ``<root>/hscc_daemon/update_check.py`` -> ``<root>``. For a brew install
    that is ``<prefix>/libexec/hscc`` (the tap copies the whole tree there);
    for a checkout it is the checkout.
    """
    return Path(__file__).resolve().parents[1]


def _has_brew_marker(path):
    """True if ``path`` carries the tap's libexec marker.

    The same filename and semantics t_bc5a3fee's ``bootstrap.sh`` uses, so
    both tools agree on "this tree came from brew" and, more importantly,
    both survive the tap's own layout. Checked on the source root AND each
    ancestor, because the deployed plugins copy (``~/.hermes/plugins/hscc``)
    and the libexec copy are different paths to the same release.
    """
    for cand in [Path(path)] + list(Path(path).parents):
        if (cand / ".hscc-brew-libexec").is_file():
            return True
    return False


def _under_brew_prefix(path):
    """Path-shape fallback: ``Cellar``/``homebrew``/``libexec/hscc[-*]``.

    Covers t_bc5a3fee's ``bootstrap.sh`` shapes (``*/Cellar/*|
    */libexec/hscc-*``) AND the tap's actual current layout, which is the
    version-STABLE ``<prefix>/libexec/hscc`` (Formula/hscc.rb installs the
    tree to ``libexec/hscc`` and puts the venv beside it at ``libexec/venv``)
    — the marker is the primary signal precisely because a prefix convention
    is a guess; this fallback only matters for a tap build predating it.
    """
    parts = [p.lower() for p in Path(path).parts]
    if "cellar" in parts or "homebrew" in parts:
        return True
    for a, b in zip(parts, parts[1:]):
        if a == "libexec" and (b == "hscc" or b.startswith("hscc-")):
            return True
    return False


def detect_install_mode(source_root=None):
    """``brew`` | ``dev`` | ``unknown`` for the tree this code runs from.

    Order (the reason dev is LAST: a brew tree is a release tarball with no
    ``.git``, so asking "is this a checkout?" first would call a brew install
    a dev install and print ``git pull`` to someone with no repo):

      1. the ``.hscc-brew-libexec`` marker the tap drops (primary — a tap can
         change its prefix and the marker still says what it is);
      2. path shape (fallback — an older tap build predates the marker);
      3. a ``.git`` at the source root -> dev;
      4. unknown (the caller prints both commands rather than guessing).
    """
    root = Path(source_root) if source_root else _source_root()
    try:
        if _has_brew_marker(root):
            return MODE_BREW
        if _under_brew_prefix(root):
            return MODE_BREW
        if (root / ".git").exists():
            return MODE_DEV
    except OSError:
        return MODE_UNKNOWN
    return MODE_UNKNOWN


def upgrade_command(mode=None, source_root=None):
    """The one actionable command for this install (single source of truth).

    Every surface that names an upgrade path — the verb, the status notice,
    the daemon log line — goes through here, so the wording evolves in one
    place (the body's "make the command string come from one helper").

    brew:  ``brew upgrade hscc``
    dev:   ``git -C <repo> pull`` + the bootstrap reminder. The reminder
           prefers ``hscc bootstrap --yes`` and keeps the script form as the
           fallback, phrased so it stays true whether or not the installed
           build already HAS the ``bootstrap`` verb (that verb is t_bc5a3fee;
           this card ships before it is necessarily deployed).
    unknown: both commands, so the operator is never told the wrong thing.
    """
    mode = mode or detect_install_mode(source_root)
    if mode == MODE_BREW:
        return "brew upgrade hscc"
    if mode == MODE_DEV:
        root = str(Path(source_root) if source_root else _source_root())
        script = os.path.join(root, "hscc-bootstrap", "bootstrap.sh")
        return (f"git -C {root} pull, then refresh the install with "
                f"'hscc bootstrap --yes' (or '{script} --yes' on a build "
                "that predates the bootstrap verb)")
    return ("upgrade for your install mode: 'brew upgrade hscc' (brew) or "
            "'git -C <repo> pull && hscc-bootstrap/bootstrap.sh --yes' "
            "(development checkout)")


# ── fetch ─────────────────────────────────────────────────────────────────

def _gh_argv():
    """``gh`` argv for the latest-release lookup, or None if unusable.

    Requires a runnable AND authenticated gh — ``gh auth status`` exits
    non-zero for "installed, not logged in", and an unauthenticated gh call
    would burn the same 60 req/h budget the anonymous path gets while
    adding a subprocess.
    """
    exe = shutil.which(GH_BIN)
    if not exe:
        return None
    try:
        probe = subprocess.run([exe, "auth", "status"],
                               capture_output=True, timeout=FETCH_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return [exe, "api", LATEST_RELEASE_API, "--jq", ".tag_name"]


def _tag_from_gh():
    argv = _gh_argv()
    if argv is None:
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=FETCH_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateCheckError(f"gh api failed: {exc}") from exc
    if proc.returncode != 0:
        raise UpdateCheckError(
            f"gh api exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:160]}")
    tag = (proc.stdout or "").strip()
    if not tag:
        raise UpdateCheckError("gh api returned no tag_name")
    return tag


def _tag_from_http():
    """Anonymous REST read. Works with no gh, no token, in a bare venv."""
    req = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "hscc-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(
            f"GitHub releases API returned HTTP {exc.code}"
            f"{_rate_limit_hint(exc)}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateCheckError(f"unreachable: {getattr(exc, 'reason', exc)}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("GitHub releases API returned malformed JSON") from exc
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not tag:
        raise UpdateCheckError("GitHub releases API response has no tag_name")
    return str(tag).strip()


def _rate_limit_hint(exc):
    """' (rate limited)' when a 403/429 is the anonymous quota being spent.

    Without it the one line an operator sees offline-versus-quota-limited is
    identical, and the fixes are completely different (wait vs. ``gh``).
    """
    try:
        if exc.code in (403, 429):
            return " (rate limited — sign in with 'gh auth login' for a higher quota)"
    except AttributeError:
        pass
    return ""


def fetch_latest_release():
    """The latest published tag (e.g. ``v2.13.0``).

    Transport preference: authenticated ``gh`` if present (higher rate limit),
    plain anonymous urllib otherwise — and the urllib path must work with no
    gh at all, which is what public CI and a fresh brew install have.

    Raises :class:`UpdateCheckError` for anything that kept the tag unknown
    (offline, DNS, TLS, quota, non-JSON, no ``tag_name``).
    """
    try:
        tag = _tag_from_gh()
        if tag:
            return tag
        return _tag_from_http()
    except UpdateCheckError:
        raise
    except Exception as exc:                      # never let a surface crash
        raise UpdateCheckError(f"{type(exc).__name__}: {exc}") from exc


# ── cache ─────────────────────────────────────────────────────────────────

def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(ts):
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _cache_path(cache_file=None):
    """Expand the cache path at CALL time (never at import).

    ``CACHE_FILE`` is a ``~``-template on purpose (see its comment): the
    suite's autouse HOME-redirect patches ``os.path.expanduser`` at runtime,
    and expanding here is what lets it redirect this file too. An absolute
    ``cache_file`` (every test passes one) is returned unchanged.
    """
    if cache_file is not None:
        return os.path.expanduser(str(cache_file))
    return os.path.expanduser(CACHE_FILE)


def load_cache(cache_file=None):
    """The cached check dict, or None when absent / unreadable / malformed.

    A corrupt cache is treated as "no cache", never as an error: the whole
    point of the cache is that status and the daemon can read it without
    needing a error-handling path of their own.
    """
    try:
        with open(_cache_path(cache_file)) as f:
            data = json.load(f)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError,
            PermissionError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cache_age_seconds(cache, now=None):
    """Age of a cached entry in seconds, or None when it has no usable stamp.

    Negative ages (the machine's clock went backwards) report None: the
    caller treats that as "not usable" rather than "fresh", so a bad clock
    can neither freeze nor fake a notice.
    """
    if not isinstance(cache, dict):
        return None
    stamp = _parse_iso(cache.get("checked_at"))
    if stamp is None:
        return None
    age = ((now or _utcnow()) - stamp).total_seconds()
    return age if age >= 0 else None


def cache_is_fresh(cache, now=None, ttl=None):
    """True if ``cache`` holds a usable answer (age within the TTL window)."""
    age = cache_age_seconds(cache, now=now)
    return age is not None and age <= (ttl or CACHE_TTL_SECONDS)


def write_cache(entry, cache_file=None):
    """Atomically persist ``entry``. Returns True on success, False otherwise.

    A cache write must never take down a caller: the state-dir convention
    (tmp + ``os.replace``) is reused from ``state.write_state`` and every
    failure is reported by return value, not by exception. The daemon and
    ``hscc status`` both ignore it — the cache is an optimisation, and
    unwritable merely means the next run retries.
    """
    path = _cache_path(cache_file)
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(entry, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def record_check(latest_tag, installed, cmp_result, mode=None,
                 cache_file=None, now=None):
    """Cache a completed comparison. Returns ``(entry, written)``."""
    entry = {
        "checked_at": (now or _utcnow()).isoformat(),
        "latest_tag": latest_tag,
        "installed": installed,
        "available": cmp_result < 0,
        "upgrade_cmd": upgrade_command(mode) if cmp_result < 0 else "",
    }
    return entry, write_cache(entry, cache_file=cache_file)


def record_check_failure(previous, error, cache_file=None, now=None):
    """What a failed check leaves behind: the last good answer, untouched.

    Spec: "a failed check does NOT poison the cache (next run retries;
    success caches 24h)". Recording the error only in memory (never on disk)
    is what keeps the status notice alive until its own TTL expires, while
    the next tick — unthrottled, because no fresh success was written —
    retries the fetch.
    """
    if not isinstance(previous, dict):
        return None
    return {**previous, "error": str(error)[:200]}


# ── comparison against the cache ──────────────────────────────────────────

def check_from_cache(cache=None, installed=None, cache_file=None, now=None):
    """What ``hscc status`` and the daemon may say, from the cache alone.

    Returns ``{"available", "latest_tag", "installed", "upgrade_cmd"}``, or
    None when there is nothing to say — which is every case except
    "a newer release is out AND the answer is inside the TTL window":
    no cache, stale cache (spec: status with a >24h cache prints nothing),
    cached "up to date", a corrupt stamp, or a comparison this module cannot
    order. Never fetches, never raises.
    """
    if cache is None:
        cache = load_cache(cache_file)
    if not isinstance(cache, dict):
        return None
    if not cache_is_fresh(cache, now=now):
        return None
    latest = cache.get("latest_tag")
    if not latest:
        return None
    installed = installed or installed_version()
    try:
        cmp_result = version_compare(installed, latest)
    except UnknownVersion:
        return None
    if cmp_result >= 0:
        return None
    return {
        "available": True,
        "latest_tag": display_tag(latest),
        "installed": display_tag(installed),
        "upgrade_cmd": cache.get("upgrade_cmd") or upgrade_command(),
    }


def run_check(cache_file=None, fetch=None, now=None):
    """One check: fetch, compare against the installed version, cache it.

    The single funnel every surface calls, so the loud/quiet split lives only
    in the callers. ``fetch`` is injectable — the tests hand in a fake, so
    the suite makes zero network calls (spec requirement), and a future
    offline mirror can be wired without touching the surfaces.

    Returns a result dict:
      ``{"ok": True, "available", "latest_tag", "installed", "cmp",
         "mode", "upgrade_cmd", "cache_written", "entry"}``
      ``{"ok": False, "error": str, "cached": <last good answer|None>}``

    Writes ONLY ``~/.hscc/update-check.json`` (spec: "no network writes
    anywhere", "NEVER writes outside ~/.hscc").
    """
    fetch = fetch or fetch_latest_release
    installed = installed_version()
    cache = load_cache(cache_file)
    try:
        latest = fetch()
        cmp_result = version_compare(installed, latest)
    except UpdateCheckError as exc:
        return {"ok": False, "error": str(exc),
                "cached": record_check_failure(cache, exc, now=now)}
    except UnknownVersion as exc:
        # Comparison problem, not a network problem: keep the cache clean and
        # say which side is unparseable.
        return {"ok": False, "error": str(exc), "cached": None, "unsortable": True}
    except Exception as exc:
        # A fetch that raises something outside the documented exception set
        # (an injected stub, a bug in a future transport) is still just "the
        # check failed" — no surface may crash because of it. Same non-
        # poisoning rule as UpdateCheckError.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "cached": record_check_failure(cache, exc, now=now)}
    mode = detect_install_mode()
    entry, written = record_check(latest, installed, cmp_result, mode,
                                  cache_file=cache_file, now=now)
    return {
        "ok": True,
        "available": cmp_result < 0,
        "latest_tag": str(latest),
        "installed": str(installed),
        "cmp": cmp_result,
        "mode": mode,
        "upgrade_cmd": upgrade_command(mode),
        "cache_written": written,
        "entry": entry,
    }


def check_due(cache_file=None, now=None, ttl=None):
    """True when a once-a-day surface should fetch again.

    The cache is the throttle: any fresh answer (success OR a cached
    "up to date") suppresses the fetch, so a daemon tick that wakes every
    300 s still calls the API at most once per TTL. A missing/stale cache is
    "due" — that is also how a failure retries.
    """
    return not cache_is_fresh(load_cache(cache_file), now=now, ttl=ttl)


def daemon_tick(cache_file=None, fetch=None, now=None, log_fn=None):
    """The daemon's daily update notice. Returns the notice text, or None.

    At most one GitHub call per TTL (the cache guards it), INFO when the
    fleet is current and WARN when a release is out — WARN is the highest
    level this ever uses, so the update notice can never turn a stream red
    (spec), and a failure is logged at INFO at worst: "offline" is not an
    ERROR for a check nobody asked for.

    Every exception is swallowed: the caller is a daemon loop that must not
    die because api.github.com is unreachable.
    """
    if log_fn is None:
        from hscc_daemon.daemon_ops import log as log_fn  # noqa: N806
    try:
        if not check_due(cache_file=cache_file, now=now):
            return None
        res = run_check(cache_file=cache_file, fetch=fetch, now=now)
        if not res.get("ok"):
            # Quiet by contract: a failed update check is not a fleet problem.
            return None
        if not res.get("available"):
            log_fn(f"HSCC is up to date (installed "
                   f"{display_tag(res['installed'])}, latest "
                   f"{display_tag(res['latest_tag'])})")
            return None
        notice = (f"HSCC {display_tag(res['latest_tag'])} available "
                  f"(installed {display_tag(res['installed'])}); upgrade: "
                  f"{res['upgrade_cmd']}")
        log_fn(notice, "WARN")
        return notice
    except Exception as exc:                       # a tick must never raise
        try:
            log_fn(f"update check skipped: {exc}")
        except Exception:
            pass
        return None


# ── human rendering ───────────────────────────────────────────────────────

def format_check_result(res):
    """The verb's stdout lines.

    Shapes are the contract brew-5 (t_31e996a7) copies into the README, so
    they are pinned string-for-string in test_update_check.py:

      up to date  -> ONE quiet line (spec: "Up to date: quiet one-liner
                     rc=0" — no comparison block for a non-event);
      newer       -> installed/latest comparison + the actionable command;
      installed newer (dev checkout ahead of the last release) -> comparison
                     + a plain-language explanation.
    """
    if res["cmp"] == 0:
        return [f"up to date — {display_tag(res['installed'])} is the "
                "latest release."]
    lines = [f"installed: {display_tag(res['installed'])}",
             f"latest:    {display_tag(res['latest_tag'])}"]
    if res.get("cached"):
        age = cache_age_seconds(res["cached"])
        if age is not None:
            lines.append(f"(using cached check from "
                         f"{int(age // 3600)}h ago)")
    if res["available"]:
        lines.append(f"upgrade:   {res['upgrade_cmd']}")
        lines.append("HSCC does not upgrade itself — run the command above.")
    else:
        lines.append("installed build is NEWER than the latest published "
                     "release (a development checkout?).")
    return lines
