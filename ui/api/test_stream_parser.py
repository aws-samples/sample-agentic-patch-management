"""Tests for _parse_agentcore_stream workflow event emission (Task 1.8).

Verifies:
- Workflow events are emitted for known tools
- Malformed/missing tool data doesn't crash the parser
- Existing text/tool_start/done events are unaffected
"""

import io
import json
import unittest


# Import the parser and helpers directly
from server import _parse_agentcore_stream, WORKFLOW_TOOLS


def _make_sse_line(data: dict) -> bytes:
    """Create a single SSE data line as bytes."""
    return f"data: {json.dumps(data)}\n".encode("utf-8")


def _build_stream(events: list[dict]) -> io.BytesIO:
    """Build a BytesIO stream from a list of SSE event dicts."""
    raw = b""
    for evt in events:
        raw += _make_sse_line(evt)
    return io.BytesIO(raw)


def _collect_events(stream) -> list[dict]:
    """Run the parser and collect all emitted SSE events as parsed dicts."""
    results = []
    for sse_line in _parse_agentcore_stream(stream):
        # Each line is "data: {...}\n\n"
        if sse_line.startswith("data: "):
            payload = json.loads(sse_line[6:].strip())
            results.append(payload)
    return results


class TestWorkflowEvents(unittest.TestCase):
    """Test workflow event emission from _parse_agentcore_stream."""

    def _workflow_tool_sequence(self, tool_name: str, tool_input: dict) -> list[dict]:
        """Build a standard contentBlockStart → deltas → contentBlockStop sequence."""
        return [
            {"event": {"contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"name": tool_name, "toolUseId": "tu-1"}},
            }}},
            {"event": {"contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": json.dumps(tool_input)}},
            }}},
            {"event": {"contentBlockStop": {"contentBlockIndex": 0}}},
        ]

    def test_workflow_step_emitted_for_known_tool(self):
        """A known workflow tool emits workflow_step with status=started."""
        events = self._workflow_tool_sequence(
            "get_patch_compliance", {"environment": "dev", "severity": "CRITICAL"}
        )
        results = _collect_events(_build_stream(events))

        step_events = [e for e in results if e["type"] == "workflow_step"]
        self.assertEqual(len(step_events), 1)
        self.assertEqual(step_events[0]["step"], "discovery")
        self.assertEqual(step_events[0]["status"], "started")
        self.assertEqual(step_events[0]["tool"], "get_patch_compliance")

    def test_workflow_start_emitted_once(self):
        """workflow_start is emitted on the first workflow tool only."""
        events = (
            self._workflow_tool_sequence("get_patch_compliance", {"environment": "dev", "severity": "HIGH"})
            + self._workflow_tool_sequence("get_maintenance_windows", {"environment": "dev"})
        )
        results = _collect_events(_build_stream(events))

        start_events = [e for e in results if e["type"] == "workflow_start"]
        self.assertEqual(len(start_events), 1)
        self.assertEqual(start_events[0].get("severity"), "HIGH")

    def test_workflow_data_emitted_with_parsed_input(self):
        """workflow_data contains the parsed tool input JSON."""
        tool_input = {"environment": "staging", "instance_ids": ["i-111", "i-222"]}
        events = self._workflow_tool_sequence("execute_patch_operation", tool_input)
        results = _collect_events(_build_stream(events))

        data_events = [e for e in results if e["type"] == "workflow_data"]
        self.assertEqual(len(data_events), 1)
        self.assertEqual(data_events[0]["data"]["instance_ids"], ["i-111", "i-222"])
        self.assertEqual(data_events[0]["step"], "patch_execute")

    def test_workflow_env_done_on_report(self):
        """workflow_env_done is emitted when generate_compliance_report completes."""
        events = self._workflow_tool_sequence(
            "generate_compliance_report",
            {"cve_id": "CVE-2025-1234", "environment": "prod", "command_id": "cmd-1", "decision": "EMERGENCY"},
        )
        results = _collect_events(_build_stream(events))

        env_done = [e for e in results if e["type"] == "workflow_env_done"]
        self.assertEqual(len(env_done), 1)
        self.assertEqual(env_done[0]["environment"], "prod")

    def test_malformed_tool_input_no_crash(self):
        """Malformed JSON in tool input doesn't crash — just skips workflow_data."""
        events = [
            {"event": {"contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"name": "get_patch_compliance", "toolUseId": "tu-1"}},
            }}},
            {"event": {"contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": "{broken json!!!"}},
            }}},
            {"event": {"contentBlockStop": {"contentBlockIndex": 0}}},
        ]
        # Should not raise
        results = _collect_events(_build_stream(events))

        # workflow_step started should still be emitted
        step_events = [e for e in results if e["type"] == "workflow_step"]
        self.assertEqual(len(step_events), 1)
        # workflow_data should NOT be emitted (empty parsed result)
        data_events = [e for e in results if e["type"] == "workflow_data"]
        self.assertEqual(len(data_events), 0)
        # done event should still appear
        done_events = [e for e in results if e["type"] == "done"]
        self.assertEqual(len(done_events), 1)

    def test_non_workflow_tool_no_workflow_events(self):
        """A non-workflow tool emits tool_start but no workflow events."""
        events = self._workflow_tool_sequence("consult_patch_manager", {"prompt": "hello"})
        results = _collect_events(_build_stream(events))

        tool_starts = [e for e in results if e["type"] == "tool_start"]
        self.assertEqual(len(tool_starts), 1)
        self.assertEqual(tool_starts[0]["tool"], "consult_patch_manager")

        workflow_events = [e for e in results if e["type"].startswith("workflow_")]
        self.assertEqual(len(workflow_events), 0)

    def test_existing_text_events_unaffected(self):
        """Text delta events still work alongside workflow events."""
        events = [
            {"event": {"contentBlockDelta": {"delta": {"text": "Hello "}}}},
            {"event": {"contentBlockDelta": {"delta": {"text": "world"}}}},
        ]
        results = _collect_events(_build_stream(events))

        text_events = [e for e in results if e["type"] == "text"]
        self.assertEqual(len(text_events), 2)
        self.assertEqual(text_events[0]["content"], "Hello ")
        self.assertEqual(text_events[1]["content"], "world")

    def test_empty_stream_yields_done(self):
        """An empty stream still yields a done event."""
        results = _collect_events(io.BytesIO(b""))
        done_events = [e for e in results if e["type"] == "done"]
        self.assertEqual(len(done_events), 1)

    def test_chunked_tool_input_reassembly(self):
        """Tool input arriving in multiple delta chunks is reassembled correctly."""
        full_input = {"environment": "dev", "severity": "CRITICAL", "instance_ids": ["i-1"]}
        input_str = json.dumps(full_input)
        mid = len(input_str) // 2

        events = [
            {"event": {"contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"name": "get_patch_compliance", "toolUseId": "tu-1"}},
            }}},
            {"event": {"contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": input_str[:mid]}},
            }}},
            {"event": {"contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": input_str[mid:]}},
            }}},
            {"event": {"contentBlockStop": {"contentBlockIndex": 0}}},
        ]
        results = _collect_events(_build_stream(events))

        data_events = [e for e in results if e["type"] == "workflow_data"]
        self.assertEqual(len(data_events), 1)
        self.assertEqual(data_events[0]["data"], full_input)


if __name__ == "__main__":
    unittest.main()
