import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent  # .../hscc-mcp


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(
        f"hscc_mcp.{mod_name}", _PKG_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[f"hscc_mcp.{mod_name}"] = module
    return module


# Register a synthetic package so `from hscc_mcp import runner` works despite the
# hyphenated directory name (not a valid Python identifier).
if "hscc_mcp" not in sys.modules:
    import types
    pkg = types.ModuleType("hscc_mcp")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hscc_mcp"] = pkg

_load("runner", "runner.py")
_load("tools", "tools.py")
