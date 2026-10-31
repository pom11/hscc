"""Media (image/video) proxy registration contract — mode tags, in ONE place.

THE DECISION (t_966db5b3, card: "autodiscovery vs explicit model_list entry
emitted by hscc apply — decide + implement"): **autodiscovery.** hscc does NOT
and MUST NOT emit model_list entries into ``~/.config/sparkrun/proxy.yaml``.
Evidence read off the live build (sparkrun 0.4.2, editable install resolved
from the ``sparkrun`` CLI shebang — the interpreter the operator actually
runs), file:line as of commit 39f64273 of the fork checkout:

* ``build_litellm_config`` (``plugins/litellm/engine.py:102``) builds every
  ``model_list`` entry from ``DiscoveredEndpoint`` objects (``:148-181``) plus
  the proxy.yaml ``aliases:`` expansion (``:185-209``). proxy.yaml itself has
  NO ``model_list`` region: ``ProxyConfig`` reads only ``proxy.*``
  (``proxy/config.py:166-268``), ``gateways.<name>.*`` (read-only,
  ``:293-307``), ``aliases`` (``:311-333``) and ``default_recipes`` (``:340``).
  A ``model_list:`` region hand-merged into proxy.yaml would be read by NOBODY.
* Worse, it would be read by the wrong party: ``hscc_daemon/
  declaration_agreement.py:498-505`` treats a proxy.yaml ``model_list`` region
  as drift ("who wrote this?") precisely because §2.3 gives apply exactly two
  proxy.yaml regions and NO MORE. Emitting media entries there would convert
  every future ``proxy_declaration`` leg into a false alarm.
* The alias expansion path (the one proxy.yaml region that DOES reach
  ``model_list`` today) cannot carry a mode either: alias entries are emitted
  with ``litellm_params`` only, no ``model_info`` at all (``engine.py:200-209``).

Consequence: the concrete media model + its alias already reach the proxy the
same way LLM units do (``--served-model-name "<concrete> <alias>"`` ->
SGLang's ``/v1/models`` -> autodiscovery), exactly as M0 §3 step 1-2 predicts.
The one thing NO hscc-side code can deliver is the ``model_info.mode`` tag —
``DiscoveredEndpoint`` (``proxy/discovery.py:46-76``) captures only model ids
and ``max_model_len`` from ``/v1/models``; ``task_type``/``supported_task_types``
exist nowhere in sparkrun. That half belongs in the fork (M0 §3 "smallest
sparkrun addition") and is proposed verbatim in this card's PR body; hscc must
not patch sparkrun (operator's fork — card boundary).

What lives HERE is the contract every consumer of that tag shares, so the
mapping exists exactly once:

* M4 (``kind: image|video`` on ModelIntent) validates declared kind against
  the recipe's advertised task types through :func:`mode_from_task_types` and
  builds expected entries with :func:`media_model_list_entry`.
* M5 (ComfyUI node pack) and M6 (Hermes recipes) filter models by mode; the
  enum values they filter on come from :data:`KIND_TO_MODE`, not from a second
  spelling in each pack.
* A follow-up doctor leg (separate card — hscc_daemon is not this card's file
  set) will probe a live unit's ``/v1/models`` with :func:`advertised_media_models`
  and WARN when the advertised task types disagree with the declared kind,
  the same pure-read WARN doctrine as ``hscc_daemon/proxy_router.py``.

The entry shape produced here is byte-compatible with what sparkrun's
``build_litellm_config`` emits today plus the one ``model_info.mode`` key the
fork patch adds — pinned by tests, so when the patch lands the hscc-side
expectation and the fork's emission are the same object, not two dialects.

No network, no sparkrun import, stdlib only (this module is consulted offline
by validate/doctor/tests on the Mac).
"""

# ── The two vocabularies, and the ONE mapping between them ──────────────────
#
# ``kind`` is hscc's word (M0 §4: image|video on ModelIntent). ``mode`` is
# LiteLLM's word: ModelInfo.mode (litellm/types/router.py), which 1.82.6's
# router reads for capability routing and which /v1/models-style consumers
# filter on. The values below are LiteLLM's own enum strings — NOT invented.

#: hscc unit kind -> LiteLLM ``model_info.mode``. The only legal spelling pair.
KIND_TO_MODE = {
    "image": "image_generation",
    "video": "video_generation",
}

#: The mode values media may carry, in stable order (M5's dropdown filters on
#: exactly this set; nothing else may invent a third spelling).
MEDIA_MODES = tuple(KIND_TO_MODE.values())


