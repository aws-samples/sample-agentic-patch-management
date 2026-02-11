# Web UI Plan — Intelligent Patch Automation

## Overview

Replace the terminal chat client (`./chat.sh`) with a browser-based operations console. Not a generic chatbot UI — an opinionated patch management dashboard with an embedded conversational agent.

**Why:** The terminal chat works for developers, but presenting to a CISO or VP of Operations requires a real operations tool. A dashboard with vulnerability cards, environment status, and a chat panel tells a stronger story than a blinking cursor.

---

## Architecture

```
Browser (React + Cloudscape)  ←→  Python API (FastAPI)  ←→  AgentCore Runtime (existing)
                                        ↓
                                  boto3 invoke_agent_runtime
                                  (same as patch_client.py today)
                                        ↓
                                  boto3 direct calls
                                  (Inspector, SSM, EC2, S3 for dashboard data)
```

The browser can't call AgentCore directly (it's a boto3 SDK call, not an HTTP endpoint). The API server is essentially `patch_client.py` exposed as HTTP endpoints with SSE streaming, plus direct AWS API calls for dashboard panels. ~200 lines of code — it's a proxy, not a backend.

**No database.** Session state lives in AgentCore memory (already working). Compliance reports are in S3 (already working). The API is stateless.

**No auth for local/demo.** Runs on `localhost:3000` during presentations. For production deployment, add Cognito + API Gateway in front — separate concern.

---

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | React + TypeScript | Standard for AWS demo UIs, strong typing, large ecosystem |
| Build Tool | Vite | De facto standard for React SPAs in 2025. Fast HMR, zero opinions, static `dist/` output drops into S3 for production hosting. Cloudscape docs and examples use it |
| Component Library | Cloudscape Design System | AWS Console's own design system. Customers instantly recognize the visual language — same muscle memory as EC2/SSM/Inspector consoles. Tables, status indicators, split panels, app layout all map 1:1 to our data model |
| Backend | FastAPI (Python) | Same ecosystem as agent (boto3, same venv), native SSE via `StreamingResponse` |
| Streaming | Server-Sent Events (SSE) | Simpler than WebSockets for one-directional agent response streaming |
| State | React state + context | No Redux/Zustand — overkill for this scale |

### Why Cloudscape Over Alternatives

- **vs Tailwind + shadcn/ui:** Beautiful and fast to build, but looks like a startup SaaS product. Customers subconsciously trust it less for operations tooling. Also requires hundreds of design decisions (spacing, colors, density) that Cloudscape already made.
- **vs Ant Design:** Strong enterprise library, but distinctly non-AWS visual identity. Customers notice the mismatch.
- **vs Retool / Streamlit / Gradio:** Fastest to build, but look like internal tools. Limited control over streaming chat, custom status indicators, and interactive approval flows.

### Why Vite Over Alternatives

- **vs Next.js:** Overkill. No SSR, no file-based routing, no server components needed. Adds a Node.js server in production for features we won't use.
- **vs CRA:** Dead project, unmaintained.
- **vs @aws/nx-plugin:** Scaffolds React + Cloudscape + CDK deployment, but brings Nx monorepo tooling — heavy machinery for a single app.

### What NOT to Build

- No user management or login screen
- No custom dashboard widgets duplicating CloudWatch
- No WebSocket server — SSE is sufficient
- No state management library
- No separate API and frontend deployments for demo

---

## Reference: sample-ai-agent-accelerator

