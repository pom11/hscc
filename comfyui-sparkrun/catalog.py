"""Model-catalogue parsing: which models may be listed for which node kind.

The sparkrun proxy (LiteLLM) lists every registered unit through
``GET /v1/models``; the media M3 contract
(``hscc-cluster/media_registration.py``) pins the two mode enum strings —
``KIND_TO_MODE = {"image": "image_generation", "video": "video_generation"}``.
This module is the client-side half of that contract: an image node lists
exactly the models whose entry carries ``model_info.mode ==
"image_generation"``, the video node lists ``video_generation``.

Deliberately import-free of its pack siblings (``nodes.py`` loads each sibling
by file location under a unique module name, because ComfyUI does NOT put a
custom_nodes pack on ``sys.path`` — and this pack owns a ``nodes.py``, which
must never shadow ComfyUI's own).

Fail-closed rules — the same doctrine as media_registration, "a model that
reads as chat is not a media model":

* an entry with no ``model_info``, or whose mode is not one of the two media
  modes (notably ``chat``), is SKIPPED for media dropdowns;
* when the proxy carries no usable media mode at all (the honest state before
  the operator applies the M3 fork patch), the caller shows
  :data:`NO_MODELS_SENTINEL` rather than silently listing chat models;
* a model name can still be typed over the combo (a ComfyUI COMBO accepts
  custom text), so the pack stays usable during that transition.
"""

from __future__ import annotations

#: The only two mode spellings media may carry. Mirrors the VALUES of
#: hscc-cluster/media_registration.py::KIND_TO_MODE — the repo test
#: test_mode_enums_match_media_registration binds them so they cannot drift.
IMAGE_MODE = "image_generation"
VIDEO_MODE = "video_generation"
MODES = (IMAGE_MODE, VIDEO_MODE)

#: hscc's unit kind -> the LiteLLM mode its dropdown lists (same pair as the
#: parent contract's KIND_TO_MODE).
KIND_TO_MODE = {"image": IMAGE_MODE, "video": VIDEO_MODE}

#: Shown as the sole dropdown entry when discovery produced no media models —
#: either the proxy is unreachable or nothing is registered for that mode.
#: Selecting it makes the node name the cause instead of firing a doomed
#: request at a model the proxy will refuse.
NO_MODELS_SENTINEL = "(no media models registered on proxy)"

#: Dropdown entry shown when discovery could not reach the proxy at all.
UNREACHABLE_SENTINEL = "(proxy unreachable — check base_url)"


def _entry_name(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    name = entry.get("id") or entry.get("model") or entry.get("name")
    return name.strip() if isinstance(name, str) else ""


def _entry_mode(entry):
    mi = entry.get("model_info")
    if not isinstance(mi, dict):
        return None
    mode = mi.get("mode")
    return mode.strip() if isinstance(mode, str) and mode.strip() else None


def models_for_mode(models_payload, mode: str):
    """Model names from a decoded ``/v1/models`` body whose mode == ``mode``.

    ``models_payload`` is the decoded body (a ``data`` list is unwrapped) or an
    already-unwrapped list of entries. Order preserved; duplicates dropped.
    Entries that carry no media mode — untagged, or ``chat`` — never appear:
    the proxy's own router rejects them for media endpoints, so listing them
    would only produce a confusing 400.
    """
    if mode not in MODES:
        raise ValueError(f"not a media mode: {mode!r} (expected one of {MODES})")
    if isinstance(models_payload, dict):
        entries = models_payload.get("data") or []
    elif isinstance(models_payload, list):
        entries = models_payload
    else:
        entries = []
    out, seen = [], set()
    for entry in entries:
        name = _entry_name(entry)
        if not name:
            continue
        if _entry_mode(entry) != mode:
            continue  # chat, untagged, or a mode we do not speak: not ours
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def modes_of(models_payload):
    """Every mode string the proxy advertised, deduped (diagnostics/tests)."""
    if isinstance(models_payload, dict):
        entries = models_payload.get("data") or []
    elif isinstance(models_payload, list):
        entries = models_payload
    else:
        entries = []
    seen, out = set(), []
    for entry in entries:
        m = _entry_mode(entry) if isinstance(entry, dict) else None
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out
