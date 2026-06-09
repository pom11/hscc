"""JSON schemas for hscc-cluster tools."""

CLUSTER_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

LIST_RECIPES_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
PICK_NODE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

PROVISION_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "recipe": {"type": "string", "description": "sparkrun recipe name"},
        "node": {"type": "string", "description": "worker IP or 'auto'", "default": "auto"},
        "confirm": {"type": "boolean", "description": "must be true to execute", "default": False},
    },
    "required": ["recipe"], "additionalProperties": False,
}
STOP_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "recipe": {"type": "string"},
        "node": {"type": "string"},
        "confirm": {"type": "boolean", "default": False},
        "force": {"type": "boolean", "default": False},
    },
    "required": ["recipe", "node"], "additionalProperties": False,
}
MODEL_HEALTH_SCHEMA = {
    "type": "object",
    "properties": {"node": {"type": "string", "description": "IP, default head .244"}},
    "additionalProperties": False,
}

_NODE_ONLY = {"type": "object", "properties": {"node": {"type": "string"}}, "additionalProperties": False}
VLLM_LOGS_SCHEMA = _NODE_ONLY
NODE_DIAGNOSTICS_SCHEMA = _NODE_ONLY
NAS_DIAGNOSE_SCHEMA = _NODE_ONLY

RESTART_MODEL_SCHEMA = {"type": "object", "properties": {
    "recipe": {"type": "string"}, "node": {"type": "string"},
    "confirm": {"type": "boolean", "default": False},
    "force": {"type": "boolean", "default": False}},
    "required": ["recipe", "node"], "additionalProperties": False}
REMOUNT_NAS_SCHEMA = {"type": "object", "properties": {
    "node": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
    "required": ["node"], "additionalProperties": False}
REPAIR_NAS_EXPORT_SCHEMA = {"type": "object", "properties": {
    "confirm": {"type": "boolean", "default": False}}, "additionalProperties": False}
REAP_ORPHANS_SCHEMA = {"type": "object", "properties": {
    "confirm": {"type": "boolean", "default": False}}, "additionalProperties": False}
