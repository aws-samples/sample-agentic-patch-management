# Tasks: Real-Time Workflow Tracker

## Task 1: Extend SSE parser to emit workflow events
**Requirement:** Req 1  
**File:** `ui/api/server.py`

### Subtasks:
- [x] 1.1: Add `WORKFLOW_TOOLS` mapping dict (tool name → step name) and `_emit_workflow_event()` helper function
- [x] 1.2: Add `tool_input_buffer`, `current_block_tool`, and `current_block_index` tracking variables to `_parse_agentcore_stream`
- [x] 1.3: Extend `contentBlockStart` handler to detect workflow tools and emit `workflow_step` with `status: "started"` and buffer the tool input
- [x] 1.4: Add `contentBlockDelta` handler for `toolUse.input` chunks — append to `tool_input_buffer`
- [x] 1.5: Add `contentBlockStop` handler — parse accumulated tool input JSON, emit `workflow_data` event with extracted data, reset buffer
- [x] 1.6: Add `workflow_start` detection — emit on first workflow tool in a session (extract CVE ID and severity from tool input if available)
- [x] 1.7: Add `workflow_env_done` detection — emit when `generate_compliance_report` tool completes (extract environment from input)
- [x] 1.8: Test with malformed/missing tool data — verify no crashes, existing events unaffected

## Task 2: Create `useWorkflowState` hook
**Requirement:** Req 2  
**File:** `ui/frontend/src/hooks/useWorkflowState.ts` (new)

### Subtasks:
- [x] 2.1: Define TypeScript interfaces: `WorkflowState`, `EnvState`, `StepState`, `InstanceState`, `WorkflowEvent`
- [x] 2.2: Define the 7 default steps with labels: Discovery, Window Check, SLA Decision, Patch Execute, Health Check, Alarm Check, Report
- [x] 2.3: Implement `workflowReducer` function that handles: `workflow_start`, `workflow_step`, `workflow_data`, `workflow_env_done`, `reset`
- [x] 2.4: Implement `workflow_start` reducer — set active, populate environments (from event data or default `["dev","staging","prod"]`), set CVE/severity
- [x] 2.5: Implement `workflow_step` reducer — update step status, set environment to active on first step, extract key data points per step type
- [x] 2.6: Implement `workflow_data` reducer — populate instances array from discovery data, update instance statuses from command status data, store command IDs
- [x] 2.7: Implement `workflow_env_done` reducer — mark environment complete, advance `currentEnvIndex`
- [x] 2.8: Implement `reset` reducer — return initial empty state
- [x] 2.9: Implement `estimateRemainingMs()` utility — average ms per instance from completed environments × remaining instances
- [x] 2.10: Export `useWorkflowState` hook that returns `{ state, dispatch, estimatedRemainingMs }`

## Task 3: Extend `useAgentStream` to forward workflow events
**Requirement:** Req 2  
**File:** `ui/frontend/src/hooks/useAgentStream.ts`

### Subtasks:
- [x] 3.1: Add `onWorkflowEvent` callback parameter to `useAgentStream` (or expose via return value)
- [x] 3.2: In the SSE event processing loop, add handlers for `workflow_start`, `workflow_step`, `workflow_data`, `workflow_env_done` event types
- [x] 3.3: Forward these events to the `onWorkflowEvent` callback
- [x] 3.4: On `clearMessages`, also fire a `reset` event to the workflow callback

## Task 4: Build `WorkflowHeader` component
**Requirement:** Req 6  
**File:** `ui/frontend/src/components/WorkflowHeader.tsx` (new)

### Subtasks:
- [x] 4.1: Create component accepting `WorkflowState` and `estimatedRemainingMs` props
- [x] 4.2: Display CVE ID and severity badge using Cloudscape `StatusIndicator`
- [x] 4.3: Display live elapsed time counter (updates every second via `setInterval`)
- [x] 4.4: Display ETA as "~Xm remaining" or "Estimating..." if no completed environments
- [x] 4.5: Display overall progress: "Environment 2/3 · 8/15 instances"

## Task 5: Build `EnvPipeline` component
**Requirement:** Req 3  
**File:** `ui/frontend/src/components/EnvPipeline.tsx` (new)

