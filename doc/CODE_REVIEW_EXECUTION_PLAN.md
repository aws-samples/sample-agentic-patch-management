# Code Review — Execution Plan

**Reviewer:** Principal Engineer Review  
**Date:** 2026-02-11  
**Project:** reinvent25-cop407-agentic-patch-management  
**Status:** All items identified, pending execution

---

## Phase 1: CRITICAL (Do First, Block Everything Else)

### 1.1 — Scrub leaked credentials from `.env`

| Field | Value |
|-------|-------|
| Severity | CRITICAL |
| Files | `.env`, `.env.example` |
| Effort | 30 min |
| Dependencies | None |

**Problem:** `.env` contains a real AWS account ID and IAM role ARN (`arn:aws:iam::125140434174:role/...`). While `.gitignore` lists `.env`, the file exists in the workspace and may have been committed historically.

**Action:**
1. Replace `.env` contents with a copy of `.env.example` (all values blank or placeholder)
2. Run `git log --all --full-history -- .env` to check if `.env` was ever committed
3. If yes:
   - Rotate the IAM role (the ARN is burned)
   - Use `git filter-branch` or `BFG Repo-Cleaner` to purge `.env` from all history
   - Force-push the cleaned history
4. Add a pre-commit hook (`.git/hooks/pre-commit` or `pre-commit` framework) that rejects commits containing `.env`

**Validation:** `git log --all -p -- .env` returns nothing after cleanup.

---

### 1.2 — Remove hardcoded memory ID from `supervisor.py`

| Field | Value |
|-------|-------|
| Severity | CRITICAL |
| Files | `agent/supervisor.py`, `.env.example`, `deploy.sh` |
| Effort | 30 min |
| Dependencies | None |

**Problem:** `supervisor.py:163` contains a hardcoded memory ID: `intelligent_patch_automation_mem_20251201144427-55J3BSGwvF`. A comment references `reset_memory.py` which doesn't exist in the repo. Anyone deploying without updating this will silently share memory state with a stale or nonexistent session.

**Action:**
1. Replace the hardcoded `memory_id` with environment variable lookup:
```python
memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
if not memory_id:
    logger.warning("AGENTCORE_MEMORY_ID not set — memory disabled")
```
2. Add `AGENTCORE_MEMORY_ID=` to `.env.example` with a comment explaining how to obtain it
3. Add `AGENTCORE_MEMORY_ID` to `deploy.sh` validation (alongside `AGENTCORE_ROLE_ARN`)
4. Create `scripts/reset_memory.py` that generates a new memory ID and writes it to `.env`

**Validation:** Agent starts without hardcoded ID, falls back gracefully when env var is missing.

---

### 1.3 — Lock down security groups

| Field | Value |
|-------|-------|
| Severity | CRITICAL |
| Files | `infra/lib/security-stack.ts` |
| Effort | 15 min |
| Dependencies | None |

**Problem:** Security group allows SSH from `0.0.0.0/0` (line ~27). SSM Session Manager is already configured on these instances — there is zero reason to open port 22 to the internet. HTTP/HTTPS are also open from `0.0.0.0/0` on instances behind ALBs.

**Action:**
1. Remove the SSH ingress rule entirely:
```typescript
// DELETE:
// this.instanceSecurityGroup.addIngressRule(
//   ec2.Peer.anyIpv4(), ec2.Port.tcp(22), 'SSH access for demo'
// );
```
2. Restrict HTTP/HTTPS ingress to the security group itself (ALB → instance traffic):
```typescript
this.instanceSecurityGroup.addIngressRule(
  this.instanceSecurityGroup,
  ec2.Port.tcp(80),
  'HTTP from ALB only'
);
this.instanceSecurityGroup.addIngressRule(
  this.instanceSecurityGroup,
  ec2.Port.tcp(443),
  'HTTPS from ALB only'
);
```

**Validation:** `cdk diff PatchAutomationSecurity` shows removed SSH rule, scoped HTTP rules.

---

### 1.4 — Scope down IAM policies

| Field | Value |
|-------|-------|
| Severity | CRITICAL |
| Files | `infra/lib/security-stack.ts`, `infra/lib/agentcore-permissions-stack.ts` |
| Effort | 1 hr |
| Dependencies | None |

