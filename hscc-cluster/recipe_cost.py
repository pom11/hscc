"""Parse sparkrun's per-recipe VRAM cost for template auto-fit placement (D12).

`sparkrun show <recipe>` emits an authoritative VRAM Estimation block (model
weights, KV cache, per-GPU total, "DGX Spark fit: YES/NO", usable memory). HSCC
parses it to decide what fits where — never proposing an OOM layout. There is no
`--json` on `sparkrun show`, so we text-parse (like detect.recipe_model).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict

SPARKRUN = "sparkrun"


@dataclass
class RecipeCost:
    recipe: str
    weights_gb: Optional[float] = None
    kv_gb: Optional[float] = None
    per_gpu_total_gb: Optional[float] = None
    usable_gb: Optional[float] = None
    tensor_parallel: int = 1
    fits: Optional[bool] = None  # sparkrun's "DGX Spark fit"
    raw_ok: bool = False         # did we parse anything?


def _f(text: str, pattern: str) -> Optional[float]:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, TypeError):
        return None


def parse_show(text: str, recipe: str = "") -> RecipeCost:
    """Parse the VRAM Estimation block out of `sparkrun show` output."""
    c = RecipeCost(recipe=recipe)
    c.weights_gb = _f(text, r"Model weights:\s*([\d.]+)\s*GB")
    c.kv_gb = _f(text, r"KV cache:\s*([\d.]+)\s*GB")
    c.per_gpu_total_gb = _f(text, r"Per-GPU total:\s*([\d.]+)\s*GB")
    c.usable_gb = _f(text, r"Usable GPU memory:\s*([\d.]+)\s*GB")
    tp = _f(text, r"Tensor parallel:\s*(\d+)")
    c.tensor_parallel = int(tp) if tp else 1
    fit = re.search(r"DGX Spark fit:\s*(YES|NO)", text, re.IGNORECASE)
    c.fits = (fit.group(1).upper() == "YES") if fit else None
    c.raw_ok = c.per_gpu_total_gb is not None
    return c


# small mtime-keyed cache so repeated planning is cheap
_CACHE: Dict[str, tuple] = {}

# existence answers for non-path (registry) tokens; a registry recipe does not
# have an mtime to key on, and preflight asks about the same handful repeatedly.
_EXISTS_CACHE: Dict[str, bool] = {}


def recipe_cost(recipe: str, *, _runner=None) -> RecipeCost:
    """Return the parsed cost for a recipe (by name or path). Best-effort:
    RecipeCost(raw_ok=False) when sparkrun show fails."""
    key = recipe
    expanded = os.path.expanduser(recipe)
    mtime = os.path.getmtime(expanded) if os.path.isfile(expanded) else 0
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    runner = _runner or _run_show
    text = runner(recipe)
    cost = parse_show(text or "", recipe=recipe)
    _CACHE[key] = (mtime, cost)
    return cost


# A recipe is a registry name (@reg/name), a bare name, or a filesystem path
# (absolute /…, home ~/…, or relative). Must start with one of @ ~ / or a word
# char — never '-' — so a value can't smuggle a flag into sparkrun. The `--`
# sentinel in _run_show is the real guard; this just rejects obvious junk.
_RECIPE_RE = re.compile(r"^[@~/\w][\w./:@~+-]*$")


def _run_show(recipe: str) -> str:
    # sparkrun show accepts a recipe name or path; pass through as given.
    if not _RECIPE_RE.match(recipe or ""):
        return ""  # invalid/suspicious recipe token — don't shell out
    try:
        # `--` end-of-options sentinel: everything after is positional, so a
        # recipe like "--foo" can't be parsed as a flag (argv injection).
        r = subprocess.run([SPARKRUN, "show", "--", recipe],
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _run_show_norvam(recipe: str) -> str:
    """`sparkrun show --no-vram` — resolution only, no VRAM estimation.

    Existence does not need the VRAM block, and skipping it matters: the VRAM
    estimator performs HuggingFace Hub metadata lookups, which are slow and
    require the network. `_structural_validate` documents itself as OFFLINE, so
    its existence check must not reach the internet — `--no-vram` keeps the
    probe to sparkrun's local registry cache.
    """
    if not _RECIPE_RE.match(recipe or ""):
        return ""  # invalid/suspicious recipe token — don't shell out
    try:
        r = subprocess.run([SPARKRUN, "show", "--no-vram", "--", recipe],
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def recipe_exists(recipe: str, *, _runner=None) -> bool:
    """True when `recipe` names something sparkrun can actually serve.

    A recipe token is a filesystem path (``~/…``, ``/…``, ``./…``) OR a registry
    name (``@reg/name``, or a bare name resolved across enabled registries). A
    plain ``Path.is_file()`` check answers only the first kind and reports every
    registry recipe as missing — which is why this exists: templates that source
    from the community/eugr/atlas/official registries are otherwise rejected at
    preflight even though `sparkrun show` resolves them fine.

    Path-shaped tokens are answered from the filesystem (no subprocess). Anything
    else is probed with `sparkrun show`, whose output is cached by
    ``recipe_cost``'s own cache via ``_run_show``. Fail-safe direction is
    deliberate: an unreachable/broken sparkrun makes this return False, so
    preflight refuses rather than proposing a layout it cannot launch.
    """
    if not recipe:
        return False
    # Registry tokens start with '@' and are never filesystem paths.
    if not recipe.startswith("@"):
        # Path check via pathlib, matching the original is_file() behaviour this
        # replaced (tests monkeypatch Path.is_file).
        if Path(os.path.expanduser(recipe)).is_file():
            return True
        # Path- or filename-shaped and not on disk: genuinely missing. Don't
        # burn a subprocess asking sparkrun about it. Only an extension-less
        # bare token can still be a registry name worth probing.
        if (recipe.startswith(("~", "/", "./", "../"))
                or "/" in recipe
                or recipe.endswith((".yaml", ".yml"))):
            return False
    cached = _EXISTS_CACHE.get(recipe)
    if cached is not None:
        return cached
    runner = _runner or _run_show_norvam
    ok = bool((runner(recipe) or "").strip())
    _EXISTS_CACHE[recipe] = ok
    return ok


# ── placement ───────────────────────────────────────────────────────────────

@dataclass
class Placement:
    node_ip: str
    recipe: str
    port: int
    per_gpu_total_gb: Optional[float]


def plan_placement(models: List[dict], nodes: List[dict], *,
                   base_port: int = 8000, _coster=None) -> dict:
    """Assign models to nodes by real VRAM cost (D12).

    models: [{"recipe": str, "tp": int}], nodes: [{"ip", "vram_free_gb"|None}].
    Greedy first-fit: place each model on the first node whose remaining free
    VRAM covers its per_gpu_total. tp>1 models occupy a node exclusively (G3);
    co-located (tp=1) models on a node get sequential ports from base_port.

    Returns {"ok", "placements": [Placement...], "errors": [...]}. Refuses
    (errors) rather than proposing an overcommit.
    """
    coster = _coster or recipe_cost
    errors: List[str] = []
    # remaining free vram + next port per node; track exclusivity
    rem = {}
    nextport = {}
    exclusive = set()
    for n in nodes:
        rem[n["ip"]] = n.get("vram_free_gb")
        nextport[n["ip"]] = base_port
    placements: List[Placement] = []

    for m in models:
        recipe = m["recipe"]
        tp = int(m.get("tp", 1))
        cost = coster(recipe)
        need = cost.per_gpu_total_gb
        if cost.fits is False:
            errors.append(f"{recipe}: sparkrun says it does not fit a DGX Spark")
            continue
        placed = False
        for n in nodes:
            ip = n["ip"]
            if ip in exclusive:
                continue
            # a node already hosting a model can't take a tp>1 (needs exclusivity)
            already = any(p.node_ip == ip for p in placements)
            if tp > 1 and already:
                continue
            free = rem[ip]
            if free is not None and need is not None and need > free:
                continue  # won't fit
            # place it
            placements.append(Placement(node_ip=ip, recipe=recipe,
                                        port=nextport[ip],
                                        per_gpu_total_gb=need))
            nextport[ip] += 1
            if free is not None and need is not None:
                rem[ip] = free - need
            if tp > 1:
                exclusive.add(ip)
            placed = True
            break
        if not placed:
            errors.append(
                f"{recipe}: no node with enough free VRAM "
                f"(needs {need} GB; tp={tp})")

    return {"ok": not errors, "placements": placements, "errors": errors}
