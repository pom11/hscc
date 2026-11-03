"""The doctor feature-row registry (t_32a06db9).

Why this module exists
----------------------
``run_doctor()`` in ``hscc-bootstrap/doctor.py`` had become a merge hotspot: every
feature that wanted a doctor row edited the SAME three hunks — a row adapter
appended after the previous one, a seam keyword appended to ``run_doctor``'s
signature, and one entry (plus its "why this row exists" comment) appended to the
``checks`` list. Two features touching doctor therefore collided in all three.
PR #175 vs #173 did exactly that and needed a rebase card (t_3f805c4f) to resolve.

This module moves the *shape* of a feature row out of ``run_doctor``'s body:

* a row is a :class:`FeatureCheckRow` record — its key, the seam keyword tests use,
  and the names of the three hooks that produce its verdict (loader / refusal /
  fallback check name);
* ``run_doctor()`` iterates the registry instead of carrying one call and one
  comment block per feature, and takes the seams through ``**feature_rows``
  instead of one keyword each, so the signature never grows again;
* a NEW feature writes its loader/refusal in its own ``doctor_<feature>.py``
  (the precedent is already in the tree: ``doctor_supervision.py``,
  ``doctor_notifier_mask.py``) and contributes exactly ONE registry entry.

The hooks are named as STRINGS and resolved against the doctor module at call
time (:func:`resolve_hook`). Two reasons, both load-bearing:

1. no import cycle — doctor imports this module, so this module must not import
   doctor at module level;
2. the row tests patch the hooks on the doctor module object
   (``monkeypatch.setattr(doctor, "_load_orch_waker", ...)``) and they patch the
   *row function itself* (``test_doctor_kanban_enforce.py`` pins the
   ``hscc_dir``/seam forwarding at the call site). A registry that captured
   function objects at import time would make those patches invisible, so
   resolution has to happen per call.

Semantics deliberately unchanged: the three-source trust order (injected seam →
in-suite refusal → real check), the fence (an uninjected check inside pytest is an
ERROR naming the seam, never a silent pass and never a read of the operator's
``~/.hscc``), the UNVERIFIABLE row for a feature module that cannot be imported,
and the five-field ``Check`` adapter that keeps ``doctor --json`` one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

#: The five fields every doctor row exposes. ``doctor --json`` and the ○/●
#: renderer key off exactly these, so the adapter below drops anything else a
#: feature's ``doctor_check()`` may return.
CHECK_FIELDS = ("name", "ok", "detail", "fix", "fatal")


def adapt(res: dict, fallback_name: Optional[str] = None) -> "tuple":
    """The five ``Check`` fields from a feature verdict dict.

    Shared by every feature row (and by the fleet checks in doctor.py) so a new
    row cannot invent a sixth field or drop one: the row tests pin the field set
    at the bridge, and this is the single place it is now defined.

    Returns the five values as a tuple in CHECK_FIELDS order — the caller builds
    its ``Check`` with them, because ``Check`` is declared in doctor.py and this
    module must not import doctor (that would be a cycle).
    """
    name = res.get("name") or fallback_name
    return (name, res["ok"], res.get("detail", ""), res.get("fix", ""),
            res.get("fatal", False))


@dataclass(frozen=True)
class FeatureCheckRow:
    """One feature's row in the doctor check list.

    key
        Stable identifier for the row (used in the unknown-seam error message
        and in tests). Not the printed name — the printed name comes from the
        feature's own verdict dict.
    seam_kwarg
        The keyword a test passes to ``run_doctor()`` to inject the verdict,
        e.g. ``_orch_waker_check``. Registry-owned: a new row never touches
        ``run_doctor``'s signature.
    row_fn
        Name of the row function on the doctor module to call, or "" to let
        :func:`build_row` assemble the row from loader/refusal/fallback_name.
        The five rows that predate this registry keep their own function (their
        tests call it directly and pin the five-field copy at the bridge), so
        this field is the compatibility path — new rows should leave it empty.
    row_kwargs
        Which per-row context run_doctor() must forward to the row: "hscc_dir"
        (the sandboxed ~/.hscc root — the doctor_notifier_mask convention the
        daemon-state rows read) and/or "hermes_home" (the deployed plugin-tree
        root — what the budget-hook row reads). Defaults to ("hscc_dir",).
    loader
        Name of the doctor-module hook returning the feature's module (or None
        when the module is not deployed next to bootstrap). Ignored when
        ``row_fn`` is set.
    refusal
        Name of the doctor-module hook returning the in-suite refusal verdict
        dict. Ignored when ``row_fn`` is set.
    fallback_name
        Name of the doctor-module constant holding the check name to print when
        the feature module cannot be imported at all — a missing check must
        never render as absence-of-output. Ignored when ``row_fn`` is set.
    missing_detail / missing_fix
        The UNVERIFIABLE row's text when the loader returns None.
    """

    key: str
    seam_kwarg: str
    row_fn: str = ""
    row_kwargs: tuple = ("hscc_dir",)
    loader: str = ""
    refusal: str = ""
    fallback_name: str = ""
    missing_detail: str = ""
    missing_fix: str = ""

    def __post_init__(self) -> None:
        if not self.key or not self.seam_kwarg:
            raise ValueError("FeatureCheckRow needs a key and a seam_kwarg")
        if not self.row_fn and not (self.loader and self.refusal
                                    and self.fallback_name):
            raise ValueError(
                f"FeatureCheckRow {self.key!r}: either row_fn, or loader + "
                f"refusal + fallback_name must be set")


def resolve_hook(module, name: str) -> Callable:
    """Look a hook up on ``module`` at call time.

    Per-call on purpose: the row tests patch these names on the doctor module
    (see the module docstring), so a captured-at-import reference would make
    their patches no-ops.
    """
    hook = getattr(module, name)
    if not callable(hook):
        raise TypeError(f"{module.__name__}.{name} is not callable")
    return hook


def build_row(doctor_module, spec: FeatureCheckRow, injected, **context):
    """Build one feature row's ``Check`` from its registry entry.

    ``context`` carries the per-row context run_doctor() forwards — only the
    names in ``spec.row_kwargs`` are passed (see :func:`render_rows`):
    ``hscc_dir`` (the sandboxed ~/.hscc root) and/or ``hermes_home`` (the
    deployed plugin-tree root the budget-hook row reads).

    Three sources, in the same order of trust every feature row has always used:

    1. ``injected`` — a test's fixture-backed callable (the seam);
    2. the in-suite refusal — inside a pytest process a row whose verdict reads
       operator state MUST be injected, so without a seam it renders as an
       ERROR naming the missing seam (never a silent pass, never a real read);
    3. the feature's own ``doctor_check(...)`` — a human CLI run.

    A feature module that cannot be imported at all reports itself UNVERIFIABLE
    (ok=False, fatal=False) rather than vanishing from the list.
    """
    if spec.row_fn:
        # Compatibility path: the row keeps its own function on doctor.py, which
        # its own suite calls directly. Resolved per call so a test can swap it.
        row_fn = resolve_hook(doctor_module, spec.row_fn)
        return row_fn(injected, **context)

    check_cls = doctor_module.Check
    if injected is not None:
        res = injected()
    elif "pytest" in doctor_module.sys.modules:
        res = resolve_hook(doctor_module, spec.refusal)()
    else:
        feature = resolve_hook(doctor_module, spec.loader)()
        if feature is None:
            return check_cls(
                getattr(doctor_module, spec.fallback_name), False,
                detail=spec.missing_detail, fix=spec.missing_fix, fatal=False)
        res = feature.doctor_check(**context)
    # ``fallback_name`` is the NAME OF A MODULE CONSTANT (the same convention as
    # ``_BUDGET_HOOK_CHECK_NAME``), so resolve it here rather than hand the
    # attribute name to adapt() — otherwise a verdict dict without a name would
    # print the literal string "_PROBE_CHECK_NAME" to a human.
    fallback = getattr(doctor_module, spec.fallback_name, None) \
        if spec.fallback_name else None
    name, ok, detail, fix, fatal = adapt(res, fallback)
    return check_cls(name=name, ok=ok, detail=detail, fix=fix, fatal=fatal)


def seam_names(specs: List[FeatureCheckRow]) -> frozenset:
    """The seam keywords the registry owns — doctor validates ``**feature_rows``
    against this so a typo'd seam cannot silently downgrade a row to a refusal."""
    return frozenset(s.seam_kwarg for s in specs)


def render_rows(doctor_module, specs: List[FeatureCheckRow], seams: dict,
                **context) -> List:
    """Build every registered row, in registry order.

    ``seams`` is the keyword mapping a caller passed to ``run_doctor()``; a row
    whose seam is absent gets None and applies its own fence. ``context`` is the
    per-row context run_doctor() has available; each row receives only the names
    it declares in ``row_kwargs``, so a row can never receive a kwarg its
    function does not accept.
    """
    rows = []
    for spec in specs:
        row_ctx = {k: v for k, v in context.items() if k in spec.row_kwargs}
        rows.append(build_row(doctor_module, spec, seams.get(spec.seam_kwarg),
                              **row_ctx))
    return rows