class MediaRegistrationError(ValueError):
    """A declared unit cannot produce a coherent media model_list entry.

    Raised by :func:`media_model_list_entry` when kind and advertised task
    types disagree, or when no media mode can be decided — an entry without a
    mode is exactly the "proxy listed it under chat" lie M0 §3 refuses to
    ship, so it is refused at build time, not warned about later.
    """


def mode_for_kind(kind):
    """``image``/``video`` -> LiteLLM mode; unknown kind -> ``None``.

    Returns None (does not raise) because callers validate kinds with their
    own vocabulary errors first (M4 refuses an unknown ``kind:`` at parse with
    the template's path in the message). This function's job is only the pair
    mapping.
    """
    if not isinstance(kind, str):
        return None
    return KIND_TO_MODE.get(kind.strip().lower())


def mode_from_task_types(*tokens, primary=None):
    """Derive the LiteLLM mode from a unit's advertised task-type tokens.

    Mapping (verbatim from M0 §3, the same rule the sparkrun patch will
    implement): ``*2I`` / ``i2i`` -> ``image_generation``; ``*2V`` / ``i2v`` /
    ``t2v`` -> ``video_generation``. Matching is case-insensitive; the enum
    strings themselves (``image_generation`` / ``video_generation``) are
    accepted unchanged so the function round-trips its own output.

    ``primary`` is the single advertised ``task_type`` (SGLang's
    ``/v1/models`` entry carries one); when it maps, it wins outright.
    Otherwise every token is classified and they must agree — a unit that
    advertises both image and video task types is genuinely ambiguous for a
    per-deployment mode tag, so ``None`` is returned (fail-closed: the caller
    declares ``kind`` explicitly rather than us guessing ``chat``, which is
    the wrong-default that motivated the whole mode tag).
    """

    def classify(tok):
        if not isinstance(tok, str):
            return None
        t = tok.strip().upper()
        if not t:
            return None
        if t == "IMAGE_GENERATION" or t == "IMAGE":
            return "image_generation"
        if t == "VIDEO_GENERATION" or t == "VIDEO":
            return "video_generation"
        # The M0 §3 shapes: anything ending 2I (T2I, I2I) is image-side;
        # anything ending 2V (T2V, I2V) is video-side.
        if t.endswith("2I"):
            return "image_generation"
        if t.endswith("2V"):
            return "video_generation"
        return None

    pmode = classify(primary)
    if pmode is not None:
        return pmode
    kinds = {c for c in (classify(tok) for tok in tokens) if c is not None}
    if len(kinds) == 1:
        return kinds.pop()
    return None  # zero mapped (unknown enums) or mixed (ambiguous) — never guess


def advertised_media_models(models_payload):
    """Parse a backend's ``GET /v1/models`` body into media registrations.

    ``models_payload`` is the decoded OpenAI-style body. SGLang Diffusion's
    entries are OpenAI entries PLUS ``task_type`` / ``supported_task_types`` /
    precision fields (M0 §2 — the mode-tagged ``/v1/models`` the contract asked
    for); a chat backend advertises no task fields and is skipped.

    Returns a list of ``{"model_name", "mode", "task_types"}`` for entries that
    advertise task fields, ``mode`` possibly ``None`` when the advertised
    enums are unknown or mixed (never defaulted to chat). Callers who want
    only cleanly-tagged models filter on ``mode is not None``.
    """
    out = []
    if not isinstance(models_payload, dict):
        return out
    for entry in models_payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("model")
        if not isinstance(name, str) or not name.strip():
            continue
        task_types = [t for t in (entry.get("supported_task_types") or [])
                      if isinstance(t, str) and t.strip()]
        primary = entry.get("task_type")
        if not task_types and not isinstance(primary, str):
            continue  # no task vocabulary at all: a chat entry, not ours
        out.append({
            "model_name": name.strip(),
            "mode": mode_from_task_types(*task_types, primary=primary),
            "task_types": task_types or ([primary] if primary else []),
        })
    return out