The [aws-samples/sample-ai-agent-accelerator](https://github.com/aws-samples/sample-ai-agent-accelerator) provides useful patterns to reference (not fork — the UI architecture is too different):

**Reuse these patterns:**
- `database.py` — Conversation history via AgentCore Memory `list_events` to reconstruct Q&A pairs. Use this for the sidebar conversation list.
- Docker + ECS Fargate deployment with ALB — production hosting pattern if we move beyond localhost.
- OTEL instrumentation — Flask + boto3 auto-instrumentation for CloudWatch GenAI Observability tracing.

**Skip these patterns:**
- Flask + HTMX approach — HTMX swaps HTML fragments, which is awkward for streaming text into chat bubbles, polling patch status, or updating multiple dashboard panels independently. React gives better control.
- Non-streaming `orchestrator.py` — It does `response.read()` (reads entire response), then renders. Our `patch_client.py` already handles SSE streaming better. For 30-60 second agent responses, streaming is critical UX.
- Bootstrap styling — Cloudscape is the right choice for AWS audience.

---

## UI Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  🛡️ Intelligent Patch Automation                                │
├──────────────────────────────────────┬───────────────────────────┤
│                                      │                           │
│   DASHBOARD PANEL (left 72%)         │   CHAT PANEL (right 28%) │
│                                      │                           │
│  ┌────────────────────────────────┐  │  ┌─────────────────────┐  │
│  │ KPI Bar                       │  │  │ 🤖 Patchy           │  │
│  │ High: 78 | Med: 128 | 206 tot│  │  │                     │  │
│  └────────────────────────────────┘  │  │ Agent: Found 3 CVEs │  │
│                                      │  │ in dev...           │  │
│  ┌────────────────────────────────┐  │  │                     │  │
│  │ [Workflow Tracker]            │  │  │ 🔍 Consulting Vuln..│  │
│  │ ████████░░ 3/5 patched       │  │  │ 🛠️ Consulting Patch..│  │
│  └────────────────────────────────┘  │  │                     │  │
│                                      │  │ ✅ Patching complete│  │
│  ┌────────────────────────────────┐  │  │                     │  │
│  │ Tabs: Vulns | Envs | Activity │  │  ├─────────────────────┤  │
│  │ | Compliance | Explainability │  │  │ [Type a message..] ➤│  │
│  │                               │  │  └─────────────────────┘  │
│  │ [Tab content here]            │  │                           │
│  └────────────────────────────────┘  │                           │
│                                      │                           │
│  ◀ Collapse                          │                           │
├──────────────────────────────────────┴───────────────────────────┤
│  Last refresh: 10:35 UTC  |  Session: patch-session-abc123      │
└──────────────────────────────────────────────────────────────────┘
```

Dashboard is 72% width (min 500px) with smooth CSS collapse/expand. Chat always stays on the right side — never moves to bottom. Collapse arrow points left (toward dashboard) when collapsing.

### Cloudscape Component Mapping

| UI Element | Cloudscape Component |
|-----------|---------------------|
| Page shell | `AppLayout` (navigation, content, split panel) |
| Environment cards | `Cards` with `StatusIndicator` |
| Vulnerability list | `Table` with sorting, filtering, pagination |
| Activity feed | `Table` (compact variant) |
| Compliance summary | `BarChart` or `KeyValuePairs` with `StatusIndicator` |
| Chat panel | `SplitPanel` or `Drawer` containing custom chat component |
| Patching alerts | `Flashbar` |
| Approval buttons | `ButtonGroup` inside chat messages |
| Environment selector | `Select` in header |
| Progress indicator | `ProgressBar` |

The chat panel is the one custom piece — Cloudscape doesn't have a chat/messaging component. Build it as a custom component (~200 lines of React) inside a `SplitPanel`: text input, message list with markdown rendering, streaming indicators.

---

## Data Strategy

### Dashboard Panels → Direct AWS API Calls

The API server calls the same boto3 APIs that `tools.py` uses:

| Panel | Data Source | API Call |
|-------|------------|----------|
| Environment Status | EC2 + SSM | `describe_instances` + `describe_instance_information` |
| Active Vulnerabilities | Inspector | `list_findings` (ACTIVE, sorted by severity) |
| Recent Activity | S3 | `list_objects_v2` on compliance reports bucket |
| Compliance Summary | S3 | Parse report metadata (same as `query_compliance_reports`) |

This is faster, more reliable, and avoids parsing agent markdown into structured data.

### Chat Panel → Agent (AgentCore)

Interactive operations go through the agent:
- "Handle CVE-2025-38527 in dev" → full patching workflow
- "Check health of prod instances" → health check + recommendations
- "Show compliance report for last week" → formatted analysis

### Why Not Agent-Only?

Considered routing everything through the agent (dashboard cards fire agent queries). Rejected because:
- Agent response format can change → dashboard cards break
- Slower (agent thinking time vs direct API call)
- Dashboard is read-only views of AWS state — no decision-making needed

---

## Interactive Features

### Click-to-Action

Dashboard elements pre-fill the chat input for common operations:

| Click Target | Chat Pre-fill |
|-------------|---------------|
| CVE card | "Handle CVE-2025-XXXXX in dev environment" |
| Environment card | "Show instances in prod" |
| Activity row | "Show compliance report for CVE-2025-XXXXX" |
| Compliance breach | "What SLA breaches occurred this week?" |

### Approval Buttons

During phased rollout, render approval buttons instead of requiring typed input:

```
Agent: Dev patching complete. 5/5 instances healthy.
       Ready to proceed to staging?

  [ Proceed to Staging ]  [ Verify First ]  [ Stop ]
```

These buttons send the corresponding message to the agent ("proceed", "verify and proceed", "stop").

### Status Polling

When a patch operation is running, the UI polls `get_command_status` via the API every 10 seconds and renders a progress indicator:

```
Patching dev environment...  ████████░░  3/5 instances complete
```

---

## Project Structure

```
ui/
├── api/                          # FastAPI backend (thin proxy)
│   ├── server.py                 # /api/chat (SSE), /api/dashboard, /api/health
│   └── requirements.txt          # fastapi, uvicorn, boto3, pyyaml
├── frontend/                     # React + Vite + Cloudscape
│   ├── src/
│   │   ├── App.tsx               # Two-column layout with error boundaries
│   │   ├── components/
│   │   │   ├── ChatPanel.tsx         # Chat with streaming + approval buttons
│   │   │   ├── MessageBubble.tsx     # Message with markdown + action extraction
│   │   │   ├── DashboardPanel.tsx    # Tabbed dashboard layout
│   │   │   ├── KpiBar.tsx            # Top-level vulnerability/compliance KPIs
│   │   │   ├── EnvironmentCards.tsx  # Environment status cards
│   │   │   ├── VulnTable.tsx         # Vulnerability findings table
│   │   │   ├── ActivityTable.tsx     # Recent patching activity
│   │   │   ├── ComplianceSummary.tsx # Compliance statistics
│   │   │   ├── ExplainabilityTable.tsx # Decision audit trail
│   │   │   ├── WorkflowTracker.tsx   # Real-time patching progress
│   │   │   ├── EnvPipeline.tsx       # Environment pipeline visualization
│   │   │   ├── StepProgress.tsx      # Workflow step progress bars
│   │   │   ├── InstanceStatusTable.tsx # Per-instance patch status
│   │   │   ├── WorkflowHeader.tsx    # Workflow tracker header
│   │   │   ├── WorkflowActions.tsx   # Workflow action buttons
│   │   │   └── ErrorBoundary.tsx     # Isolated crash recovery
│   │   ├── hooks/
│   │   │   ├── useAgentStream.ts     # SSE hook for streaming responses
│   │   │   ├── useWorkflowState.ts   # Workflow state reducer + ETA estimation
│   │   │   └── useDashboard.ts       # Dashboard data fetching + caching
│   │   └── lib/
│   │       └── api.ts                # fetch wrapper for API endpoints
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig.json
├── Dockerfile                    # Multi-stage: build frontend, serve with API
└── docker-compose.yml            # Local dev: api + frontend hot-reload
```

---

## API Endpoints

```
POST /api/chat              # Send message, returns SSE stream
  Body: { "message": "...", "session_id": "..." }
  Response: text/event-stream

GET  /api/dashboard              # All dashboard data (single endpoint)
  Query: ?force=true             # Bypass 30s cache
  Response: { environments, findings, severity_counts, activities,
              compliance, report_details, errors, cached }

GET  /api/health                 # Health check
```

The `/api/chat` endpoint wraps `invoke_agent_runtime`, parses the SSE stream, drains SQS workflow events, and re-emits both to the browser. The `/api/dashboard` endpoint runs 3 concurrent boto3 fetches (environments, vulnerabilities, reports) with 30-second caching.

> **Note:** The original plan specified separate dashboard endpoints (`/api/dashboard/environments`, `/api/dashboard/vulnerabilities`, etc.) but the implementation consolidated them into a single `/api/dashboard` endpoint for simplicity — one fetch from the frontend gets everything.

---

## SSE Stream Format (Browser ← API)

The API re-emits AgentCore's SSE events in a simplified format:

```
data: {"type": "tool_start", "tool": "consult_vulnerability_analyst"}
data: {"type": "text", "content": "I found 3 critical"}
data: {"type": "text", "content": " vulnerabilities..."}
data: {"type": "tool_start", "tool": "consult_patch_manager"}
data: {"type": "text", "content": "Initiating emergency"}
data: {"type": "done", "duration_ms": 12340}
```

Additionally, workflow events are injected inline from an SQS FIFO queue:

```
data: {"type": "workflow_start", "cve_id": "CVE-2025-38527", "severity": "CRITICAL", "environments": ["dev"]}
data: {"type": "workflow_step", "step": "discovery", "tool": "get_patch_compliance", "status": "started"}
data: {"type": "workflow_step", "step": "discovery", "tool": "get_patch_compliance", "status": "complete"}
data: {"type": "workflow_data", "step": "discovery", "tool": "get_patch_compliance", "data": {...}}
data: {"type": "workflow_env_done", "environment": "dev"}
```

### Workflow Event Transport Architecture

The agent runs inside an AgentCore container (remote). The FastAPI SSE endpoint runs locally. These are separate processes on separate machines, so in-memory queues cannot work.

**Solution: SQS FIFO queue** (`patch-automation-workflow-events.fifo`)

```
AgentCore Container                    Local Machine
+---------------------+               +----------------------+
| Specialist Agent    |               | FastAPI SSE Endpoint |
|   |                 |               |   |                  |
| WorkflowTrackingHook|--SQS FIFO--> | emitter.drain()      |
|   |                 |               |   |                  |
| sqs.send_message()  |               | yield evt.to_sse()   |
+---------------------+               +----------------------+
                                              |
                                        Browser (SSE)
                                              |
                                      useWorkflowState reducer
                                              |
                                      WorkflowTracker UI
```

**Why SQS FIFO over alternatives:**
- Purpose-built for producer/consumer message passing (vs DynamoDB polling)
- MessageGroupId per session ensures ordered delivery
- Messages auto-delete after receive + delete (no cleanup needed)
- No infrastructure to manage (no tables, no TTL, no cursors)
- Both processes already have boto3 + IAM permissions
- EventBridge requires a reachable target (FastAPI runs on localhost)

The React `useAgentStream` hook consumes both text and workflow events, forwarding workflow events to the `useWorkflowState` reducer which drives the `WorkflowTracker` UI component.

---

## Implementation Phases

### Phase 1: API Proxy + Chat Panel (2 days)

- FastAPI server wrapping `invoke_agent_runtime` with SSE
- React + Cloudscape `AppLayout` with `SplitPanel` for chat
- Custom chat component: input, message list, markdown rendering
- `useAgentStream` hook parsing SSE events
- Tool indicators ("🔍 Consulting Vulnerability Analyst...")
- **Deliverable:** Terminal chat client replaced with browser version

### Phase 2: Dashboard Panels (2 days)

- Direct boto3 API calls for dashboard data
- Cloudscape `Cards` + `StatusIndicator` for environment status
- Cloudscape `Table` for vulnerability findings (sortable, filterable)
- Activity feed table (recent compliance reports from S3)
- Compliance summary with `KeyValuePairs`
- Auto-refresh every 60 seconds
- **Deliverable:** Full dashboard + chat layout

### Phase 3: Interactive Operations (1 day)

- Click CVE row → pre-fill chat input
- Click environment card → pre-fill chat input
- Approval buttons for phased rollout (Proceed / Verify / Stop)
- `ProgressBar` for active patch operations with status polling
- **Deliverable:** Dashboard and chat are connected

### Phase 4: Polish + Deploy (2 days)

- Loading states, error handling, empty states
- Responsive layout (works on projector screens)
- Cloudscape dark mode (visual mode toggle)
- `./deploy.sh ui` integration
- Dockerfile for containerized deployment
- **Deliverable:** Demo-ready, deployable

### Timeline Summary

| Phase | What | Days | Cumulative |
|-------|------|------|------------|
| 1 | API proxy + chat in browser | 2 | Day 2 |
| 2 | Dashboard panels | 2 | Day 4 |
| 3 | Interactive operations | 1 | Day 5 |
| 4 | Polish + deploy integration | 2 | Day 7 |

**Realistic estimate:** 5 working days for demo-ready, 7 for polished.

---

## Key Risks

| Risk | Mitigation |
|------|-----------|
| AgentCore SSE format changes | API proxy normalizes the stream — frontend is insulated |
| Dashboard data stale | Auto-refresh + manual refresh button + timestamp display |
| Agent response too slow for dashboard | Dashboard uses direct API calls, not agent |
| CORS issues in local dev | Vite proxy config + FastAPI CORS middleware |
| Large vulnerability lists | Cloudscape Table pagination (built-in) |

---

## Future Considerations (Not in Scope)

- Cognito authentication for multi-user deployment
- S3 + CloudFront hosting for frontend (Vite `dist/` output is static, drops straight into S3)
- API Gateway in front of FastAPI for production
- ECS Fargate deployment (reference: sample-ai-agent-accelerator pattern)
- OTEL tracing on the API layer (reference: sample-ai-agent-accelerator pattern)
- Conversation history sidebar via AgentCore Memory (reference: sample-ai-agent-accelerator `database.py` pattern)
- Mobile-responsive layout
- Notification system (SNS → browser push for SLA breaches)
- Multi-account support