### Subtasks:
- [x] 5.1: Create component accepting `environments: EnvState[]` and `currentEnvIndex: number`
- [x] 5.2: Render horizontal row of environment nodes using Cloudscape `ColumnLayout` or flex layout
- [x] 5.3: Each node shows: environment name (uppercase), status icon (✅/🔄/⏳/❌/⏭️), elapsed time, instance count with success ratio
- [x] 5.4: Connect nodes with arrow indicators (CSS `→` or border-based)
- [x] 5.5: Highlight active environment with distinct border/background
- [x] 5.6: Handle responsive collapse — stack vertically on narrow widths

## Task 6: Build `StepProgress` component
**Requirement:** Req 4  
**File:** `ui/frontend/src/components/StepProgress.tsx` (new)

### Subtasks:
- [x] 6.1: Create component accepting `steps: StepState[]` and `envName: string`
- [x] 6.2: Render vertical list of steps with Cloudscape `StatusIndicator` for each status
- [x] 6.3: Display key data point next to each step (instance count for discovery, decision for SLA, command ID for execute, etc.)
- [x] 6.4: Visually emphasize the SLA Decision step (it's the "why") — use a highlighted background or left border
- [x] 6.5: Show "Active" spinner for the currently running step

## Task 7: Build `InstanceStatusTable` component
**Requirement:** Req 5  
**File:** `ui/frontend/src/components/InstanceStatusTable.tsx` (new)

### Subtasks:
- [x] 7.1: Create component accepting `instances: InstanceState[]`
- [x] 7.2: Render Cloudscape `Table` with columns: Instance ID, Team, Status, Elapsed Time
- [x] 7.3: Use `StatusIndicator` for status column (success/error/in-progress/pending)
- [x] 7.4: Limit to 20 rows with "Showing X of Y" counter in header
- [x] 7.5: Sort: failed first, then running, then queued, then success

## Task 8: Build `WorkflowActions` component
**Requirement:** Req 8  
**File:** `ui/frontend/src/components/WorkflowActions.tsx` (new)

### Subtasks:
- [x] 8.1: Create component accepting `workflowState`, `isStreaming`, and `onSend` props
- [x] 8.2: Render "Proceed" button (primary variant) — visible when current env is complete and next is pending, sends "proceed"
- [x] 8.3: Render "Verify & Proceed" button (normal variant) — visible alongside Proceed, sends "verify and proceed to {next_env}"
- [x] 8.4: Render "Abort" button (link variant, red) — always visible when active, sends "stop patching, do not proceed to next environment"
- [x] 8.5: Disable all buttons when `isStreaming=true`

## Task 9: Build `WorkflowTracker` container component
**Requirement:** Req 7  
**File:** `ui/frontend/src/components/WorkflowTracker.tsx` (new)

### Subtasks:
- [x] 9.1: Create component that composes `WorkflowHeader`, `EnvPipeline`, `StepProgress`, `InstanceStatusTable`, `WorkflowActions`
- [x] 9.2: Use Cloudscape `ExpandableSection` with `variant="container"`
- [x] 9.3: When `active=true`: expanded by default, full tracker visible
- [x] 9.4: When `completedAt != null`: collapsed by default, header shows summary line ("✅ CVE-X patched across N environments in Xm — X/X instances")
- [x] 9.5: When `active=false && completedAt=null`: render nothing (return null)
- [x] 9.6: Pass `onSend` and `isStreaming` through to `WorkflowActions`

## Task 10: Integrate into `DashboardPanel` and `App`
**Requirement:** Req 7  
**Files:** `ui/frontend/src/components/DashboardPanel.tsx`, `ui/frontend/src/App.tsx`

### Subtasks:
- [x] 10.1: In `App.tsx`, instantiate `useWorkflowState` hook and wire it to `useAgentStream` via the workflow event callback
- [x] 10.2: Pass `workflowState` and `estimatedRemainingMs` down to `DashboardPanel`
- [x] 10.3: In `DashboardPanel.tsx`, render `<WorkflowTracker>` above the `<Tabs>` component
- [x] 10.4: Pass `onSend` (from `useAgentStream`) and `isStreaming` through to `WorkflowTracker`
- [x] 10.5: Verify existing dashboard tabs, chat panel, and all other functionality still works unchanged
- [x] 10.6: Test end-to-end: send a patching command, verify tracker appears, updates in real-time, collapses on completion