def media_model_list_entry(model_name, api_base, *, kind=None, task_types=None,
                           max_input_tokens=None, api_key=None):
    """One ``model_list`` entry for a media unit, in sparkrun's own shape.

    Byte-compatible with ``build_litellm_config``'s output (engine.py:165-181)
    plus the ``model_info.mode`` key the proposed fork patch adds:

        {"model_name": <name>,
         "litellm_params": {"model": "openai/<name>",
                            "api_base": "http://<host>:<port>/v1",
                            "api_key": <key or "not-needed">},
         "model_info": {"mode": "image_generation"|"video_generation"}}

    ``kind`` (hscc vocabulary) is the declared intent and wins when given;
    ``task_types`` (the backend's advertised enums) is the discovery-side
    input. When both are given they MUST agree — disagreement is a template
    bug (a ``kind: video`` unit advertising T2I will 400 every client) and
    raises :class:`MediaRegistrationError` naming both values.

    ``api_base`` accepts ``host:port`` (rendered the sparkrun way,
    ``http://host:port/v1``) or an explicit ``http(s)://`` URL passed through.

    No decidable mode -> raise. That is the whole point of the module: an
    entry without a mode is indistinguishable from a chat model to LiteLLM's
    capability routing, and M0 §3 rules that lie out.
    """
    from_kind = mode_for_kind(kind) if kind is not None else None
    from_types = mode_from_task_types(*task_types) if task_types else None
    if from_kind is not None and from_types is not None and from_kind != from_types:
        raise MediaRegistrationError(
            f"{model_name}: declared kind {kind!r} (mode {from_kind}) contradicts "
            f"advertised task types {list(task_types or [])} (mode {from_types}) — "
            f"fix the template kind or the recipe, not this entry")
    mode = from_kind or from_types
    if mode is None:
        raise MediaRegistrationError(
            f"{model_name}: no media mode derivable from kind={kind!r} "
            f"task_types={list(task_types or [])} — refusing to emit an entry "
            f"that would read as a chat model")
    if not isinstance(model_name, str) or not model_name.strip():
        raise MediaRegistrationError("media entry needs a model_name")

    base = api_base if isinstance(api_base, str) else ""
    if base.startswith("http://") or base.startswith("https://"):
        url = base
    elif isinstance(base, str) and ":" in base:
        url = f"http://{base}/v1"
    else:
        raise MediaRegistrationError(
            f"{model_name}: api_base must be host:port or an http(s) URL, got {api_base!r}")

    entry = {
        "model_name": model_name.strip(),
        "litellm_params": {
            "model": f"openai/{model_name.strip()}",
            "api_base": url,
            "api_key": api_key or "not-needed",
        },
        "model_info": {"mode": mode},
    }
    # Media units report max_model_len null (M0 §3 cycle-1 note); the key is
    # present only when a real window is known, mirroring sparkrun's
    # `if ep.max_model_len:` gate so a diff against the fork's output stays
    # empty for chat-shaped data.
    if isinstance(max_input_tokens, int) and not isinstance(max_input_tokens, bool) \
            and max_input_tokens > 0:
        entry["model_info"]["max_input_tokens"] = max_input_tokens
    return entry


def media_entries_for_units(units):
    """Every ``model_list`` entry a set of resolved media units registers.

    ``units`` is any iterable of unit-shaped objects (attributes or dict keys):
    ``model`` (concrete id), ``node`` + ``port`` (the endpoint), optional
    ``alias``, optional ``kind`` (image|video — M4), optional ``task_types``
    (the backend's advertised enums), optional ``max_model_len``. Chat units
    (``kind`` absent/``chat`` with no media task types) are SKIPPED — they are
    autodiscovery's ordinary path, nothing here touches them (§7 byte-identity).

    Each unit advertises ``<concrete> <alias>`` through ``--served-model-name``
    exactly like an LLM unit (M0 §3 step 1), so each advertised name gets one
    entry — same dedupe key sparkrun uses (``name@host:port``).

    Raises :class:`MediaRegistrationError` on the first unit whose mode cannot
    be decided, naming that unit. §3's one-backend-per-alias rule is a
    VALIDATE concern (two units sharing one media alias is a template error);
    this function reports it the same fail-closed way rather than emitting a
    model group LiteLLM would fan out and break video poll routing.
    """
    entries, seen = [], set()
    for u in units or []:
        get = (u.get if isinstance(u, dict)
               else (lambda k, d=None, _o=u: getattr(_o, k, d)))
        name = (get("model") or "").strip()
        kind = get("kind")
        task_types = get("task_types") or []
        if mode_for_kind(kind) is None and not task_types:
            continue  # plain chat unit — zero behaviour change (§7)
        node, port = get("node"), get("port")
        if not node or port is None:
            raise MediaRegistrationError(
                f"{name}: media unit needs node+port to register an api_base")
        base = f"{node}:{port}"
        for n in [x for x in (name, (get("alias") or "").strip()) if x]:
            dedup = f"{n}@{base}"
            if dedup in seen:
                continue
            seen.add(dedup)
            entries.append(media_model_list_entry(
                n, base, kind=kind, task_types=task_types,
                max_input_tokens=get("max_model_len")))
    return entries