**Problem:** Both stacks use `resources: ['*']` on nearly every policy statement. The AgentCore permissions stack grants read access to ~20+ AWS services. `iam:List*` and `iam:Get*` on `*` lets the agent enumerate every role, policy, and user in the account. Duplicate role definitions exist across stacks.

**Action:**

**Step A — Remove duplicate policies in `security-stack.ts`:**
- Delete the `agentRole` and `agentCoreExecutionRole` definitions entirely (~lines 70-150). All agent permissions should live in `agentcore-permissions-stack.ts` only.

**Step B — Scope resources in `agentcore-permissions-stack.ts`:**
- EC2/SSM/Inspector/CloudWatch read actions: Keep `resources: ['*']` (describe/list actions don't support resource-level restrictions)
- SSM write actions (`SendCommand`, `RegisterTaskWithMaintenanceWindow`): Scope to tagged instances:
```typescript
new iam.PolicyStatement({
  effect: iam.Effect.ALLOW,
  actions: ['ssm:SendCommand'],
  resources: [
    `arn:aws:ssm:${this.region}:${this.account}:document/AWS-RunPatchBaseline`,
    `arn:aws:ssm:${this.region}:${this.account}:document/AWS-RunShellScript`,
    `arn:aws:ec2:${this.region}:${this.account}:instance/*`
  ],
  conditions: {
    'StringEquals': {
      'ssm:resourceTag/ManagedBy': 'IntelligentPatchAutomation'
    }
  }
})
```
- Remove the broad read permissions block (`lambda:List*`, `s3:List*`, `rds:Describe*`, etc.) entirely. If `use_aws` tool needs these, create a separate policy named `AgentCoreDemoReadOnlyPolicy` with a comment that it must be removed before production
- Remove `iam:List*` and `iam:Get*` — never needed for patch management

**Step C — Scope `iam:PassRole`:**
```typescript
new iam.PolicyStatement({
  effect: iam.Effect.ALLOW,
  actions: ['iam:PassRole'],
  resources: [`arn:aws:iam::${this.account}:role/IntelligentPatchAutomation`],
  conditions: {
    'StringEquals': { 'iam:PassedToService': 'ssm.amazonaws.com' }
  }
})
```

**Validation:** `cdk diff` shows reduced permissions. Run agent end-to-end to confirm no access denied errors on core workflow.

---

### 1.5 — Tighten CORS

| Field | Value |
|-------|-------|
| Severity | CRITICAL |
| Files | `ui/api/server.py` |
| Effort | 5 min |
| Dependencies | None |

**Problem:** `allow_methods=["*"]` and `allow_headers=["*"]` is overly permissive.

**Action:**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Accept"],
)
```

**Validation:** Frontend still works. `OPTIONS` preflight returns scoped headers.

---

## Phase 2: HIGH (Do Before Any External Access)

### 2.1 — Add input validation to `/api/chat`

| Field | Value |
|-------|-------|
| Severity | HIGH |
| Files | `ui/api/server.py` |
| Effort | 45 min |
| Dependencies | None |

**Problem:** No length limit, no sanitization, no rate limiting on the chat endpoint. A 10MB message body gets forwarded directly to AgentCore.

**Action:**
1. Add Pydantic model for request validation:
```python
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    session_id: str | None = Field(None, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
```
2. Replace raw `request.json()` with the model:
```python
@app.post("/api/chat")
async def chat(req: ChatRequest):
    message = req.message.strip()
    session_id = req.session_id or f"web-{uuid.uuid4().hex}"
```
3. Add a rate limiter (in-memory token bucket):
```python
from collections import defaultdict

_rate_limits: dict[str, list[float]] = defaultdict(list)
MAX_REQUESTS_PER_MINUTE = 20

def _check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    window = [t for t in _rate_limits[client_ip] if now - t < 60]
    _rate_limits[client_ip] = window
    if len(window) >= MAX_REQUESTS_PER_MINUTE:
        return False
    _rate_limits[client_ip].append(now)
    return True
```
4. Return 429 if rate limited, 422 if validation fails

**Validation:** `curl -X POST /api/chat -d '{"message":""}' ` returns 422. Rapid-fire requests return 429.

---

### 2.2 — Harden rollback script execution

| Field | Value |
|-------|-------|
| Severity | HIGH |
| Files | `agent/helper/tools.py` |
| Effort | 30 min |
| Dependencies | None |

**Problem:** `rollback_patches` sends a raw bash script via `AWS-RunShellScript`. The script is hardcoded (good), but the pattern is dangerous if anyone adds user-provided parameters.

**Action:**
1. Add security warning docstring
2. Create shared instance ID validator:
```python
import re
INSTANCE_ID_PATTERN = re.compile(r'^i-[0-9a-f]{8,17}$')

def _validate_instance_ids(instance_ids: List[str]) -> Optional[dict]:
    """Returns error dict if any ID is invalid, None if all valid."""
    for iid in instance_ids:
        if not INSTANCE_ID_PATTERN.match(iid):
            return {
                "error": f"Invalid instance ID: {iid}",
                "error_code": "InvalidInstanceId",
                "category": "ABORT",
                "retryable": False
            }
    return None
```
3. Apply validation to all functions accepting `instance_ids`: `rollback_patches`, `execute_patch_operation`, `check_instance_health`, `check_cloudwatch_alarms`, `get_command_status`, `verify_and_proceed`

**Validation:** Pass `["i-abc123; rm -rf /"]` as instance_id — returns ABORT error, does not execute.

---

### 2.3 — Eliminate global mutable state

| Field | Value |
|-------|-------|
| Severity | HIGH |
| Files | `agent/helper/globals.py`, `agent/helper/agent_factory.py`, `agent/supervisor.py` |
| Effort | 1.5 hr |
| Dependencies | 1.2 |

**Problem:** Module-level globals `MEMORY_ID` and `SESSION_ID` in `globals.py` are mutated on every agent creation. In concurrent environments, Agent A's session could bleed into Agent B's context.

**Action:**

**Step A:** Delete `globals.py` entirely.

**Step B:** Modify `agent_factory.py` — stop writing to globals (it already receives values via `context`).

**Step C:** Modify `supervisor.py` tool functions to receive context through closure:
```python
def _make_tools(memory_id: str, session_id: str):
    """Create tool functions with captured context (no globals)."""
    
    @tool
    def consult_vulnerability_analyst(query: str) -> str:
        context_str = get_relevant_context(memory_id, session_id)
        # ... rest using captured memory_id/session_id
    
    @tool
    def consult_patch_manager(query: str) -> str:
        # ... same pattern
    
    @tool
    def consult_compliance_analyst(query: str) -> str:
        # ... same pattern
    
    return [consult_vulnerability_analyst, consult_patch_manager, consult_compliance_analyst]
```

**Step D:** In `agent_invocation`, call `_make_tools(memory_id, context.session_id)` to get the tool list.

**Validation:** Two concurrent agent invocations with different session IDs — context doesn't bleed.

---

### 2.4 — Fix `.env` loading inconsistency in API server

| Field | Value |
|-------|-------|
| Severity | HIGH |
| Files | `ui/api/server.py`, `ui/api/Dockerfile`, `ui/docker-compose.yml` |
| Effort | 30 min |
| Dependencies | 1.1 |

**Problem:** API server loads `.env` via fragile path traversal (`Path(__file__).parent.parent.parent`). In Docker, this path doesn't exist.

**Action:**
1. Replace with explicit env var fallback:
```python
_env_paths = [
    Path(__file__).parent.parent.parent / ".env",  # dev: project root
    Path("/app/.env"),                                # docker
]
for p in _env_paths:
    if p.exists():
        load_dotenv(p)
        break
```
2. In `docker-compose.yml`, use `env_file` instead of mounting `~/.aws`:
```yaml
services:
  api:
    env_file: ../.env
```

**Validation:** `docker-compose up` works without `~/.aws` mount. Local dev still works.

---

### 2.5 — Add authentication middleware stub

| Field | Value |
|-------|-------|
| Severity | HIGH |
| Files | `ui/api/server.py`, `.env.example`, `ui/frontend/src/lib/api.ts`, `ui/frontend/src/hooks/useAgentStream.ts` |
| Effort | 45 min |
| Dependencies | 2.1 |

**Problem:** All API endpoints are completely open. Anyone who can reach port 8000 can invoke the agent, which can execute SSM commands on EC2 instances.

**Action:**
1. Add API key middleware (disabled by default for local dev):
```python
API_KEY = os.environ.get("API_KEY")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/api/health":
        return await call_next(request)
    if not API_KEY:
        return await call_next(request)
    provided_key = request.headers.get("X-API-Key")
    if provided_key != API_KEY:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)
```
2. Add `API_KEY=` to `.env.example`
3. Update frontend to include key in headers if configured:
```typescript
const API_KEY = import.meta.env.VITE_API_KEY;
const headers: Record<string, string> = { "Content-Type": "application/json" };
if (API_KEY) headers["X-API-Key"] = API_KEY;
```

**Validation:** With `API_KEY` set, requests without header return 401. Without `API_KEY`, everything works as before.

---

## Phase 3: MEDIUM (Do Before Scaling)

### 3.1 — Make `verify_and_proceed` non-blocking

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `agent/helper/tools.py`, `agent/patch_manager.py` |
| Effort | 1 hr |
| Dependencies | None |

**Problem:** `verify_and_proceed` has a blocking `time.sleep(120)` plus a polling loop that can block for up to 10 minutes.

**Action:**
1. Split into two tools: `start_verification` (returns immediately) and `check_post_patch_health` (no sleep)
2. Keep original `verify_and_proceed` as convenience wrapper with configurable `max_wait_minutes` parameter
3. Update `patch_manager.py` SYSTEM_PROMPT to document both patterns

**Validation:** Agent can use non-blocking pattern. Blocking pattern still works with configurable timeout.

---

### 3.2 — Pin all dependencies

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `agent/requirements.txt`, `ui/api/requirements.txt`, `ui/frontend/package.json` |
| Effort | 30 min |
| Dependencies | None |

**Problem:** No version pins on `bedrock-agentcore`, `boto3`, or `croniter`. Breaking changes will silently break the build.

**Action:**
1. Generate pinned versions from current working environment: `pip freeze > requirements.lock.txt`
2. Update `requirements.txt` with `~=` compatible release pins
3. Add `engines` field to `package.json`

**Validation:** `pip install -r requirements.txt` in fresh venv produces identical versions. `npm ci` uses lockfile.

---

### 3.3 — Add thread-safe dashboard cache

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `ui/api/server.py` |
| Effort | 20 min |
| Dependencies | None |

**Problem:** Dashboard cache is a bare global dict with no thread safety. Concurrent requests could read/write simultaneously.

**Action:** Replace bare globals with `asyncio.Lock` and double-check pattern after acquiring lock.

**Validation:** Concurrent dashboard requests don't produce duplicate AWS API calls.

---

### 3.4 — Optimize S3 report fetching in dashboard

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `ui/api/server.py`, `agent/helper/tools.py` |
| Effort | 1 hr |
| Dependencies | None |

**Problem:** Dashboard downloads every single JSON report from S3 in parallel. With 100+ reports, this is slow and expensive.

**Action:**
1. When `generate_compliance_report` writes a report, also append a line to a daily `index.jsonl` file
2. In `_fetch_reports`, read index files instead of individual report bodies

**Validation:** Dashboard loads in <2s with 100+ reports.

---

### 3.5 — Add React error boundary

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `ui/frontend/src/components/ErrorBoundary.tsx` (new), `ui/frontend/src/main.tsx` |
| Effort | 15 min |
| Dependencies | None |

**Problem:** If any component throws during render, the entire app crashes to a white screen.

**Action:**
1. Create `ErrorBoundary.tsx` class component with fallback UI and reload button
2. Wrap `<App />` in `<ErrorBoundary>` in `main.tsx`

**Validation:** Throw an error in any component — app shows error UI instead of white screen.

---

### 3.6 — Remove `~/.aws` mount from docker-compose

| Field | Value |
|-------|-------|
| Severity | MEDIUM |
| Files | `ui/docker-compose.yml` |
| Effort | 15 min |
| Dependencies | 2.4 |

**Problem:** `~/.aws:/root/.aws:ro` exposes the host's full AWS credentials directory to the container.

**Action:** Use `env_file` and environment variables instead. Add README note about exporting credentials for Docker.

**Validation:** `docker-compose up` works with exported env vars, no `~/.aws` mount.

---

## Phase 4: LOW (Polish)

### 4.1 — Deduplicate IAM policies

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `infra/lib/security-stack.ts` |
| Effort | Covered by 1.4 Step A |
| Dependencies | 1.4 |

Already addressed in 1.4 Step A (removing duplicate roles from `security-stack.ts`).

---

### 4.2 — Reduce NAT gateways

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `infra/lib/networking-stack.ts` |
| Effort | 5 min |
| Dependencies | None |

**Action:** Change `natGateways: 2` to `natGateways: 1`. Saves ~$32/month.

**Validation:** `cdk diff` shows 1 NAT gateway. Private subnet instances still have internet access.

---

### 4.3 — Use dynamic AMI lookup

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `infra/lib/vulnerability-driven-patching-stack.ts` |
| Effort | 10 min |
| Dependencies | None |

**Problem:** AMI is hardcoded to `us-east-1` only. Deploy to any other region and it fails silently.

**Action:** Use SSM parameter lookup or add a region map for the hardcoded AMI.

**Validation:** `cdk synth` succeeds in target region.

---

### 4.4 — Extract inline styles to constants

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `ui/frontend/src/components/MessageBubble.tsx`, `ChatPanel.tsx` |
| Effort | 30 min |
| Dependencies | None |

**Action:** Create `ui/frontend/src/styles/chat.ts` with exported `React.CSSProperties` constants. Import in components.

**Validation:** No visual changes. Stable style references in React DevTools.

---

### 4.5 — Cache `config.json` reads

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `agent/helper/tools.py` |
| Effort | 10 min |
| Dependencies | None |

**Problem:** `_normalize_environment` and `_calculate_sla_requirement` read `config.json` from disk on every call.

**Action:** Add `@functools.lru_cache(maxsize=1)` wrapper around a `_load_config()` function. Replace all `open(config_path)` calls.

**Validation:** Config file is read once. Subsequent calls use cache.

---

### 4.6 — Document session persistence behavior

| Field | Value |
|-------|-------|
| Severity | LOW |
| Files | `ui/frontend/src/hooks/useAgentStream.ts` |
| Effort | 5 min |
| Dependencies | None |

**Action:** Add docstring explaining that session ID is per-page-load, not persisted. Optionally add `localStorage` persistence.

**Validation:** Comment exists.

---

## Execution Order Summary

| Order | Item | Severity | Est. Effort | Dependencies |
|-------|------|----------|-------------|--------------|
| 1 | 1.1 Scrub `.env` | CRITICAL | 30 min | None |
| 2 | 1.2 Remove hardcoded memory ID | CRITICAL | 30 min | None |
| 3 | 1.3 Lock down security groups | CRITICAL | 15 min | None |
| 4 | 1.4 Scope IAM policies | CRITICAL | 1 hr | None |
| 5 | 1.5 Tighten CORS | CRITICAL | 5 min | None |
| 6 | 2.1 Input validation | HIGH | 45 min | None |
| 7 | 2.2 Harden rollback | HIGH | 30 min | None |
| 8 | 2.3 Eliminate globals | HIGH | 1.5 hr | 1.2 |
| 9 | 2.4 Fix `.env` loading | HIGH | 30 min | 1.1 |
| 10 | 2.5 Auth middleware | HIGH | 45 min | 2.1 |
| 11 | 3.1 Non-blocking verify | MEDIUM | 1 hr | None |
| 12 | 3.2 Pin dependencies | MEDIUM | 30 min | None |
| 13 | 3.3 Thread-safe cache | MEDIUM | 20 min | None |
| 14 | 3.4 Optimize S3 fetching | MEDIUM | 1 hr | None |
| 15 | 3.5 Error boundary | MEDIUM | 15 min | None |
| 16 | 3.6 Remove `~/.aws` mount | MEDIUM | 15 min | 2.4 |
| 17 | 4.1-4.6 Polish items | LOW | 1.5 hr | None |

**Total estimated effort:** ~10 hours  
**Items 1-5:** Parallelizable  
**Items 6-10:** Mostly parallelizable  
**Items 11+:** All independent
