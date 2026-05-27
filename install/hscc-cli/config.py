#!/usr/bin/env python3
"""
HSCC Config Parser — YAML config reader and writer.
"""

import os
import re
from pathlib import Path


HSCC_DIR = Path.home() / ".hscc"
CONFIG_PATH = HSCC_DIR / "config.yaml"


def load_config(path=None):
    """Load HSCC config from YAML file."""
    if path is None:
        path = CONFIG_PATH
    
    if not path.exists():
        return None
    
    content = path.read_text()
    return parse_yaml(content)


def save_config(config, path=None):
    """Save HSCC config to YAML file."""
    if path is None:
        path = CONFIG_PATH
    
    path.write_text(dump_yaml(config))
    print(f"  ✓ Config saved to {path}")


def parse_yaml(content):
    """Simple YAML parser for HSCC config."""
    config = {}
    current_section = None
    
    for line in content.split("\n"):
        # Skip empty lines and comments
        if not line.strip() or line.strip().startswith("#"):
            continue
        
        # Check indentation level
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        
        if indent == 0 and ":" in stripped:
            # Top-level key
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            
            if value:
                config[key] = parse_value(value)
            else:
                config[key] = {}
                current_section = key
        elif current_section and ":" in stripped:
            # Nested key
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            
            if isinstance(config.get(current_section), dict):
                config[current_section][key] = parse_value(value)
    
    return config


def dump_yaml(config):
    """Simple YAML dumper for HSCC config."""
    lines = []
    for key, value in config.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for subkey, subvalue in value.items():
                if isinstance(subvalue, list):
                    lines.append(f"  {subkey}: [{', '.join(str(v) for v in subvalue)}]")
                else:
                    lines.append(f"  {subkey}: {subvalue}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def parse_value(value):
    """Parse a YAML value string into Python type."""
    if not value:
        return None
    
    # Remove quotes
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    
    # Boolean
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    
    # Integer
    try:
        return int(value)
    except ValueError:
        pass
    
    # Float
    try:
        return float(value)
    except ValueError:
        pass
    
    # String
    return value


def get_config_value(key, default=None):
    """Get a specific config value."""
    config = load_config()
    if config is None:
        return default
    
    # Handle nested keys like "cluster.primary"
    parts = key.split(".")
    value = config
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default
    
    return value
