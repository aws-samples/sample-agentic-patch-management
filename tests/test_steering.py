"""Unit tests for steering handlers — no Strands runtime required.

Mocks the strands SDK imports and steering context to test each rule in isolation.
Run with:
    python3 -m pytest tests/test_steering.py -v
"""

import os
import sys
import asyncio
import types
from unittest.mock import MagicMock

# ── Mock the strands SDK before importing steering ────────────────────
# The steering module imports from strands.experimental.steering and strands.types.
# We mock these at the sys.modules level so helper/steering.py loads cleanly.

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


class _Guide:
    """Mock Guide steering action."""
    def __init__(self, reason: str = ""):
        self.reason = reason


class _Proceed:
    """Mock Proceed steering action."""
    def __init__(self, reason: str = ""):
        self.reason = reason


class _SteeringHandler:
    """Mock base class for steering handlers."""
    name = ""

    def __init__(self, context_providers=None):
        self.steering_context = None


class _LedgerProvider:
    pass


# Build mock module tree
_strands = types.ModuleType("strands")
_strands_experimental = types.ModuleType("strands.experimental")
_strands_experimental_steering = types.ModuleType("strands.experimental.steering")
_strands_types = types.ModuleType("strands.types")
_strands_types_content = types.ModuleType("strands.types.content")
_strands_types_streaming = types.ModuleType("strands.types.streaming")
_strands_types_tools = types.ModuleType("strands.types.tools")

_strands_experimental_steering.Guide = _Guide
_strands_experimental_steering.Proceed = _Proceed
_strands_experimental_steering.SteeringHandler = _SteeringHandler
_strands_experimental_steering.LedgerProvider = _LedgerProvider
_strands_experimental_steering.ModelSteeringAction = object
_strands_experimental_steering.ToolSteeringAction = object

_strands_types_content.Message = dict
_strands_types_streaming.StopReason = str
_strands_types_tools.ToolUse = dict

_strands.experimental = _strands_experimental
_strands.types = _strands_types

sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("strands.experimental", _strands_experimental)
sys.modules.setdefault("strands.experimental.steering", _strands_experimental_steering)
sys.modules.setdefault("strands.types", _strands_types)
sys.modules.setdefault("strands.types.content", _strands_types_content)
sys.modules.setdefault("strands.types.streaming", _strands_types_streaming)
sys.modules.setdefault("strands.types.tools", _strands_types_tools)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

import pytest
from helper.steering import (
    PatchWorkflowSteering,
    ComplianceOutputSteering,
)
from helper.goals import ConfirmationGoalHandler

Guide = _Guide
Proceed = _Proceed


# ── Mocks ─────────────────────────────────────────────────────────────

class MockContextData:
    def __init__(self, data: dict):
        self._data = data

    def get(self):
        return self._data


class MockSteeringContext:
    def __init__(self, ledger: dict | None = None):
        self.data = MockContextData({"ledger": ledger or {"tool_calls": []}})


class MockAgent:
    def __init__(self, user_text: str = ""):
        self.messages = []
        if user_text:
            self.messages.append({
                "role": "user",
                "content": [{"text": user_text}],
            })


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_tool_use(name: str, input_: dict | None = None) -> dict:
    return {"name": name, "input": input_ or {}, "toolUseId": "test-id"}


# ── PatchWorkflowSteering ─────────────────────────────────────────────

