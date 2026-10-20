"""Declaration <-> live agreement for the v4 template declarations (spec §6).

Pure-read companion to :func:`hscc_daemon.verify.check_provider_routing`: the
applied template (``~/.hscc/applied_template.json``, written by
``hscc template apply`` with the nested ``declarations`` record from C2 /
t_70814001) says what the operator DECLARED and what apply RESOLVED. This
module asks, per leg, whether the LIVE state agrees — and never touches
anything:

  PURE READ: never repairs, never restarts, never writes any file. A leg that
  cannot be read is UNVERIFIED (``ok=None``) — the provider_routing fail-closed
  doctrine (t_ac795424). "Could not check" is never a silent pass, and never
  a hard failure either.

The four legs, named exactly as docs/design/2026-10-15-template-declarations.md
§6 numbers them (each is a function returning the standard
``{"name", "ok", "detail"}`` shape, plus ``remediation`` where there is an
action to name, and ``next_step`` for the verify table's hint contract):

  1. ``hermes_endpoints``   — hermes <-> ``endpoints:`` (spec §6.1): the
     existing fleet-node scan stays in ``provider_routing`` (UNCHANGED, not
     duplicated here); this leg adds the positive agreement — each consumer
     the applied template declares must carry the declared target's URL/alias
     in the root config and each matched profile.
  2. ``proxy_declaration``  — proxy.yaml <-> the applied ``proxy:`` block
     (spec §6.2). ``router_settings`` is read NESTED FIRST
     (``gateways.litellm.router_settings``, top-level fallback) per the
     2026-10-19 addendum item 1, via the ONE implementation of that read
     order — ``proxy_router.router_settings_source``.
  3. ``balancer_pair``      — the balancer-pair cross-ref (spec §6.3,
     shared with t_39d7c915 as ONE implementation referenced, not
     duplicated): when proxy.yaml asks for ``balancing``, the generated
     ``litellm_config.yaml`` must carry ``litellm_settings.callbacks`` with
     ``sparkrun_balancer.handler`` AND the module file must exist next to it;
     otherwise WARN (never auto-repair, never restart) — the operator WARN
     doctrine already encoded in :mod:`hscc_daemon.proxy_router`.
  4. ``live_model_info``    — spec §6.4: each routed target's
     ``/v1/model/info`` model set must cover the ids the applied
     ``endpoints:`` routes hermes to.

Address doctrine: this module never resolves node addresses. It reads loopback
proxy URLs from the applied record / hermes configs and, where a fleet-node
question would arise, :func:`verify.check_provider_routing` (sparkrun's own
``resolve_hosts`` under sparkrun's venv python) already owns it. No ssh, no
sparkrun contact here.

All inputs are injectable (``applied_state_path`` / ``config`` /
``profiles_dir`` / ``proxy_config`` / ``litellm_config`` / ``balancer_module``
/ ``_http_get`` / ``_read_json`` / ``_read_yaml``): tests build healthy and
broken worlds in tmp dirs and never touch the operator's state, the fleet, or
the network. Placeholders in tests: 10.0.0.x / 100.64.0.1 only.
"""

import json
import os
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml ships with hermes
    yaml = None

try:
    from . import proxy_router as _proxy_router
except Exception:  # pragma: no cover - import-time safety
    _proxy_router = None

try:
    from . import serving as _serving
except Exception:  # pragma: no cover - import-time safety
    _serving = None

try:
    from . import health as _health
except Exception:  # pragma: no cover - import-time safety
    _health = None

#: Spec §3.2 — the operator's load-instrument. Every remediation that speaks
#: load/watch conditions names this command, not board-guessing.
LOAD_INSTRUMENT = "hscc cluster monitor --live"

#: The §2.2 blessed default for a uniform_pool apply, and where proxy.yaml
#: must carry it (sparkrun 0.4.0 read order, addendum item 1).
BALANCING_DEFAULT = "least-in-flight"

