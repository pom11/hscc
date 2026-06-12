"""Tests for gateway_restart module.

Tests verify that restart_gateway() handles subprocess errors gracefully
and returns the expected result dict structure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add cluster-template directory to path (same pattern as other tests)
PLUGIN_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(PLUGIN_DIR))

from gateway_restart import restart_gateway


class TestRestartGatewayHappyPath:
    """Test the normal success path."""

    def test_returns_success_on_good_kickstart(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            result = restart_gateway()

            assert result["success"] is True
            assert result.get("note", "")
            # Should call kickstart
            assert any("kickstart" in str(c[0][0]) for c in mock_run.call_args_list)

    def test_calls_kickstart(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            restart_gateway()

            # Should have called kickstart
            assert any("kickstart" in str(c[0][0]) for c in mock_run.call_args_list)

    def test_kickstart_uses_slash_domain_syntax(self):
        # Regression: domain target must be gui/<uid>/<label> (slash), not the
        # dotted gui.<uid>/ form, which launchctl rejects.
        import os
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            restart_gateway()
            kick = [c for c in mock_run.call_args_list
                    if "kickstart" in str(c[0][0])][0]
            argv = kick[0][0]
            target = argv[-1]
            assert target == f"gui/{os.getuid()}/ai.hermes.gateway"
            assert "gui." not in target


class TestRestartGatewayKickstartFails:
    """Test when kickstart returns a non-zero exit code."""

    def test_returns_failure_with_stderr_note(self):
        list_mock = MagicMock(returncode=0)
        kick_mock = MagicMock(returncode=1, stderr="Service not found")
        mock_run = MagicMock(side_effect=[list_mock, kick_mock])

        with patch("subprocess.run", mock_run):
            result = restart_gateway()

            assert result["success"] is False
            assert "note" in result


class TestRestartGatewaySubprocessErrors:
    """Test graceful handling of subprocess exceptions."""

    def test_handles_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
            cmd=["launchctl", "list"], timeout=10
        )):
            result = restart_gateway()

            assert result["success"] is False
            assert "note" in result

    def test_handles_generic_exception(self):
        with patch("subprocess.run", side_effect=RuntimeError("unexpected")):
            result = restart_gateway()

            assert result["success"] is False
            assert "note" in result


class TestRestartGatewayResultStructure:
    """Test that the result dict always has the expected shape."""

    def test_result_always_has_success_and_note(self):
        for side_effect in [
            MagicMock(returncode=0, stderr=""),
            subprocess.TimeoutExpired(cmd=["launchctl"], timeout=10),
            RuntimeError("boom"),
        ]:
            with patch("subprocess.run", side_effect=[side_effect]):
                result = restart_gateway()

            assert "success" in result
            assert "note" in result
            assert isinstance(result["success"], bool)
            assert isinstance(result["note"], str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
