"""
Gateway restart integration for HSCC cluster template apply pipeline.

Handles restarting the Hermes gateway process to pick up config changes
after a template is applied.
"""

from __future__ import annotations

import os
import subprocess


def restart_gateway() -> dict:
    """Restart the Hermes gateway process.
    
    This kicks the gateway launchd process so it reloads config files.
    The gateway typically takes ~10-30s to come back up.
    
    Returns:
        Dict with status and any error message.
    """
    user_id = os.getuid()
    # launchctl domain target is gui/<uid>/<label> (slash, not dot). The dotted
    # form silently fails every kickstart.
    plist_id = f"gui/{user_id}/ai.hermes.gateway"
    
    try:
        # Check if gateway is running
        status_result = subprocess.run(
            ["launchctl", "list", "ai.hermes.gateway"],
            capture_output=True, text=True, timeout=10
        )
        
        # Kickstart the gateway
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", plist_id],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode == 0:
            return {
                "success": True,
                "note": f"Gateway (ai.hermes.gateway) kicked successfully",
            }
        else:
            return {
                "success": False,
                "note": f"Gateway kick failed: {result.stderr.strip() or 'unknown error'}",
            }
    
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "note": "Gateway restart timed out after 30s",
        }
    except Exception as e:
        return {
            "success": False,
            "note": f"Gateway restart failed: {str(e)}",
        }


if __name__ == "__main__":
    result = restart_gateway()
    print(result)
