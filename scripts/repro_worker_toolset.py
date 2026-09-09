#!/usr/bin/env python3
"""Reproduce the exact tool-definition computation a dispatched kanban worker
gets, to prove whether `terminal` / `execute_code` are present in the function
set a worker sees. Mirrors model_tools._compute_tool_definitions with the
dispatch environment: HERMES_KANBAN_TASK set + the --toolsets pin the
dispatcher resolves via _resolve_worker_cli_toolsets(dispatcher_HERMES_HOME).
"""
import os
import sys

sys.path.insert(0, "/Users/desac/.hermes/hermes-agent")

# Simulate the dispatched-worker environment
os.environ["HERMES_KANBAN_TASK"] = "t_74e1ff6f"
os.environ["HERMES_KANBAN_BOARD"] = "hscc"
os.environ["HERMES_HOME"] = "/Users/desac/.hermes"  # dispatcher home
os.environ["TERMINAL_ENV"] = "local"
os.environ["HERMES_SESSION_SOURCE"] = "kanban"

from hermes_cli.kanban_db import _resolve_worker_cli_toolsets
from model_tools import _compute_tool_definitions

# 1. What the dispatcher pins
pinned = _resolve_worker_cli_toolsets(env_hermes_home := "/Users/desac/.hermes")
print(f"Dispatched-woker --toolsets pin: {sorted(pinned) if pinned else None}")
print(f"  terminal in pin: {'terminal' in (pinned or [])}")
print(f"  code_execution in pin: {'code_execution' in (pinned or [])}")

# 2. Compute the actual tool definitions a worker would see (quiet)
defs = _compute_tool_definitions(
    enabled_toolsets=pinned, quiet_mode=True, skip_tool_search_assembly=True
)
names = {d["function"]["name"] for d in defs}
print(f"\nTotal tools actually available to worker: {len(names)}")
for want in ["terminal", "execute_code", "read_file", "write_file", "patch",
             "browser_exec", "computer_use", "web_search"]:
    print(f"  {'PRESENT' if want in names else 'MISSING':9} {want}")
