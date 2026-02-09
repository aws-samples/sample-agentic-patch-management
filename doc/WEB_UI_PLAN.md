# Web UI Plan — Intelligent Patch Automation

## Overview

Replace the terminal chat client (`./chat.sh`) with a browser-based operations console. Not a generic chatbot UI — an opinionated patch management dashboard with an embedded conversational agent.

**Why:** The terminal chat works for developers, but presenting to a CISO or VP of Operations at re:Invent requires a real operations tool. A dashboard with vulnerability cards, environment status, and a chat panel tells a stronger story than a blinking cursor.

---

## Architecture

```
Browser (React)  ←→  Python API (FastAPI)  ←→  AgentCore Runtime (existing)
                           ↓
                     boto3 invoke_agent_runtime
                     (same as patch_client.py today)
```

The browser can't call AgentCore directly (it's a boto3 SDK call, not an HTTP endpoint). The API server is essentially `patch_client.py` exposed as HTTP endpoints with SSE streaming. ~150 lines of code — it's a proxy, not a backend.

**No database.** Session state lives in AgentCore memory (already working). Compliance reports are in S3 (already working). The API is stateless.

**No auth for local/demo.** Runs on `localhost:3000` during presentations. For production deployment, add Cognito + API Gateway in front — separate concern.

---

## Tech Choices

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | React + Vite + TypeScript | Standard for AWS demo UIs, fast builds, no SSR needed |
| Styling | Tailwind CSS | Dashboard-friendly utility classes, fast iteration |
| Components | Hand-built (or shadcn/ui) | No heavy library dependency. shadcn is copy-paste, not a dep |
| Backend | FastAPI (Python) | Same ecosystem as agent (boto3, same venv), native SSE support |
| Streaming | Server-Sent Events (SSE) | Simpler than WebSockets for one-directional streaming |
| State | React state + context | No Redux/Zustand — overkill for this scale |
| Deployment | Single Dockerfile | Multi-stage: build frontend, serve with API |

### What NOT to Build

- No user management or login screen
- No custom dashboard widgets duplicating CloudWatch
- No WebSocket server — SSE is sufficient
- No state management library
- No separate API and frontend deployments

---

## UI Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  🛡️ Intelligent Patch Automation          [env: prod ▾] [⚙️]    │
├────────────────────────────────┬─────────────────────────────────┤
│                                │                                 │
│   DASHBOARD PANEL (left 60%)   │   CHAT PANEL (right 40%)       │
│                                │                                 │
│  ┌──────────────────────────┐  │  ┌───────────────────────────┐  │
│  │ Environment Status       │  │  │ 🤖 Patchy                │  │
│  │ ┌─────┐ ┌─────┐ ┌─────┐ │  │  │                           │  │
│  │ │ DEV │ │ STG │ │ PRD │ │  │  │ Agent: Found 3 CRITICAL   │  │
│  │ │ ✅5 │ │ ✅5 │ │ ⚠️5 │ │  │  │ CVEs in dev...            │  │
│  │ └─────┘ └─────┘ └─────┘ │  │  │                           │  │
│  └──────────────────────────┘  │  │ 🔍 Consulting Vuln...     │  │
│                                │  │ 🛠️ Consulting Patch...     │  │
│  ┌──────────────────────────┐  │  │                           │  │
│  │ Active Vulnerabilities   │  │  │ ✅ Patching complete.     │  │
│  │ CVE-2025-38527  CRITICAL │  │  │ 5/5 instances patched.    │  │
│  │ CVE-2025-12345  HIGH     │  │  │                           │  │
│  │ CVE-2025-99999  MEDIUM   │  │  │                           │  │
│  └──────────────────────────┘  │  ├───────────────────────────┤  │
│                                │  │ [Type a message...]    ➤  │  │
│  ┌──────────────────────────┐  │  └───────────────────────────┘  │
│  │ Recent Patching Activity │  │                                 │
│  │ 10:32 EMERGENCY dev ✅   │  │                                 │
│  │ 10:28 SCHEDULED stg ⏳  │  │                                 │
│  └──────────────────────────┘  │                                 │
│                                │                                 │
│  ┌──────────────────────────┐  │                                 │
│  │ Compliance Summary       │  │                                 │
│  │ SLA Met: 96% | Breach: 1 │  │                                 │
│  └──────────────────────────┘  │                                 │
│                                │                                 │
├────────────────────────────────┴─────────────────────────────────┤
│  Last refresh: 10:35 UTC  |  Session: patch-session-abc123      │
└──────────────────────────────────────────────────────────────────┘
```

Dashboard panels are populated by direct AWS API calls (Inspector, SSM, S3) for reliability. The chat panel uses the agent for interactive operations. This separation means dashboard data is always fast and predictable, while the agent handles the complex decision-making workflows.

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
│   ├── server.py                 # /chat (SSE), /dashboard/*, /health
│   └── requirements.txt          # fastapi, uvicorn, boto3, pyyaml
├── frontend/                     # React + Vite
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── ChatPanel.tsx         # Chat input + streaming messages
│   │   │   ├── MessageBubble.tsx     # Single message with markdown rendering
│   │   │   ├── ToolIndicator.tsx     # "🔍 Consulting Vulnerability Analyst..."
│   │   │   ├── DashboardPanel.tsx    # Left side layout
│   │   │   ├── EnvironmentCard.tsx   # Dev/Staging/Prod status cards
│   │   │   ├── VulnTable.tsx         # Active vulnerabilities table
│   │   │   ├── ActivityFeed.tsx      # Recent patching activity
│   │   │   └── ComplianceBar.tsx     # SLA compliance summary bar
│   │   ├── hooks/
│   │   │   └── useAgentStream.ts     # SSE hook for streaming responses
│   │   └── lib/
│   │       └── api.ts                # fetch wrapper for API endpoints
│   ├── index.html
│   ├── package.json
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   └── vite.config.ts
├── Dockerfile                    # Multi-stage: build frontend, serve with API
└── docker-compose.yml            # Local dev: api + frontend hot-reload
```