class TestPatchWorkflowPathSelection:
    """Tests 1-2: Path A vs Path B routing based on instance IDs in user text."""

    def setup_method(self):
        self.handler = PatchWorkflowSteering()
        self.handler.steering_context = MockSteeringContext()

    def test_path_a_blocked_without_instance_ids(self):
        """Path A tool blocked when no instance IDs in user message → Guide to Path B."""
        agent = MockAgent("Patch all critical CVEs in staging environment")
        tool_use = make_tool_use("patch_dry_run", {"environment": "staging"})
        result = run(self.handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "multi_account_dry_run" in result.reason

    def test_path_b_blocked_with_instance_ids(self):
        """Path B tool blocked when instance IDs in user message → Guide to Path A."""
        agent = MockAgent("Patch i-0abc1234def56789 for CVE-2024-1234")
        tool_use = make_tool_use("multi_account_dry_run", {"environment": "dev"})
        result = run(self.handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "patch_dry_run" in result.reason


class TestPatchWorkflowScopeGate:
    """Test 3: multi_account_execute blocked without prior resolve_execution_scope."""

    def setup_method(self):
        self.handler = PatchWorkflowSteering()
        self.handler.steering_context = MockSteeringContext({"tool_calls": []})

    def test_multi_account_execute_blocked_without_scope(self):
        agent = MockAgent("Execute patches across all accounts in staging")
        tool_use = make_tool_use("multi_account_execute", {"environment": "staging"})
        result = run(self.handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "resolve_execution_scope" in result.reason


class TestPatchWorkflowSeverityFilter:
    """Test 4: severity_filter mismatch between dry-run and execute."""

    def setup_method(self):
        self.handler = PatchWorkflowSteering()
        self.handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "patch_dry_run",
                    "status": "success",
                    "tool_args": {"severity_filter": "Critical"},
                    "result": {},
                },
            ]
        })

    def test_severity_mismatch_guides(self):
        agent = MockAgent("Execute the patches on i-0abc1234def56789")
        tool_use = make_tool_use("execute_patch_operation", {
            "severity_filter": "Important",
            "instance_ids": ["i-0abc1234def56789"],
        })
        result = run(self.handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "severity_filter mismatch" in result.reason


class TestPatchWorkflowCVEForwarding:
    """Test 5: CVE in user message but missing from tool input."""

    def setup_method(self):
        self.handler = PatchWorkflowSteering()
        self.handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {"tool_name": "resolve_execution_scope", "status": "success",
                 "tool_args": {}, "result": {}},
            ]
        })

    def test_cve_missing_from_execute_input(self):
        agent = MockAgent("Apply fix for CVE-2024-6387 on i-0abc1234def56789")
        tool_use = make_tool_use("execute_patch_operation", {
            "instance_ids": ["i-0abc1234def56789"],
            "environment": "dev",
        })
        result = run(self.handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "CVE-2024-6387" in result.reason


class TestPatchWorkflowCrossEnv:
    """Tests 6-8: Cross-environment progression blocking."""

    def test_failed_dev_blocks_staging(self):
        """Test 6: Unresolved failure in dev blocks staging."""
        handler = PatchWorkflowSteering()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {"tool_name": "resolve_execution_scope", "status": "success",
                 "tool_args": {}, "result": {}},
                {
                    "tool_name": "execute_patch_operation",
                    "status": "error",
                    "tool_args": {"environment": "dev", "cve_id": "CVE-2024-1234"},
                    "result": {"error": "SSM timeout"},
                },
            ]
        })
        agent = MockAgent("Now patch staging for CVE-2024-1234 on i-0abc1234def56789")
        tool_use = make_tool_use("execute_patch_operation", {
            "environment": "staging",
            "cve_id": "CVE-2024-1234",
            "instance_ids": ["i-0abc1234def56789"],
        })
        result = run(handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Guide)
        assert "dev" in result.reason

    def test_resolved_failure_does_not_block(self):
        """Test 7: Resolved failure (success after error) does NOT block."""
        handler = PatchWorkflowSteering()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {"tool_name": "resolve_execution_scope", "status": "success",
                 "tool_args": {}, "result": {}},
                {
                    "tool_name": "execute_patch_operation",
                    "status": "error",
                    "tool_args": {"environment": "dev", "cve_id": "CVE-2024-1234"},
                    "result": {"error": "SSM timeout"},
                },
                {
                    "tool_name": "execute_patch_operation",
                    "status": "success",
                    "tool_args": {"environment": "dev", "cve_id": "CVE-2024-1234"},
                    "result": {"status": "success"},
                },
            ]
        })
        agent = MockAgent("Now patch staging for CVE-2024-1234 on i-0abc1234def56789")
        tool_use = make_tool_use("execute_patch_operation", {
            "environment": "staging",
            "cve_id": "CVE-2024-1234",
            "instance_ids": ["i-0abc1234def56789"],
        })
        result = run(handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Proceed)

    def test_same_env_retry_allowed(self):
        """Test 8: Same-env retry after failure is allowed."""
        handler = PatchWorkflowSteering()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {"tool_name": "resolve_execution_scope", "status": "success",
                 "tool_args": {}, "result": {}},
                {
                    "tool_name": "execute_patch_operation",
                    "status": "error",
                    "tool_args": {"environment": "dev", "cve_id": "CVE-2024-1234"},
                    "result": {"error": "SSM timeout"},
                },
            ]
        })
        agent = MockAgent("Retry patching dev for CVE-2024-1234 on i-0abc1234def56789")
        tool_use = make_tool_use("execute_patch_operation", {
            "environment": "dev",
            "cve_id": "CVE-2024-1234",
            "instance_ids": ["i-0abc1234def56789"],
        })
        result = run(handler.steer_before_tool(agent=agent, tool_use=tool_use))
        assert isinstance(result, Proceed)


