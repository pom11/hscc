"""Pure-read drift check: proxy.yaml router intent vs the live litellm config.

The sparkrun LiteLLM proxy regenerates
``~/.cache/sparkrun/proxy/litellm_config.yaml`` from
``~/.config/sparkrun/proxy.yaml`` on every (re)start. Three settings decide
how requests are spread across fleet backends:

* ``router_settings:`` (proxy.yaml, top level) — emitted verbatim into
  ``litellm_config.yaml``'s top-level ``router_settings`` (sparkrun
  ``proxy/config.py::ProxyConfig.router_settings`` +
  ``proxy/engine.py::build_litellm_config``). Anything not a mapping is
  ignored by sparkrun, so it must be ignored here too.
* ``gateways: {litellm: {balancing: <value>}}`` — only ``least-in-flight`` is
  implemented (``BALANCING_CALLBACKS``); it makes sparkrun emit
  ``litellm_settings.callbacks: [sparkrun_balancer.handler]`` and copy the
  balancer module next to the config. Any other value sparkrun logs
  "Ignoring" and emits nothing.
* ``routing_strategy: least-busy`` inside ``router_settings`` is a TRAP on the
  pinned LiteLLM: its in-flight counter is only incremented from a sync hook
  ``Router.acompletion`` never calls, so every backend reads 0 and the tie
  goes to the first ``model_list`` entry (see sparkrun ``proxy/balancer.py``
  docstring). The working knob is ``gateways.litellm.balancing`` above.

Why this exists: after a sparkrun dep-bump that loses the fork branch (see
``dep_bump_guard.py``) the emission code disappears and the config silently
reverts — no error anywhere. This check makes that drift visible.

HARD CONSTRAINTS (operator rule): pure-read. Never writes, never restarts,
never mutates either config. Divergence is a WARN — the check never reports
a hard failure for drift (``ok`` is True with a ``WARN:`` detail; None only
when the inputs could not be read at all, i.e. genuinely unverified).
"""

#: The one ``gateways.litellm.balancing`` value sparkrun implements, and the
#: callback name it emits (mirrors sparkrun ``proxy/engine.py::
#: BALANCING_CALLBACKS`` — keep in sync when bumping sparkrun).
BALANCING_CALLBACKS = {"least-in-flight": "sparkrun_balancer.handler"}

#: routing_strategy values that look right but are no-ops on the pinned
#: LiteLLM — the check names them so the operator is not left believing
#: traffic is load-balanced when it is pinned to the first backend.
BROKEN_STRATEGIES = {
    "least-busy": (
        "least-busy is a no-op on the pinned LiteLLM (in-flight counter "
        "never incremented on the async path — ties go to the first "
        "model_list entry); use proxy.yaml gateways.litellm.balancing: "
        "least-in-flight instead"
    ),
}

PROXY_CONFIG_PATH = "~/.config/sparkrun/proxy.yaml"
LITELLM_CONFIG_PATH = "~/.cache/sparkrun/proxy/litellm_config.yaml"


def _load_yaml(path):
    """Return (doc, error). doc is a dict, or None with an error string.

    error starts with "absent:" when the file simply is not there, "unreadable:"
    for I/O or parse failures, and "no-yaml:" when PyYAML is unavailable — the
    caller maps these onto skip/unverified so a missing PyYAML in some
    interpreter is never reported as cluster drift.
    """
    import os
    try:
        import yaml
    except ImportError:
        return None, "no-yaml: PyYAML unavailable"
    try:
        with open(os.path.expanduser(path)) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        return None, "absent:"
    except OSError:
        return None, "unreadable:"
    except Exception:  # yaml.YAMLError and anything a corrupt doc can raise
        return None, "unreadable:"
    return (doc if isinstance(doc, dict) else {}), None


def configured_intent(proxy_doc):
    """The (router_settings, balancing) sparkrun WOULD emit from a proxy.yaml doc.

    Mirrors sparkrun exactly: router_settings only when the top-level value is
    a mapping; balancing only when it is a value sparkrun implements —
    anything else sparkrun warns-and-ignores, so it must not be demanded
    here. ``balancing_requested`` keeps the raw value for the warn text.
    """
    raw = proxy_doc.get("router_settings")
    # sparkrun (ProxyConfig.router_settings) ignores a non-mapping value
    # entirely — mirror that here, but KEEP the raw value so the caller can
    # warn that an expressed intent was silently dropped.
    rs = dict(raw) if isinstance(raw, dict) else {}
    gateways = proxy_doc.get("gateways")
    litellm = gateways.get("litellm") if isinstance(gateways, dict) else None
    balancing = litellm.get("balancing") if isinstance(litellm, dict) else None
    return {
        "router_settings": rs,
        "raw_router_settings": raw,
        "balancing_requested": balancing,
        "callback": BALANCING_CALLBACKS.get(balancing or ""),
    }


