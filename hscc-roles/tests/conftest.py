import os
import sys

# Make the plugin modules importable as top-level (mirrors hscc-cluster tests).
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