#: Paths are resolved AT CALL TIME (never baked at import) so the
#: hscc_daemon/tests conftest ~/.hscc redirect keeps every check hermetic —
#: same reasoning as verify._APPLIED_TEMPLATE_STATE_REL.
_APPLIED_STATE_REL = "~/.hscc/applied_template.json"
_PROXY_YAML_REL = "~/.config/sparkrun/proxy.yaml"
_LITELLM_YAML_REL = "~/.cache/sparkrun/proxy/litellm_config.yaml"
_BALANCER_FILE = "sparkrun_balancer.py"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


# ── shared readers ──────────────────────────────────────────────────────────

def _default_http_get(url, timeout=5):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _read_json_default(path):
    with open(path) as f:
        return json.load(f)


def _read_yaml_default(path):
    if yaml is None:
        return None, "unverified: pyyaml not installed"
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        return None, f"unverified: file absent ({path})"
    except OSError as exc:
        return None, f"unverified: could not read {path} ({exc})"
    except Exception as exc:  # yaml.YAMLError and whatever a corrupt doc raises
        return None, f"unverified: could not read {path} ({exc})"
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        return None, f"unverified: {path} is not a mapping"
    return doc, None


def _origin(url):
    """``(scheme, host, port)`` for a url, or None. Mirrors verify._origin
    (loopback comparisons ignore the path, so a trailing /v1 never matters).
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return (parsed.scheme.lower(), parsed.hostname.lower(), port)
    except (ValueError, AttributeError, TypeError):
        return None


def _is_loopback(url):
    o = _origin(url)
    return bool(o) and o[1] in _LOOPBACK_HOSTS


def load_applied_state(applied_state_path=None, _read_json=None):
    """(state_dict_or_None, error_or_None). The ONE applied-state reader the
    four legs share; missing/unreadable is the §6 "no applied state" case —
    every leg turns that into ok=None, never a pass.
    """
    path = os.path.expanduser(applied_state_path or _APPLIED_STATE_REL)
    reader = _read_json or _read_json_default
    try:
        state = reader(path)
    except FileNotFoundError:
        return None, (f"unverified: no applied-template state ({path}) — "
                      "cannot tell what the template declared (fail-closed)")
    except Exception as exc:
        return None, (f"unverified: applied-template state unreadable "
                      f"({path}: {exc})")
    if not isinstance(state, dict):
        return None, f"unverified: applied-template state is not an object ({path})"
    return state, None


def endpoint_expectations(state):
    """The written ``endpoints:`` consumers from the applied record, as ONE
    shared projection (spec §6 legs 1 and 4 read the same view — one
    implementation, so they can never disagree about what was declared).

    Returns ``(mode, uniform, chat_declared, written, skips)``:

      mode           "proxy-alias" | "explicit" | None (nothing declared)
      uniform        bool of the recorded uniform_pool flag
      chat_declared  the raw declared ``chat`` target, or None
      written        {consumer_key: entry} for consumers apply actually wrote
                     (probe "ok" AND a model/url recorded). consumer_key is
                     "delegation" or "profiles.<name>".
      skips          [(consumer_key, why)] for declared-but-unwritten entries
                     (probe refused / omitted / unresolved id) — reported in
                     the leg detail, never silently dropped.

    A chat entry never appears in ``written`` (apply writes the model block
    from the chat decision inside ``_update_hermes_config``, not through the
    per-consumer writer), so leg 1 verifies chat through the §1.2 "literally
    the same value" invariant instead of a recorded URL.
    """
    state = state or {}
    decl = state.get("declarations")
    uniform = bool(state.get("uniform_pool"))
    if not isinstance(decl, dict):
        return None, uniform, None, {}, []
    ep = decl.get("endpoints")
    if not isinstance(ep, dict):
        return None, uniform, None, {}, []
    mode = ep.get("mode") if ep.get("mode") in ("proxy-alias", "explicit") \
        else "proxy-alias"
    raw = ep.get("declared") if isinstance(ep.get("declared"), dict) else {}
    chat_declared = raw.get("chat")
    resolved = ep.get("resolved") if isinstance(ep.get("resolved"), dict) else {}
    written, skips = {}, []
    for key, entry in sorted(resolved.items()):
        if not isinstance(entry, dict):
            continue
        probe = entry.get("probe")
        if probe == "omitted":
            skips.append((key, "declaration omitted the consumer (left untouched)"))
            continue
        if probe == "refused":
            skips.append((key, f"resolve refused: {entry.get('note') or 'no endpoint'}"))
            continue
        if probe != "ok" or not entry.get("url"):
            skips.append((key, f"probe status {probe!r} — apply wrote nothing"))
            continue
        if key == "chat":
            # Defensive: apply's record does not carry a chat entry today; if a
            # later apply adds one, leg 4 can use it and leg 1 still checks the
            # invariant. Keep it OUT of `written` so no code path double-counts.
            skips.append((key, "chat is verified via the model/custom identity "
                               "invariant, not a recorded write"))
            continue
        written[key] = entry
    return mode, uniform, chat_declared, written, skips


# ── leg 1: hermes <-> endpoints (spec §6.1) ─────────────────────────────────

def _root_model_and_custom(root):
    """``(model_block, custom_base_url)`` from a parsed root config."""
    model_block = root.get("model") if isinstance(root.get("model"), dict) else {}
    custom_url = None
    providers = root.get("providers")
    if isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict) and p.get("name") == "custom":
                bu = p.get("base_url")
                if isinstance(bu, str):
                    custom_url = bu
                break
    return model_block, custom_url


def check_hermes_endpoints(applied_state_path=None, config=None,
                           profiles_dir=None, _read_json=None,
                           _read_yaml=None):
    """Leg 1 (§6.1): hermes configs carry the DECLARED endpoint targets.

    The fleet-node scan stays in ``verify.check_provider_routing`` (spec:
    "existing fleet-node scan (unchanged)") — this leg is the positive half:
    for every consumer the applied record says apply WROTE, the live config
    must carry the declared URL (origin-compared, path-insensitive) — and the
    written model id where apply wrote one too (delegation, profiles).

    Chat is checked through the §1.2 invariant instead of a recorded URL
    (apply never records one): ``model.base_url`` and the ``custom``
    provider's ``base_url`` must be LITERALLY the same value, and under
    proxy-alias mode both loopback-shaped (provider_routing polices the
    fleet-node half).

    A config this leg must agree with but cannot read is UNVERIFIED —
    never a silent pass. Fallback-provider entries are deliberately NOT
    rechecked here: apply writes them in lockstep with ``delegation`` and
    provider_routing already guards their hosts; re-walking them here would be
    a second owner of the same invariant.
    """
    state, err = load_applied_state(applied_state_path, _read_json=_read_json)
    if err:
        return {"name": "declaration_agreement.hermes_endpoints", "ok": None,
                "detail": err,
                "remediation": f"apply a template first, or point "
                               f"applied_state_path at the applied record; "
                               f"load instrument: {LOAD_INSTRUMENT}"}
    mode, uniform, chat_declared, written, skips = endpoint_expectations(state)

    notes = []
    unverified = []
    problems = []
    if mode is None and not uniform:
        return {"name": "declaration_agreement.hermes_endpoints", "ok": True,
                "detail": "not applicable: applied template declared no "
                          "endpoints: and is not uniform_pool — nothing to "
                          "agree with"}

    root_path = os.path.expanduser(
        config or os.path.join(os.path.expanduser("~/.hermes"), "config.yaml"))
    read_yaml = _read_yaml or _read_yaml_default
    root, rerr = read_yaml(root_path)
    if rerr:
        # The applied record declares an endpoint invariant on THIS file; if we
        # cannot read it there is no evidence of agreement. Fail closed.
        return {"name": "declaration_agreement.hermes_endpoints", "ok": None,
                "detail": f"unverified: root hermes config unreadable "
                          f"({root_path}: {rerr})"}

    model_block, custom_url = _root_model_and_custom(root)
    model_url = model_block.get("base_url")

    # Chat invariant (§1.2 rows 1-2): one computation point => literally the
    # same value in both consumers.
    if chat_declared is not None or uniform:
        if not isinstance(model_url, str) or not isinstance(custom_url, str):
            unverified.append(
                "chat: model.base_url or custom.base_url missing in root "
                "config — cannot verify the same-value invariant")
        elif model_url != custom_url:
            problems.append(
                f"chat: model.base_url ({model_url!r}) and custom provider "
                f"base_url ({custom_url!r}) differ — §1.2 requires literally "
                "the same value")
        elif mode != "explicit" and not _is_loopback(model_url):
            problems.append(
                f"chat: proxy-alias mode but both consumers name a non-loopback "
                f"URL ({model_url!r}) — provider_routing polices fleet-node "
                "hosts; this is the shape-declaration half")
        else:
            notes.append(f"chat target {chat_declared or '<uniform pool front>'}"
                         f": model/custom carry the same URL ({model_url})")
    else:
        notes.append("chat: not declared under a non-uniform template — "
                     "nothing to agree with")

    # Per-consumer URL agreement (delegation, profiles.<name>).
    pd = os.path.expanduser(
        profiles_dir or (_serving.PROFILES_DIR if _serving is not None
                         else "~/.hermes/profiles"))
    for key, entry in written.items():
        want = _origin(entry["url"])
        if key == "delegation":
            dele = root.get("delegation")
            dele = dele if isinstance(dele, dict) else {}
            got = _origin(dele.get("base_url") if dele else None)
            where = "root: delegation.base_url"
            id_val = dele.get("model")
        elif key.startswith("profiles."):
            name = key.split(".", 1)[1]
            pfile = os.path.join(pd, name, "config.yaml")
            prof, perr = read_yaml(pfile)
            if perr:
                # The applied record says apply WROTE this profile's model
                # block; a profile we cannot read is missing evidence.
                unverified.append(f"{key}: profile config unreadable "
                                  f"({pfile}: {perr})")
                continue
            prof_model = prof.get("model") if prof else None
            prof_model = prof_model if isinstance(prof_model, dict) else {}
            got = _origin(prof_model.get("base_url") if prof_model else None)
            where = f"profile:{name}: model.base_url"
            id_val = prof_model.get("default")
        else:
            unverified.append(f"{key}: unknown consumer key — cannot check")
            continue
        if want is None or got is None:
            unverified.append(f"{key}: declared URL {entry['url']!r} or live "
                              f"{where} unparseable")
            continue
        if got != want:
            problems.append(f"{key}: {where} origin {got} != declared target "
                            f"URL {entry['url']} (origin {want})")
            continue
        want_model = entry.get("model")
        if want_model is not None and id_val != want_model:
            problems.append(f"{key}: live model id {id_val!r} != the id apply "
                            f"probe-confirmed at the declared target "
                            f"({want_model!r})")
            continue
        notes.append(f"{key} -> {entry['url']} (target "
                     f"{entry.get('target')}) agrees")

    for key, why in skips:
        notes.append(f"{key}: not checked ({why})")

    detail = "; ".join((problems or unverified or notes))
    if problems:
        return {"name": "declaration_agreement.hermes_endpoints", "ok": False,
                "detail": f"{len(problems)} declared-endpoint disagreement(s): "
                          + detail,
                "remediation": f"re-apply the template "
                               f"(`hscc template apply <name> --confirm`) — "
                               f"this check never repairs; load instrument: "
                               f"{LOAD_INSTRUMENT}"}
    if unverified:
        return {"name": "declaration_agreement.hermes_endpoints", "ok": None,
                "detail": "unverified: " + detail,
                "remediation": f"read the named config(s); a leg that cannot "
                               f"be read is unverified, never a pass "
                               f"(provider_routing doctrine)"}
    return {"name": "declaration_agreement.hermes_endpoints", "ok": True,
            "detail": f"declared endpoints agree with live configs: {detail}"}


# ── leg 2: proxy <-> declaration (spec §6.2) ────────────────────────────────

def check_proxy_declaration(applied_state_path=None, proxy_config=None,
                            _read_json=None, _read_yaml=None):
    """Leg 2 (§6.2): ``~/.config/sparkrun/proxy.yaml`` agrees with the applied
    ``proxy:`` declaration.

    Reads proxy.yaml through ``proxy_router`` — the ONE implementation of the
    sparkrun 0.4.0 read order (``gateways.litellm.router_settings`` NESTED
    first, top-level only as the 0.3.10 fallback; addendum item 1). The
    expected balancing policy is the applied record's resolved
    ``proxy.balancing`` when the template declared ``proxy:``; when it did not
    but the pool is uniform, the §2.2 blessed default
    (``least-in-flight``) is the expectation the operator blessed. The inert
    §2.1 knobs (max_parallel_requests / weight / timeout / num_retries) are
    expected ABSENT from proxy.yaml (recorded, never written — §2.3/§8), and
    their presence is named as drift the other direction.

    Never writes proxy.yaml, never restarts — and says so in the remediation:
    a restart is the operator's detached cutover unit
    (docs/audits/sparkrun_proxy_venv_cutover.sh, addendum item 4).
    """
    res_name = "declaration_agreement.proxy_declaration"
    state, err = load_applied_state(applied_state_path, _read_json=_read_json)
    if err:
        return {"name": res_name, "ok": None, "detail": err}
    decl = state.get("declarations")
    proxy_rec = decl.get("proxy") if isinstance(decl, dict) else None
    proxy_rec = proxy_rec if isinstance(proxy_rec, dict) else None
    uniform = bool(state.get("uniform_pool"))

    if proxy_rec is None and not uniform:
        return {"name": res_name, "ok": True,
                "detail": "not applicable: no proxy: declaration recorded and "
                          "not uniform_pool — proxy.yaml has no declared "
                          "expectation here"}

    if _proxy_router is None:
        return {"name": res_name, "ok": None,
                "detail": "unverified: hscc_daemon.proxy_router unavailable — "
                          "the nested router_settings read order (addendum "
                          "item 1) has one owner and it is missing"}

    path = os.path.expanduser(proxy_config or _PROXY_YAML_REL)
    proxy_doc, perr = (_read_yaml or _read_yaml_default)(path)
    if perr:
        # §6: "any leg that cannot be read (no proxy.yaml) is unverified".
        # The one exception: nothing was ever expected here (no declaration,
        # non-uniform) — handled above — so every path down this branch has a
        # real expectation the file cannot answer.
        want_label = (f"balancing {(proxy_rec or {}).get('balancing')!r}"
                      if proxy_rec else
                      f"{BALANCING_DEFAULT} (§2.2 uniform_pool default)")
        return {"name": res_name, "ok": None,
                "detail": f"unverified: proxy.yaml unreadable ({path}: {perr}) "
                          f"— the applied declaration expects {want_label}"}

    proxy_doc = proxy_doc or {}
    intent = _proxy_router.configured_intent(proxy_doc)
    problems, notes = [], []

    if proxy_rec is not None and proxy_rec.get("written") is False:
        # The applied record itself says the write path never landed the
        # region; live proxy.yaml cannot be asked to agree with intent apply
        # never wrote. That is NOT a silent pass — it names the state loudly.
        return {"name": res_name, "ok": None,
                "detail": f"unverified: applied record says proxy declaration "
                          f"NOT written (written=False); live proxy.yaml "
                          f"carries balancing={intent['balancing_requested']!r}",
                "remediation": "fix the sparkrun write path and re-apply; this "
                               "check never writes proxy.yaml"}

    expected = (proxy_rec or {}).get("balancing") if proxy_rec else None
    expected_source = "applied declaration"
    if expected is None:
        expected, expected_source = BALANCING_DEFAULT, "§2.2 blessed default"

    got = intent["balancing_requested"]
    if got == expected:
        notes.append(f"gateways.litellm.balancing={got!r} matches "
                     f"{expected_source}")
    else:
        problems.append(
            f"proxy.yaml gateways.litellm.balancing={got!r} but "
            f"{expected_source} expects {expected!r}")

    # router_settings: report the value AND the key path sparkrun reads (the
    # addendum's whole point). v1 declares no router_settings knob, so only an
    # invalid (non-mapping) value is drift.
    raw_rs = intent.get("raw_router_settings")
    if raw_rs is not None and not isinstance(raw_rs, dict):
        problems.append(
            f"router_settings at {intent['router_settings_source']!r} is "
            f"{raw_rs!r}, not a mapping — sparkrun drops it; no live effect")
    elif raw_rs is not None:
        notes.append(f"router_settings read from "
                     f"{intent['router_settings_source']!r} (nested-first, "
                     f"addendum item 1): {dict(raw_rs)}")
    else:
        notes.append("no router_settings in proxy.yaml (v1 declares none)")

    # §2.3 item 3: the inert knobs are recorded, never written — so proxy.yaml
    # must NOT carry per-deployment extras. proxy.yaml has no model_list
    # region in the v1 write path; if one appeared it is not hscc's.
    inert = (proxy_rec or {}).get("inert") or []
    if inert:
        if "model_list" in proxy_doc:
            problems.append("proxy.yaml carries a model_list region but the "
                            "applied record says the §2.1 knobs are "
                            "recorded-only (INERT, §4.3/§8) — who wrote this?")
        else:
            notes.append(f"{len(inert)} inert §2.1 knob group(s) recorded "
                         f"(INERT-PENDING-SPARKRUN); correctly absent from "
                         f"proxy.yaml")

    restart_pending = bool((proxy_rec or {}).get("restart_required"))
    if restart_pending:
        notes.append("applied record says the region CHANGED: LiteLLM reads "
                     "router settings at startup — restart is the operator "
                     "cutover unit docs/audits/sparkrun_proxy_venv_cutover.sh "
                     "§6 never restarts from a check")

    if problems:
        return {"name": res_name, "ok": False,
                "detail": "proxy.yaml disagrees with the applied "
                          f"declaration: {'; '.join(problems)}",
                "remediation": "re-apply (write goes through sparkrun's own "
                               "ProxyConfig path) and, if the region changed, "
                               "run docs/audits/sparkrun_proxy_venv_cutover.sh "
                               f"— then read the spread: {LOAD_INSTRUMENT}"}
    return {"name": res_name, "ok": True,
            "detail": "proxy.yaml agrees with the applied declaration: "
                      + "; ".join(notes)}


# ── leg 3: balancer pair (spec §6.3 — ONE implementation, t_39d7c915) ───────

def check_balancer_pair(proxy_config=None, litellm_config=None,
                        balancer_module=None, _read_yaml=None):
    """Leg 3 (§6.3): proxy.yaml asks for balancing => the pair is live.

    The pair check EXISTS once (t_39d7c915): the proxy.yaml-vs-generated-config
    comparison delegates to :func:`hscc_daemon.proxy_router.router_drift`
    (mirrors sparkrun's own emission rules; WARN-never-fail operator doctrine)
    and this leg only adds the §6.3 file-half — the balancer MODULE next to the
    generated config. Nothing here repairs, restarts, or fails the cluster
    over drift: the verdict is WARN (``ok=True`` with a ``WARN:``-prefixed
    detail — the proxy_router doctrine) or UNVERIFIED for inputs that cannot
    be read.
    """
    res_name = "declaration_agreement.balancer_pair"
    if _proxy_router is None:
        return {"name": res_name, "ok": None,
                "detail": "unverified: hscc_daemon.proxy_router unavailable "
                          "(the balancer-pair check has one owner — t_39d7c915)"}

    drift = _proxy_router.router_drift(proxy_config=proxy_config,
                                       litellm_config=litellm_config)
    if drift["status"] in ("skip",):
        # No proxy.yaml at all: nothing asked for balancing (router_drift's
        # own documented semantics) — not a pass of a live pair, and not
        # unverified either: the §6.3 condition ("WHEN proxy.yaml asks for
        # balancing") is simply not met.
        return {"name": res_name, "ok": True,
                "detail": f"not applicable: {drift['detail']}"}
    if drift["status"] == "unverified":
        return {"name": res_name, "ok": None, "detail": f"unverified: {drift['detail']}"}

    # Ask-half: does proxy.yaml request a balancer at all?
    proxy_doc, perr = (_read_yaml or _read_yaml_default)(
        os.path.expanduser(proxy_config or _PROXY_YAML_REL))
    if perr:
        return {"name": res_name, "ok": None,
                "detail": f"unverified: could not re-read proxy.yaml "
                          f"({perr})"}
    intent = _proxy_router.configured_intent(proxy_doc)
    if not intent["callback"]:
        # proxy.yaml asks for NO balancer (nothing, or an unimplemented
        # value — router_drift already WARNed about the latter). Any drift
        # router_drift found on the OTHER axis (router_settings) is named in
        # the detail — the proxy_router_settings verify check owns it too;
        # this leg must not swallow it.
        if intent["balancing_requested"] is not None:
            return {"name": res_name, "ok": True,
                    "detail": f"WARN: {drift['detail']}"}
        if drift["status"] == "warn":
            return {"name": res_name, "ok": True,
                    "detail": f"WARN: {drift['detail']} (no balancer "
                              f"requested — pair leg itself not applicable)"}
        return {"name": res_name, "ok": True,
                "detail": "not applicable: proxy.yaml requests no balancer"}

    # Ask-half positive: config callbacks half is router_drift's verdict; add
    # the module-file half here (§6.3: the file must exist next to the config).
    litellm_path = os.path.expanduser(
        litellm_config or _LITELLM_YAML_REL)
    module_path = os.path.expanduser(
        balancer_module
        or os.path.join(os.path.dirname(litellm_path), _BALANCER_FILE))
    missing = []
    if "WARN:" in drift["detail"] or drift["status"] == "warn":
        missing.append(f"generated config half: {drift['detail']}")
    if not os.path.isfile(module_path):
        missing.append(f"{_BALANCER_FILE} absent next to {litellm_path} — the "
                       "balancer callback would import a module that is not "
                       "there")
    if missing:
        return {"name": res_name, "ok": True,
                "detail": "WARN: " + "; ".join(missing),
                "remediation": "never auto-repaired by this check and never "
                               "restarted — the fix is a sparkrun proxy "
                               "regeneration via the operator cutover unit "
                               f"docs/audits/sparkrun_proxy_venv_cutover.sh; "
                               f"load instrument: {LOAD_INSTRUMENT}"}
    return {"name": res_name, "ok": True,
            "detail": f"balancer pair live: callbacks+module present "
                      f"({intent['callback']} @ {module_path})"}


# ── leg 4: live model info (spec §6.4) ──────────────────────────────────────

def check_live_model_info(applied_state_path=None, _read_json=None,
                          _http_get=None, timeout=5):
    """Leg 4 (§6.4): each routed target's ``/v1/model/info`` model set covers
    the ids the applied ``endpoints:`` routes hermes to.

    One fetch per DISTINCT consumer URL (targets may share the pool front).
    ``/v1/model/info`` — not ``/v1/models`` — because it is the endpoint that
    reveals the LIVE router (health._proxy_backend_probe's reasoning: a proxy
    can list ids while routing everything to one backend; the info body is the
    only text that carries per-model ``litellm_params``).

    An unreachable target is UNVERIFIED (§6: "unreachable proxy" is listed as
    an unverified leg), never a pass and never a fault — the proxy health
    itself is verify's ``proxy`` check's job.
    """
    res_name = "declaration_agreement.live_model_info"
    state, err = load_applied_state(applied_state_path, _read_json=_read_json)
    if err:
        return {"name": res_name, "ok": None, "detail": err}
    mode, uniform, chat_declared, written, _skips = endpoint_expectations(state)
    if not written and chat_declared is None and not uniform:
        return {"name": res_name, "ok": True,
                "detail": "not applicable: applied endpoints: route nothing "
                          "and chat is not declared"}

    # Expected ids per target URL. Chat's live id is whatever the root config
    # now says (checked for agreement by leg 1); here the declared-target
    # coverage is the written consumers' ids. A uniform_pool apply with no
    # written consumers has nothing further to cover at the info endpoint —
    # provider_routing + the proxy catalogue check own that case.
    by_url = {}
    for key, entry in written.items():
        by_url.setdefault(entry["url"], set()).add(entry.get("model"))
    expected_urls = {u: sorted(m for m in ids if m) for u, ids in by_url.items()}
    if not expected_urls:
        return {"name": res_name, "ok": True,
                "detail": "not applicable: no probe-confirmed consumer writes "
                          "to cover at /v1/model/info"}

    get = _http_get or _default_http_get

    def _info_url(url):
        """http://localhost:4000/v1 -> http://localhost:4000/v1/model/info
        (spec §6.4 names /v1/model/info; health._proxy_backend_probe's URL
        shape). A URL without the /v1 suffix gains the full path."""
        trimmed = url.rstrip("/")
        if trimmed.endswith("/v1"):
            return trimmed + "/model/info"
        return trimmed + "/v1/model/info"

    problems, unverified, notes = [], [], []
    for url in sorted(expected_urls):
        info_url = _info_url(url)
        try:
            body = get(info_url, timeout)
        except Exception as exc:
            unverified.append(f"{info_url}: unreachable ({exc})")
            continue
        try:
            data = json.loads(body.decode("utf-8", "replace")
                              if isinstance(body, bytes) else body)
        except Exception as exc:
            unverified.append(f"{info_url}: unparseable body ({exc})")
            continue
        served = set()
        for m in (data.get("data") or []):
            name = m.get("model_name") or m.get("id")
            if name:
                served.add(str(name))
        missing = [m for m in expected_urls[url] if m not in served]
        if missing:
            problems.append(
                f"{info_url}: model set does not cover the routed "
                f"id(s) {missing} (serves {sorted(served) or 'nothing'})")
        else:
            notes.append(f"{info_url} covers routed "
                         f"{expected_urls[url]} ({len(served)} served)")

    if problems:
        return {"name": res_name, "ok": False,
                "detail": "live model info does not cover the routed aliases: "
                          + "; ".join(problems),
                "remediation": f"this check never repairs or restarts; read "
                               f"the live spread with {LOAD_INSTRUMENT}, then "
                               f"re-apply so the probe-before-write gate re-"
                               f"resolves every consumer id"}
    if unverified:
        return {"name": res_name, "ok": None,
                "detail": "unverified: " + "; ".join(unverified)
                          + (" — plus " + "; ".join(notes) if notes else "")}
    return {"name": res_name, "ok": True,
            "detail": "live model info covers every routed alias: "
                      + "; ".join(notes)}


# ── the four legs, as one call ──────────────────────────────────────────────

LEGS = ("hermes_endpoints", "proxy_declaration", "balancer_pair",
        "live_model_info")


def run_declaration_agreement(leg="all", **inputs):
    """Run the four §6 legs and aggregate.

    ``leg`` names ONE leg (for tests / targeted debugging); the default runs
    all four. Aggregation for the combined verdict: any False -> False, any
    None -> None, else True — fail-closed ordering (a leg we could not read
    never hides a leg we could). Each per-leg dict is returned verbatim under
    ``legs`` so callers (the verify table, `hscc project doctor`) render the
    four names individually.
    """
    kw = dict(inputs)
    runners = {
        "hermes_endpoints": check_hermes_endpoints,
        "proxy_declaration": check_proxy_declaration,
        "balancer_pair": check_balancer_pair,
        "live_model_info": check_live_model_info,
    }
    if leg != "all" and leg not in runners:
        return {"name": "declaration_agreement", "ok": None,
                "detail": f"unverified: unknown leg {leg!r} "
                          f"(known: {', '.join(LEGS)})"}
    wanted = LEGS if leg == "all" else (leg,)
    results = []
    for name in wanted:
        import inspect
        params = set(inspect.signature(runners[name]).parameters)
        results.append(runners[name](**{k: v for k, v in kw.items()
                                        if k in params}))
    oks = [r["ok"] for r in results]
    ok = False if False in oks else (None if None in oks else True)
    detail = "; ".join(f"{r['name'].split('.', 1)[1]}={r['ok']} "
                       f"[{r['detail']}]" for r in results)
    out = {"name": "declaration_agreement", "ok": ok, "detail": detail,
           "legs": results}
    hints = [r.get("remediation") for r in results
             if not r["ok"] and r.get("remediation")]
    if hints:
        out["next_step"] = " | ".join(hints)
    return out