def emitted_state(litellm_doc):
    """The (router_settings, callbacks) actually present in litellm_config.yaml."""
    rs = litellm_doc.get("router_settings")
    rs = dict(rs) if isinstance(rs, dict) else {}
    settings = litellm_doc.get("litellm_settings")
    callbacks = settings.get("callbacks") if isinstance(settings, dict) else None
    return {
        "router_settings": rs,
        "callbacks": list(callbacks) if isinstance(callbacks, list) else [],
    }


def router_drift(proxy_config=None, litellm_config=None):
    """Compare the two live files. Returns {status, detail, warn}.

    status: "ok" | "warn" | "skip" | "unverified" (never "fail" — operator
    rule: this stream warns, it does not alarm). ``warn`` is the boolean the
    daemon proxy stream records. Paths default to sparkrun's own locations.
    """
    proxy_path = proxy_config or PROXY_CONFIG_PATH
    litellm_path = litellm_config or LITELLM_CONFIG_PATH

    proxy_doc, perr = _load_yaml(proxy_path)
    if perr == "absent:":
        # No proxy.yaml: sparkrun proxy settings were never configured here —
        # nothing to match, nothing to warn about.
        return {"status": "skip", "warn": False,
                "detail": f"no proxy.yaml at {proxy_path} — check not applicable"}
    if perr:
        return {"status": "unverified", "warn": False,
                "detail": f"could not read {proxy_path} ({perr})"}

    litellm_doc, lerr = _load_yaml(litellm_path)
    if lerr == "absent:":
        return {"status": "warn", "warn": True,
                "detail": (f"WARN: {proxy_path} configures router settings but "
                           f"{litellm_path} is absent — the proxy was never "
                           "(re)generated since; settings are not live")}
    if lerr:
        return {"status": "unverified", "warn": False,
                "detail": f"could not read {litellm_path} ({lerr})"}

    want = configured_intent(proxy_doc)
    got = emitted_state(litellm_doc)

    # A top-level router_settings that is not a mapping (e.g. the bare string
    # "least-busy") is silently dropped by sparkrun — expressed intent nobody
    # acts on, which is exactly the drift class this check exists for.
    raw_rs = want["raw_router_settings"]
    non_mapping_rs = raw_rs is not None and not isinstance(raw_rs, dict)

    # An explicitly-configured proxy.yaml with NOTHING configured in it and
    # nothing emitted is consistent (defaults) — say so instead of a vacuous ok.
    if (not want["router_settings"] and not want["balancing_requested"]
            and not non_mapping_rs):
        if not got["router_settings"] and not got["callbacks"]:
            return {"status": "skip", "warn": False,
                    "detail": (f"{proxy_path} sets no router_settings/balancing "
                               f"and {litellm_path} emits none")}

    problems = []
    if non_mapping_rs:
        problems.append(
            f"router_settings in {proxy_path} is {raw_rs!r}, not a mapping — "
            f"sparkrun ignores it and emits nothing into {litellm_path}")
    if want["router_settings"] != got["router_settings"]:
        problems.append(
            f"router_settings diverged: {proxy_path} wants {want['router_settings']!r}, "
            f"{litellm_path} carries {got['router_settings']!r}")
    if want["callback"] and want["callback"] not in got["callbacks"]:
        problems.append(
            f"balancing {want['balancing_requested']!r} requested in {proxy_path} but "
            f"{litellm_path} has no {want['callback']!r} in litellm_settings.callbacks "
            "— balancer not live (fork branch lost on a bump? proxy not restarted "
            "after a router_settings change?)")
    strategy = want["router_settings"].get("routing_strategy")
    broken = BROKEN_STRATEGIES.get(strategy if isinstance(strategy, str) else "")
    if broken:
        problems.append(f"routing_strategy: {broken}")
    if (want["balancing_requested"] is not None
            and want["balancing_requested"] not in BALANCING_CALLBACKS):
        problems.append(
            f"gateways.litellm.balancing: {want['balancing_requested']!r} is not "
            f"implemented by sparkrun (knows {sorted(BALANCING_CALLBACKS)}) — "
            f"sparkrun ignores it; {litellm_path} emits no balancer")

    if problems:
        return {"status": "warn", "warn": True,
                "detail": "WARN: " + "; ".join(problems)}
    return {"status": "ok", "warn": False,
            "detail": (f"{litellm_path} carries {proxy_path}'s router_settings "
                       f"({want['router_settings'] or 'none'}"
                       f"{', balancer ' + want['callback'] if want['callback'] else ''})")}
