# Requirements: Real-Time Workflow Tracker

## Overview
Add a real-time workflow visualization to the UI that shows the current status of multi-step patching operations as they execute. During a patching workflow (which can span multiple environments and take 10+ minutes), users currently see only tool names and a spinner — no visibility into which instances are being patched, how far along the operation is, or what decisions the agent made.

## Context
- The agent follows a 7-step workflow per environment: Discovery → Window Check → SLA Decision → Execute → Health Check → Alarm Check → Report
- Patching rolls through environments sequentially: dev → staging → prod
- The AgentCore SSE stream already contains tool names (`contentBlockStart.start.toolUse.name`) and tool input JSON (`contentBlockDelta.delta.toolUse.input`) — these are currently ignored by the UI
- The existing chat panel shows tool names as breadcrumbs in `MessageBubble.tsx` but provides no structured progress view

## Requirements

### Requirement 1: Structured SSE Events for Workflow State
The API server must parse tool-specific data from the AgentCore SSE stream and emit structured workflow events to the frontend, in addition to the existing `text`, `tool_start`, and `done` events.

**Acceptance Criteria:**
- When the stream contains a `tool_start` for a patch management tool (`get_patch_compliance`, `execute_patch_operation`, `get_command_status`, `check_instance_health`, `check_cloudwatch_alarms`, `generate_compliance_report`, `get_maintenance_windows`), the server emits a `workflow_step` SSE event with the tool name and its input arguments
- When the stream contains tool result text that includes structured JSON data (instance IDs, command IDs, status), the server emits a `workflow_data` SSE event with the parsed data
- A `workflow_start` event is emitted when the first patch management tool is detected in a session
- A `workflow_env_complete` event is emitted when `generate_compliance_report` completes for an environment
- Existing `text`, `tool_start`, `done`, and `error` events continue to work unchanged
- The parser handles malformed or missing tool data gracefully (no crashes, just skip the structured event)

### Requirement 2: Workflow State Management Hook
The frontend must maintain a state machine that reduces incoming SSE workflow events into a structured workflow state object.

**Acceptance Criteria:**
- A `useWorkflowState` hook consumes SSE events from the existing `useAgentStream` hook
- The state tracks: active workflow flag, CVE ID, severity, start time, list of environments with per-environment status
- Each environment tracks: status (pending/active/complete/failed/skipped), 7-step progress, instance list with per-instance status, timing data
- The state resets when a new conversation starts (`clearMessages`)
- The state correctly handles partial data (e.g., tool fires but result hasn't arrived yet)
- ETA is calculated from average time per instance across completed environments

### Requirement 3: Environment Pipeline Visualization
The workflow tracker must display a horizontal pipeline showing all target environments with their current status.

**Acceptance Criteria:**
- Each environment node shows: name (DEV/STAGING/PROD), status icon (✅ done, 🔄 active, ⏳ next, ❌ failed, ⏭️ skipped), elapsed time, instance count with success ratio (e.g., "3/5 ✓")
- Environments are connected with directional arrows showing the rollout order
- The active environment is visually highlighted (border, background, or scale)
- Uses Cloudscape `StatusIndicator`, `ColumnLayout`, and `Box` components
- Responsive — collapses gracefully on narrow split panel widths

### Requirement 4: Step-by-Step Progress for Active Environment
The workflow tracker must display the 7-step workflow progress for the currently active environment.

**Acceptance Criteria:**
- Steps are displayed as a vertical list: Discovery, Window Check, SLA Decision, Patch Execute, Health Check, Alarm Check, Report
- Each step shows: status (pending/active/complete/failed), key data point from the tool response
- Discovery step shows: instance count, missing patches count
- Window Check step shows: window name, next execution time
- SLA Decision step shows: decision (EMERGENCY/SCHEDULED), SLA hours, window hours, compliance framework — this is the "why" and should be visually prominent
- Patch Execute step shows: command ID, progress (X/Y instances done)
- Health Check step shows: healthy/unhealthy count
- Alarm Check step shows: alarms firing count
- Report step shows: report ID, SLA met/breached
- Uses Cloudscape `StatusIndicator` for step status and `Table` or `SpaceBetween` for layout

### Requirement 5: Instance-Level Status Table
The workflow tracker must display per-instance patching status for the active environment.

**Acceptance Criteria:**
- Table columns: Instance ID, Team (from tags), Status (queued/running/success/failed), Elapsed Time
- Updates in real-time as `get_command_status` results arrive
- Failed instances are highlighted with error status indicator
- Table is only shown for the active environment (collapses for completed environments)
- Uses Cloudscape `Table` component with `StatusIndicator` cells
- Limited to 20 rows with a "showing X of Y" message if more

### Requirement 6: Workflow Header with ETA
The workflow tracker must display a persistent header bar with overall workflow context and estimated time remaining.

**Acceptance Criteria:**
- Header shows: CVE ID, severity badge, total elapsed time (live counter), estimated time remaining
- ETA is calculated as: (avg ms per instance from completed envs) × (remaining instance count)
- If no environments have completed yet, ETA shows "Estimating..." instead of a number
- Elapsed time updates every second while workflow is active
- Uses Cloudscape `Header`, `StatusIndicator`, and `Box` components

### Requirement 7: Workflow Tracker Placement and Lifecycle
The workflow tracker must appear contextually and not permanently consume screen space.

**Acceptance Criteria:**
- The tracker appears as a collapsible `Container` at the top of `DashboardPanel` when a workflow is active
- When the workflow completes, the tracker collapses to a single summary line (e.g., "✅ CVE-2025-38527 patched across 3 environments in 8m 14s — 15/15 instances") that can be expanded
- When a new conversation starts, the tracker disappears
- The tracker does not interfere with the existing tab layout (Environments, Vulnerabilities, Activity, Compliance, Decisions)
- The tracker is visible even when the chat split panel is closed

### Requirement 8: Action Buttons for Workflow Control
The workflow tracker must provide action buttons that send predefined messages to the agent.

**Acceptance Criteria:**
- "Proceed" button appears after each environment completes (sends "proceed" to agent)
- "Verify & Proceed" button appears as alternative (sends "verify and proceed to {next_env}")
- "Abort" button is always visible during active workflow (sends "stop patching, do not proceed to next environment")
- Buttons are disabled while the agent is streaming a response
- Buttons use Cloudscape `Button` component with appropriate variants (primary for proceed, normal for verify, link for abort)
