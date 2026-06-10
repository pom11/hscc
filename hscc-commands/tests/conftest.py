import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_PLUGIN_DIR)
# Import the plugin as a package: `import hscc_commands` (dir is hscc-commands,
# so expose it under an underscore alias on a temp path).
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
