# Design: Real-Time Workflow Tracker

## Architecture

```
AgentCore SSE Stream
        │
        ▼
┌──────────────────┐     ┌──────────────────┐
│  server.py       │     │  Existing Events  │
│  _parse_agentcore│────▶│  text, tool_start │
│  _stream()       │     │  done, error      │
│                  │     └──────────────────┘
│  + workflow      │
│    event parser  │────▶┌──────────────────┐
│                  │     │  New Events       │
└──────────────────┘     │  workflow_step    │
                         │  workflow_data    │
                         │  workflow_start   │
                         │  workflow_env_done│
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │  useAgentStream   │
                         │  (extended)       │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │ useWorkflowState  │
                         │ (new hook)        │
                         │ State machine     │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │ WorkflowTracker   │
                         │ (new component)   │
                         ├──────────────────┤
                         │ ├ WorkflowHeader  │
                         │ ├ EnvPipeline     │
                         │ ├ StepProgress    │
                         │ ├ InstanceTable   │
                         │ └ ActionButtons   │
                         └──────────────────┘
```

## Design Decisions

### Decision 1: Parse tool data server-side, not client-side
**Requirement:** Req 1 (Structured SSE Events)

The AgentCore SSE stream contains tool input as incremental JSON chunks in `contentBlockDelta.delta.toolUse.input`. Reassembling these chunks into complete JSON requires buffering per content block. This is better done server-side because:
- The server already buffers and parses the stream line-by-line
- Client-side parsing would require duplicating the buffer logic in TypeScript
- Server-side parsing lets us normalize the data before it reaches the frontend

**Approach:**
- Extend `_parse_agentcore_stream` in `server.py` to track a `tool_input_buffer` per content block
- When `contentBlockStart` has `toolUse`, start buffering the tool name and input chunks
- When `contentBlockStop` fires, parse the accumulated input JSON
- Map tool names to workflow steps and emit structured events
- If parsing fails, silently skip (don't break the existing text stream)

**Tool-to-step mapping:**
```python
WORKFLOW_TOOLS = {
    "get_patch_compliance": "discovery",
    "get_maintenance_windows": "window_check",
    "execute_patch_operation": "patch_execute",
    "get_command_status": "patch_execute",  # status update for same step
    "check_instance_health": "health_check",
    "check_cloudwatch_alarms": "alarm_check",
    "generate_compliance_report": "report",
    "verify_and_proceed": "verification",
}
```

**New SSE event types:**
```
workflow_step:    {step, tool, status: "started"|"complete", input: {...}}
workflow_data:    {step, data: {...}}  — parsed tool result data
workflow_start:   {cve_id?, severity?, environments?: [...]}
workflow_env_done:{environment, success_count, failed_count, elapsed_ms}
```

### Decision 2: Workflow state as a reducer, not derived from messages
**Requirement:** Req 2 (Workflow State Management)

Two options:
- A) Derive workflow state by scanning all messages for tool events (reactive)
- B) Maintain a separate state machine that reduces events as they arrive (proactive)

**Chosen: B** — A reducer is simpler, more predictable, and doesn't require re-scanning the message array on every render. The hook subscribes to events from `useAgentStream` and maintains its own state.

**State shape:**
```typescript
interface WorkflowState {
  active: boolean;
  cveId: string | null;
  severity: string | null;
  startTime: number | null;
  environments: EnvState[];
  currentEnvIndex: number;
  completedAt: number | null;  // for collapsed summary
}

interface EnvState {
  name: string;
  status: "pending" | "active" | "complete" | "failed" | "skipped";
  steps: StepState[];
  instances: InstanceState[];
  startTime: number | null;
  endTime: number | null;
  commandId: string | null;
  decision: string | null;       // "EMERGENCY" | "SCHEDULED"
  slaHours: number | null;
  windowHours: number | null;
  framework: string | null;
}

interface StepState {
  id: string;  // "discovery" | "window_check" | "sla_decision" | ...
  label: string;
  status: "pending" | "active" | "complete" | "failed";
  data: Record<string, unknown>;
}

interface InstanceState {
  instanceId: string;
  team: string;
  status: "queued" | "running" | "success" | "failed";
  elapsedMs: number | null;
}
```

**Event reduction rules:**
- `workflow_start` → set `active=true`, populate `environments` from event or default to `["dev","staging","prod"]`
- `workflow_step` with `status=started` → set step to `active`, set environment to `active` if first step
- `workflow_step` with `status=complete` + step data → set step to `complete`, extract key data
- `workflow_data` for `discovery` → populate `instances` array from instance IDs
- `workflow_data` for `patch_execute` → update `commandId`, update instance statuses
- `workflow_env_done` → set environment to `complete`, advance `currentEnvIndex`
- `clearMessages` → reset entire state

### Decision 3: Collapsible container in DashboardPanel, not a separate panel
**Requirement:** Req 7 (Placement and Lifecycle)

Options:
- A) New tab in the dashboard tabs
- B) Floating overlay
- C) Collapsible container above the tabs

**Chosen: C** — The workflow tracker is transient (only during active operations) and cross-cutting (not a "tab" you navigate to). A collapsible container at the top of `DashboardPanel` is visible regardless of which tab is active, doesn't require navigation, and collapses to a summary when done.

**Implementation:**
- `DashboardPanel.tsx` renders `<WorkflowTracker>` above the `<Tabs>` component
- `WorkflowTracker` uses Cloudscape `ExpandableSection` with `variant="container"`
- When `active=true`: expanded by default, shows full tracker
- When `completedAt != null`: collapsed by default, header shows summary line
- When `active=false && completedAt=null`: not rendered at all

### Decision 4: ETA calculation from completed environment averages
**Requirement:** Req 6 (Workflow Header with ETA)