---

## API Endpoints

```
POST /api/chat              # Send message, returns SSE stream
  Body: { "message": "...", "session_id": "..." }
  Response: text/event-stream (same SSE format as AgentCore)

GET  /api/dashboard/environments    # Environment status cards
GET  /api/dashboard/vulnerabilities # Active vulnerability findings
GET  /api/dashboard/activity        # Recent patching activity from S3
GET  /api/dashboard/compliance      # Compliance summary statistics
GET  /api/health                    # Health check
```

The `/api/chat` endpoint is the core — it wraps `invoke_agent_runtime`, parses the SSE stream, and re-emits it to the browser. The dashboard endpoints are direct boto3 calls.

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

The React `useAgentStream` hook consumes this and updates component state incrementally.

---

## Implementation Phases

### Phase 1: API Proxy + Chat Panel (2 days)

- FastAPI server wrapping `invoke_agent_runtime` with SSE
- React app with chat panel only (right side of layout)
- `useAgentStream` hook parsing SSE events
- Tool indicators ("🔍 Consulting Vulnerability Analyst...")
- Markdown rendering in message bubbles
- **Deliverable:** Terminal chat client replaced with browser version

### Phase 2: Dashboard Panels (2 days)

- Direct boto3 API calls for dashboard data
- Environment status cards (instance count, health)
- Vulnerability table (severity, CVE ID, affected count)
- Activity feed (recent compliance reports from S3)
- Compliance summary bar (SLA met %, breach count)
- Auto-refresh every 60 seconds
- **Deliverable:** Full dashboard + chat layout

### Phase 3: Interactive Operations (1 day)

- Click CVE card → pre-fill chat input
- Click environment card → pre-fill chat input
- Approval buttons for phased rollout (Proceed / Verify / Stop)
- Status polling for active patch operations
- **Deliverable:** Dashboard and chat are connected

### Phase 4: Polish + Deploy (2 days)

- Loading states, error handling, empty states
- Responsive layout (works on projector screens)
- Dark mode (optional, looks great in presentations)
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
| Large vulnerability lists | Paginate in API, show top 10 with "Show all" link |

---

## Future Considerations (Not in Scope)

- Cognito authentication for multi-user deployment
- API Gateway + CloudFront for production hosting
- WebSocket upgrade if bidirectional communication needed
- Mobile-responsive layout
- Notification system (SNS → browser push for SLA breaches)
- Multi-account support
