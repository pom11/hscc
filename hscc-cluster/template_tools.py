"""Template agent tools: list, preview, and apply cluster templates."""

# ── Schemas (OpenAI function-parameters style) ──

LIST_TEMPLATES_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

PREVIEW_TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "description": "Template name to preview",
        },
    },
    "required": ["template"],
    "additionalProperties": False,
}

APPLY_TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "description": "Template name to apply",
        },
        "confirm": {
            "type": "boolean",
            "description": "Must be true to execute; without it returns a preview",
            "default": False,
        },
    },
    "required": ["template"],
    "additionalProperties": False,
}


# ── Handlers ──

def list_templates(args, **kwargs):
    """List available cluster templates (declared fleet layouts)."""
    try:
        ct = _get_cluster_template()
        return ct.list_templates()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def preview_template(args, **kwargs):
    """Dry-run: what applying a template would change against the live cluster (no writes)."""
    tpl = args.get("template")
    if not tpl:
        return {"ok": False, "error": "template name required"}
    try:
        ct = _get_cluster_template()
        return ct.preview_template(tpl)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def apply_template(args, **kwargs):
    """Apply a cluster template — reshapes the ENTIRE fleet (provisions orchestrator + workers). HIGH-RISK. GUARDED: confirm=true to execute; without confirm returns a preview."""
    tpl = args.get("template")
    if not tpl:
        return {"ok": False, "error": "template name required"}
    confirm = bool(args.get("confirm", False))
    try:
        ct = _get_cluster_template()
        return ct.apply_template(tpl, confirm=confirm)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_cluster_template():
    """Lazy-import the cluster_template engine (same plugin)."""
    try:
        from . import cluster_template
    except ImportError:
        import cluster_template  # noqa: F401
    return cluster_template