**Approach:**
```
avgMsPerInstance = sum(completedEnv.elapsed / completedEnv.instanceCount) / completedEnvCount
remainingInstances = sum(pendingOrActiveEnv.instanceCount)
eta = avgMsPerInstance * remainingInstances
```

- If 0 environments completed: show "Estimating..."
- If active environment has partial data (3/5 instances done): subtract completed instances from remaining count
- Update ETA every time an instance status changes
- Display as "~Xm remaining" (round to nearest minute)

### Decision 5: Action buttons send chat messages
**Requirement:** Req 8 (Action Buttons)

Rather than building a separate control channel, action buttons simply call `onSend()` from the existing `useAgentStream` hook with predefined messages. This keeps the architecture simple — the agent already understands "proceed", "verify and proceed", and "stop".

**Button visibility rules:**
- "Proceed" → visible when current environment is `complete` and next environment is `pending`
- "Verify & Proceed" → visible alongside "Proceed" (alternative)
- "Abort" → visible whenever `active=true`
- All buttons disabled when `isStreaming=true`

### Decision 6: Extract tool results from text stream, not from a separate channel
**Requirement:** Req 1 (Structured SSE Events)

The AgentCore stream doesn't provide tool results as structured events — tool results are embedded in the assistant's text response. However, the specialist agents (patch_manager, vulnerability_analyst) return structured data that the supervisor then summarizes.

For the workflow tracker, we extract data from two sources:
1. **Tool inputs** (from `toolUse.input` in the stream) — gives us instance IDs, environment names, command IDs
2. **Heuristic text parsing** (from text deltas) — detect patterns like `command_id: cmd-xxx`, `Status: Success`, instance ID patterns

The tool input approach is reliable (structured JSON). The text parsing is best-effort and used only to enrich the display (e.g., detecting when the agent mentions a command completed).

**Priority: Tool inputs first, text heuristics as enhancement only.**

## Component Structure

```
ui/frontend/src/
├── hooks/
│   ├── useAgentStream.ts        # Extended: forward workflow events
│   ├── useWorkflowState.ts      # NEW: workflow state reducer
│   └── useDashboard.ts          # Unchanged
├── components/
│   ├── WorkflowTracker.tsx       # NEW: main container
│   ├── WorkflowHeader.tsx        # NEW: CVE + ETA bar
│   ├── EnvPipeline.tsx           # NEW: horizontal env nodes
│   ├── StepProgress.tsx          # NEW: 7-step vertical list
│   ├── InstanceStatusTable.tsx   # NEW: per-instance table
│   ├── WorkflowActions.tsx       # NEW: proceed/abort buttons
│   ├── DashboardPanel.tsx        # Modified: render WorkflowTracker
│   └── ...existing components
└── lib/
    └── api.ts                    # Unchanged
```

## API Changes

### server.py — Extended SSE events

No new HTTP endpoints. The existing `/api/chat` SSE stream is extended with additional event types. The `_parse_agentcore_stream` generator gains:

1. A `tool_input_buffer: dict` to accumulate tool input JSON chunks
2. A `current_block_tool: str | None` to track which tool is being called
3. Logic to detect `contentBlockStop` events and flush the buffer
4. A `_emit_workflow_event()` helper that maps tool names to workflow steps

### useAgentStream.ts — Extended event handling

The existing SSE event loop in `sendMessage()` gains handlers for the new event types. These are forwarded to a callback that `useWorkflowState` subscribes to:

```typescript
// In useAgentStream, add to the event processing loop:
if (event.type === "workflow_step" || event.type === "workflow_data" || 
    event.type === "workflow_start" || event.type === "workflow_env_done") {
  onWorkflowEvent?.(event);  // callback provided by useWorkflowState
}
```

## Data Flow Example

User sends: "Handle CVE-2025-38527 in dev"

```
1. Agent calls consult_patch_manager("Handle CVE-2025-38527 in dev")
   → SSE: tool_start {tool: "consult_patch_manager"}

2. Patch manager calls get_patch_compliance(environment="dev", severity="CRITICAL")
   → SSE: workflow_start {cve_id: "CVE-2025-38527"}
   → SSE: workflow_step {step: "discovery", status: "started", input: {environment: "dev"}}
   
3. Tool returns: {instances: [...], sla_requirement: {sla_hours: 24, source: "PCI-DSS"}}
   → SSE: workflow_step {step: "discovery", status: "complete"}
   → SSE: workflow_data {step: "discovery", data: {instance_count: 5, instances: [...]}}

4. Patch manager calls get_maintenance_windows(environment="dev")
   → SSE: workflow_step {step: "window_check", status: "started"}
   
5. Tool returns: {windows: [{next_execution: "...", ...}]}
   → SSE: workflow_step {step: "window_check", status: "complete"}
   → SSE: workflow_data {step: "window_check", data: {window_hours: 120}}

6. Agent text: "SLA: 24hr (PCI-DSS) | Window: 120hr | Decision: EMERGENCY"
   → SSE: text {content: "SLA: 24hr..."}
   → SSE: workflow_step {step: "sla_decision", status: "complete", 
           data: {decision: "EMERGENCY", sla_hours: 24, window_hours: 120}}

7. Patch manager calls execute_patch_operation(instance_ids=[...], execution_mode="immediate")
   → SSE: workflow_step {step: "patch_execute", status: "started", 
           input: {instance_ids: [...], execution_mode: "immediate"}}

8. Tool returns: {command_id: "cmd-abc123", status: "EXECUTING"}
   → SSE: workflow_step {step: "patch_execute", status: "complete"}
   → SSE: workflow_data {step: "patch_execute", data: {command_id: "cmd-abc123"}}

... (health checks, alarms, report follow same pattern)
```