# ── ComplianceOutputSteering ──────────────────────────────────────────

class TestComplianceOutputSteering:
    """Tests 12-13: Compliance output misrepresentation."""

    def test_100_percent_with_zero_reports_guides(self):
        """Test 12: '100% compliance' with zero reports → Guide."""
        handler = ComplianceOutputSteering()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "query_compliance_reports",
                    "status": "success",
                    "tool_args": {"environment": "prod"},
                    "result": [{"text": "{'total_count': 0, 'reports': []}"}],
                },
            ]
        })
        agent = MockAgent("Show me compliance status")
        message = {"content": [{"text": "Great news! Your fleet is at 100% compliance."}]}
        result = run(handler.steer_after_model(
            agent=agent, message=message, stop_reason="end_turn",
        ))
        assert isinstance(result, Guide)
        assert "100%" in result.reason

    def test_normal_response_with_data_proceeds(self):
        """Test 13: Normal response with data → Proceed."""
        handler = ComplianceOutputSteering()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "query_compliance_reports",
                    "status": "success",
                    "tool_args": {"environment": "prod"},
                    "result": [{"text": "{'total_count': 5, 'reports': [...]}"}],
                },
            ]
        })
        agent = MockAgent("Show me compliance status")
        message = {"content": [{"text": "Here are 5 compliance reports for prod..."}]}
        result = run(handler.steer_after_model(
            agent=agent, message=message, stop_reason="end_turn",
        ))
        assert isinstance(result, Proceed)


# ── ConfirmationGoalHandler ───────────────────────────────────────────

class TestConfirmationGoalHandler:
    """Tests 14-16: Confirmation retry handled by goal handler."""

    def test_affirmative_triggers_retry(self):
        """Test 14: Operator says 'yes' after confirmation_required → Guide to retry."""
        handler = ConfirmationGoalHandler()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "execute_patch_operation",
                    "status": "success",
                    "tool_args": {"instance_ids": ["i-0abc1234def56789"], "environment": "dev"},
                    "result": [{"text": "{'status': 'confirmation_required', 'error_code': 'ExecutionConfirmation'}"}],
                },
            ]
        })
        agent = MockAgent("Yes, proceed")
        message = {"content": [{"text": "I'll go ahead and patch those instances."}]}
        result = run(handler.steer_after_model(
            agent=agent, message=message, stop_reason="end_turn",
        ))
        assert isinstance(result, Guide)
        assert "confirm_execute=True" in result.reason
        assert "execute_patch_operation" in result.reason

    def test_no_pending_confirmation_proceeds(self):
        """Test 15: No pending confirmation → Proceed."""
        handler = ConfirmationGoalHandler()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "get_fleet_overview",
                    "status": "success",
                    "tool_args": {},
                    "result": [{"text": "{'instance_count': 5}"}],
                },
            ]
        })
        agent = MockAgent("Yes")
        message = {"content": [{"text": "Here's the fleet overview."}]}
        result = run(handler.steer_after_model(
            agent=agent, message=message, stop_reason="end_turn",
        ))
        assert isinstance(result, Proceed)

    def test_no_scan_gate_includes_confirm_no_scan(self):
        """Test 16: NoScanConfirmation gate → Guide includes confirm_no_scan."""
        handler = ConfirmationGoalHandler()
        handler.steering_context = MockSteeringContext({
            "tool_calls": [
                {
                    "tool_name": "multi_account_execute",
                    "status": "success",
                    "tool_args": {"environment": "dev"},
                    "result": [{"text": "{'status': 'confirmation_required', 'error_code': 'NoScanConfirmation'}"}],
                },
            ]
        })
        agent = MockAgent("ok proceed")
        message = {"content": [{"text": "Proceeding without scan."}]}
        result = run(handler.steer_after_model(
            agent=agent, message=message, stop_reason="end_turn",
        ))
        assert isinstance(result, Guide)
        assert "confirm_no_scan=True" in result.reason


