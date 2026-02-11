# Technical Architecture

## Project Structure

```
agent/
├── supervisor.py              # Orchestration agent
├── vulnerability_agent.py     # Security specialist
├── patch_manager.py           # Operations specialist
├── compliance_analyst.py      # Reporting specialist
├── helper/                    # Helper modules
│   ├── tools.py              # 12 AWS tools
│   ├── error_handling.py     # Error classification (RETRYABLE/SKIP/ABORT)
│   ├── decision_logger.py    # Decision audit trail (CloudWatch + S3)
│   ├── agent_factory.py      # Common agent creation with workflow hooks
│   ├── workflow_events.py    # SQS FIFO workflow event transport
│   ├── context_helper.py     # Cross-agent context sharing
│   ├── memory_hooks.py       # AgentCore memory integration
│   └── globals.py            # Shared state
└── config/
    └── config.json           # SLA policies, environment aliases
```

---

## Agent System Design

### Supervisor Agent
**Role**: Intelligent routing and workflow orchestration

**Tools**:
- `consult_vulnerability_analyst()` - Delegate to security specialist
- `consult_patch_manager()` - Delegate to operations specialist
- `consult_compliance_analyst()` - Delegate to compliance specialist
- `use_aws()` - Built-in tool from `strands_tools` for AWS service access

**Routing Logic** (Keyword-Based):
- Security queries (CVE, vulnerability, CVSS, threat, Inspector) → Vulnerability Analyst
- Infrastructure queries (patch, instance, health, rollback, EC2, SSM, maintenance, SLA requirement) → Patch Manager
- Compliance queries (report, SLA breach, audit trail, compliance status) → Compliance Analyst
- AWS service queries (Lambda, S3, CloudWatch, RDS, billing, cost) → use_aws (direct)
- OUT_OF_SCOPE responses → Retry with use_aws fallback

**Performance**: 18-36% faster via keyword-based classification

**Key Design**: Only agent with `use_aws` access - enforces separation of concerns

---

### Vulnerability Analyst Agent
**Role**: Security threat analysis and risk assessment

**Tools**:
- `get_vulnerability_findings(cve_id, severity, environment)` - Query AWS Inspector
- `get_resource_dependencies(instance_id)` - Impact assessment via AWS Config + ELB health

**Behavior**:
- CVSS scoring and threat classification
- Environment-based vulnerability filtering via Inspector resourceTags
- Impact assessment for patching safety
- Returns OUT_OF_SCOPE for infrastructure/patching/AWS service queries


---

### Patch Manager Agent
**Role**: Infrastructure operations and patch execution

**Tools** (11 specialized tools):
- `get_patch_compliance(environment, severity)` - Patch states + SLA calculation
- `execute_patch_operation(instance_ids)` - Async SSM patching
- `get_command_status(command_id, instance_ids)` - Check async operation status
- `check_instance_health(instance_ids)` - Verify SSM connectivity
- `check_cloudwatch_alarms(instance_ids, alarm_name_pattern)` - Application health
- `rollback_patches(instance_ids)` - Restore via yum history undo
- `get_resource_dependencies(instance_id)` - Impact assessment via AWS Config + ELB health
- `get_maintenance_windows(environment)` - Query maintenance schedules
- `create_maintenance_window(name, schedule, duration, target_environment)` - Create windows
- `verify_and_proceed(instance_ids, environment)` - Automated verification workflow
- `generate_compliance_report(cve_id, environment, command_id, decision)` - Audit trails

**Behavior**:
- Handles ONE environment per invocation (phased rollout)
- Asynchronous workflow: execute → wait → check status → check health → proceed
- Two-layer health checks: infrastructure (SSM) + application (CloudWatch alarms)
- SLA-aware scheduling: EMERGENCY vs SCHEDULED based on maintenance windows
- Returns OUT_OF_SCOPE for security/CVE analysis/AWS service queries

**No `use_aws` access** - stays focused on patching domain

---

### Compliance Analyst Agent
**Role**: Executive reporting and audit trail analysis

**Tools**:
- `query_compliance_reports(days_back, severity, environment, sla_breaches_only)` - Query S3 reports

**Behavior**:
- Queries compliance reports from S3 (date-partitioned: YYYY/MM/DD/)
- Aggregates statistics (breach count, breach rate, team/severity breakdown)
- Trend analysis (current vs previous period)
- Executive-level insights with actionable recommendations
- Returns OUT_OF_SCOPE for patching/security/AWS service queries

**No `use_aws` access** - stays focused on compliance domain

---

## SLA-Aware Patching

### Policy Configuration
**File**: `agent/helper/config/config.json`

**Structure**:
```json
{
  "environment_aliases": {
    "dev": ["dev", "development", "non prod", "non production"],
    "staging": ["staging", "stage"],
    "prod": ["prod", "production"],
    "test": ["test", "testing", "qa"]
  },
  "frameworks": {
    "PCI-DSS": {"CRITICAL": 12, "HIGH": 48, "MEDIUM": 168, "LOW": 720},
    "SOC2": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720},
    "HIPAA": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}
  },
  "default": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}
}
```

### Implementation
- Instance tagging: `ComplianceFrameworks` tag drives SLA calculation
- Helper function: `_calculate_sla_requirement(frameworks, severity)` returns strictest SLA
- Decision logic: Compare maintenance window hours vs SLA hours → EMERGENCY or SCHEDULED
- Tools enhanced: `get_patch_compliance(environment, severity)` returns `sla_requirement` dict when severity provided

### Decision Algorithm
```python
if no_maintenance_window:
    decision = "EMERGENCY"
elif window_hours <= sla_hours:
    decision = "SCHEDULED"  # Wait for window
else:
    decision = "EMERGENCY"  # Delay unacceptable
```

---

## Workflow Patterns

### Information Query
```
User: "What is CVE-2025-12345?"
  ↓
Supervisor → Vulnerability Analyst
  ↓
get_vulnerability_findings(cve_id="CVE-2025-12345")
  ↓
Returns CVE analysis (CVSS, description, affected resources)
```

### AWS Service Query (Direct)
```
User: "List Lambda functions"
  ↓
Supervisor: Detects AWS service keyword
  ↓
use_aws(service_name="lambda", operation_name="list_functions", parameters={})
  ↓
Returns Lambda function list
```

### SLA-Aware Patching Workflow
```
User: "Handle CVE-2025-12345 (CRITICAL) in dev"
  ↓
Supervisor → Patch Manager
  ↓
1. get_patch_compliance(environment="dev", severity="CRITICAL")
   → Returns: instances[], sla_hours=12 (PCI-DSS), source="PCI-DSS"
  ↓
2. get_maintenance_windows(environment="dev")
   → Returns: next_execution=36 hours away
  ↓
3. Calculate: 36hr > 12hr → EMERGENCY (delay unacceptable)
  ↓
4. Display: "SLA: 12hr (PCI-DSS) | Window: 36hr | Decision: EMERGENCY"
  ↓
5. execute_patch_operation(instance_ids) → command_id
  ↓
User: "check status"
  ↓
6. get_command_status(command_id, instance_ids) → Success
  ↓
User: "check health" (after 2 min wait)
  ↓
7. check_instance_health(instance_ids) → All healthy
8. check_cloudwatch_alarms(instance_ids, "httpd") → No alarms
  ↓
9. generate_compliance_report(cve_id, environment="dev", command_id, decision="EMERGENCY")
   → Saved to S3: s3://patch-compliance-reports-{account}/2025/11/27/dev-CVE-2025-12345.json
  ↓
User: "proceed"
  ↓
[Repeat for staging → prod]
```

### Health Check Pattern (Two-Layer)
```
Infrastructure Layer:
  check_instance_health(instance_ids)
    ↓
  SSM DescribeInstanceInformation
    ↓
  Returns: connection_status, ping_status, platform_type

Application Layer:
  check_cloudwatch_alarms(instance_ids, "httpd")
    ↓
  CloudWatch DescribeAlarms(StateValue='ALARM')
    ↓
  Filter by InstanceId dimension
    ↓
  Returns: alarm_count, alarm_details, health_status, recommendation
```

### Rollback Pattern
```
Patch Manager: Detects alarms firing
  ↓
get_resource_dependencies(failed_instance)
  ↓
Analyzes impact (load balancers, target group health)
  ↓
User: Approves rollback
  ↓
rollback_patches(instance_ids)
  ↓
Waits 2 min for alarm evaluation
  ↓
check_cloudwatch_alarms(instance_ids, "httpd")
  ↓
Verify recovery
```

### OUT_OF_SCOPE Fallback
```
User: "Show EC2 instances" (to Vulnerability Analyst)
  ↓
Vulnerability Analyst: Recognizes infrastructure query
  ↓
Returns: OUT_OF_SCOPE: Infrastructure queries require Patch Manager
  ↓
Supervisor: Catches OUT_OF_SCOPE
  ↓
Retries with: use_aws(service_name="ec2", operation_name="describe_instances", ...)
  ↓
Returns EC2 instance list
```

---

## Infrastructure Components

### Networking Stack
- VPC with public/private subnets
- Internet Gateway
- NAT Gateway for private subnet internet access
- Route tables

### Security Stack
- Security groups for EC2 (SSH, HTTP)
- IAM roles for EC2 (SSM permissions)
- IAM roles for AgentCore (read-only + SSM execution)
- AWS Config for resource tracking
- S3 bucket for compliance reports (date-partitioned)
- SQS FIFO queue for workflow events (`patch-automation-workflow-events.fifo`)
- CloudWatch alarms for application monitoring (httpd)

### Demo Instances Stack
- 15 EC2 instances (Amazon Linux 2): 5 dev, 5 staging, 5 prod
- Tagged by: Environment, Team, Product, ComplianceFrameworks, Criticality
- SSM agent pre-installed
- Apache httpd with CloudWatch alarms (2 evaluation periods)
- Maintenance windows: dev (daily 1AM), staging (Tue 2AM), prod (1st of month 2AM)

### Permissions Stack (AgentCore IAM)
- Broad read-only access (List*, Get*, Describe*) for 25+ AWS services
- SSM execution permissions (SendCommand, GetCommandInvocation)
- Inspector read access (ListFindings)
- Config read access (GetResourceConfigHistory)
- CloudWatch read access (DescribeAlarms)
- S3 write access (PutObject for compliance reports)
- SQS access (SendMessage, ReceiveMessage, DeleteMessage for workflow events)
- No admin access (least privilege)

---

## Tool Implementation Details

### get_vulnerability_findings()
**Purpose**: Query AWS Inspector for vulnerabilities

**Parameters**:
- `cve_id` (optional): Specific CVE ID
- `severity` (optional): CRITICAL, HIGH, MEDIUM, LOW
- `environment` (optional): dev, staging, prod, test
- `limit` (default: 50): Max results in response payload

**Implementation**:
- Requires at least one filter (prevents unbounded queries)
- Paginates through all findings via `nextToken` for accurate total counts
- Filters by: findingStatus=ACTIVE, cveId, severity, resourceTags
- Normalizes environment names via `_normalize_environment()`
- Sorts by severity (DESC)
- Returns: findings[] (capped at `limit`), total_count (accurate across all pages)

### get_patch_compliance()
**Purpose**: Get patch states + SLA calculation

**Parameters**:
- `environment` (required): dev, staging, prod, test
- `instance_ids` (optional): Specific instances
- `severity` (optional): If provided, includes SLA calculation

**Implementation**:
- Discovers instances by environment tag using paginated `describe_instances` if not provided
- Normalizes environment names via `_normalize_environment()`
- Single API call: `ssm_client.describe_instance_patch_states()`
- Extracts ComplianceFrameworks tag from instances
- SLA calculation unions frameworks across ALL instances (not just first), uses strictest
- Calls `_calculate_sla_requirement(frameworks, severity)` if severity provided
- Returns: instances[], total_count, summary{}, sla_requirement{} (optional)

### execute_patch_operation()
**Purpose**: Execute patches asynchronously via SSM

**Parameters**:
- `instance_ids` (required): List of instance IDs
- `operation` (default: "Install"): Patch operation type
- `cve_ids` (optional): Specific CVEs (requires custom baseline)

**Implementation**:
- Calls `ssm_client.send_command(DocumentName='AWS-RunPatchBaseline')`
- Returns command_id immediately (async)
- Actual patching takes 5-10 minutes
- User checks status with get_command_status()

### get_command_status()
**Purpose**: Check async SSM command status

**Parameters**:
- `command_id` (required): SSM command ID
- `instance_ids` (required): List of instance IDs

**Implementation**:
- Parallel execution: ThreadPoolExecutor (max 10 threads)
- Per-instance: `ssm_client.get_command_invocation()`
- Aggregates: success count, failed count, in_progress count
- Returns: command_id, status, results[], summary{}

### check_cloudwatch_alarms()
**Purpose**: Query CloudWatch alarms for application health

**Parameters**:
- `instance_ids` (required): List of instance IDs
- `alarm_name_pattern` (optional): Filter by alarm name

**Implementation**:
- Single API call: `cloudwatch_client.describe_alarms(StateValue='ALARM')`
- Filters by InstanceId dimension (not alarm name)
- Limits to 5 alarms per instance (token optimization)
- Returns: alarm_count, alarm_details[], health_status, recommendation

**Performance**: O(1) API calls regardless of instance count

### get_resource_dependencies()
**Purpose**: Analyze patching impact via AWS Config + ELB target health

**Parameters**:
- `instance_id` (required): EC2 instance ID

**Implementation**:
- Queries AWS Config: `config_client.get_resource_config_history()`
- Extracts relationships: target groups, load balancers
- Checks target health: `elbv2_client.describe_target_health()`
- Determines `safe_to_patch`: true if other healthy targets exist in target groups
- Returns: dependencies[], target_group_health[], impact_level, safe_to_patch

### verify_and_proceed()
**Purpose**: Automated verification workflow (production mode)

**Parameters**:
- `instance_ids` (required): List of instance IDs
- `environment` (required): dev, staging, prod

**Implementation**:
- Waits 2 minutes for CloudWatch alarm evaluation
- Calls check_instance_health()
- Calls check_cloudwatch_alarms()
- Returns: verification_status, health_summary, recommendation

### generate_compliance_report()
**Purpose**: Create audit trail in S3

**Parameters**:
- `cve_id` (required): CVE ID
- `environment` (required): dev, staging, prod
- `command_id` (required): SSM command ID
- `decision` (required): EMERGENCY or SCHEDULED
- `severity` (optional): Vulnerability severity
- `cvss_score` (optional): CVSS score
- `sla_hours` (optional): SLA requirement hours
- `sla_source` (optional): SLA source framework

**Implementation**:
- Paginates `list_command_invocations` to discover all instance IDs (was capped at 50)
- Generates JSON report with timestamp, metadata, decision rationale
- Uploads to S3: `s3://patch-compliance-reports-{account}/YYYY/MM/DD/{env}-{cve}.json`
- S3 metadata: `cve-id`, `environment`, `severity`, `decision-type`, `sla-met`, `team`, `product`
- Date-partitioned for efficient querying
- Returns: report_id, s3_location, sla_met, summary

### query_compliance_reports()
**Purpose**: Query compliance reports from S3

**Parameters**:
- `days_back` (default: 7): How many days to look back
- `severity` (optional): Filter by severity
- `environment` (optional): Filter by environment
- `sla_breaches_only` (default: false): Only show confirmed breaches (sla_met=False)
- `limit` (default: 50): Max reports to return

**Implementation**:
- Uses month-prefix strategy for efficient S3 listing (few API calls vs per-day)
- Parallel `head_object` calls (ThreadPoolExecutor, max 10) for metadata
- Filters by severity, environment, sla_breaches_only
- `sla_breaches_only` only includes confirmed breaches (sla_met=False), not unknown
- Aggregates statistics: total reports, breach count, breach rate, by_severity, by_team, by_decision, sla_unknown
- Returns: reports[], statistics{}, total_count

---

## Data Flow

### Patch Execution (Asynchronous)
```
User → Patch Manager → SSM SendCommand → EC2 Instances
                    ↓
              Returns command_id
                    ↓
User: "check status"
                    ↓
        SSM GetCommandInvocation → Status
```

### Health Monitoring (Two-Layer)
```
Infrastructure Layer:
  SSM DescribeInstanceInformation → Health score

Application Layer:
  CloudWatch DescribeAlarms (StateValue='ALARM')
    ↓
  Filter by InstanceId dimension
    ↓
  Check alarm state → Recommendation
```

### Impact Assessment
```
AWS Config GetResourceConfigHistory
  ↓
Extract relationships (target groups, load balancers)
  ↓
ELBv2 DescribeTargetHealth
  ↓
Determine safe_to_patch (other healthy targets exist?)
  ↓
Impact level + recommendation
```

### Compliance Reporting
```
Patch Manager → generate_compliance_report()
  ↓
Create JSON report with metadata
  ↓
S3 PutObject (date-partitioned)
  ↓
Compliance Analyst → query_compliance_reports()
  ↓
List S3 objects by date prefix
  ↓
Download and aggregate
  ↓
Return statistics and trends
```

---

## Security Model

### IAM Permissions (Least Privilege)
- Read-only access to most AWS services (List*, Get*, Describe*)
- Write access only for:
  - SSM commands (SendCommand)
  - S3 compliance reports (PutObject)
- No infrastructure modification permissions
- No admin access

### Approval Gates
- Human approval required between environments (phased rollout)
- Rollback requires explicit user confirmation
- User controls workflow pace ("proceed" vs "verify and proceed")

### Audit Trail
- CloudWatch Logs: All agent actions, tool invocations
- SSM Command History: Patch operations with timestamps
- S3 Compliance Reports: Date-partitioned audit trail
- AWS Config: Resource configuration history

---

## Performance Characteristics

### Response Times
- Information query: 3-5 seconds
- Patch execution: 5-10 minutes (async)
- Command status check: 1-2 seconds
- Health check (infrastructure): 2-3 seconds
- Health check (CloudWatch): 1-2 seconds
- Rollback: 3-5 minutes

### Routing Performance
- 18-36% faster via keyword-based routing
- Eliminates wrong specialist calls
- OUT_OF_SCOPE fallback prevents retry loops

### Tool Optimization
- Parallel status checks: ThreadPoolExecutor (10 threads)
- Single API calls where possible (check_cloudwatch_alarms)
- Date-partitioned S3 for efficient compliance queries
- Pagination support in use_aws tool

---

## Monitoring & Observability

### CloudWatch Logs
- Agent conversations with full context
- Tool invocations with parameters
- Error messages with stack traces
- Performance metrics (request duration)

### CloudWatch Alarms
- Application health (httpd service)
- Per-instance with InstanceId dimension
- 2 evaluation periods (2 min to trigger)
- Integrated with health check workflow

### SSM Command History
- Patch execution logs
- Command status tracking
- Rollback operations

### AWS Config
- Resource configuration history
- Relationship tracking
- Impact assessment data source

### GenAI Observability Dashboard
- Token usage (input/output)
- Request latency
- Error rates
- Tool invocation frequency
- X-Ray distributed tracing

---

## Key Technical Decisions

### Asynchronous Patching
**Why**: SSM commands take 5-10 minutes
- Returns command_id immediately
- User checks status when ready
- Prevents timeout issues
- Enables phased rollout with approval gates

### Two-Layer Health Checks
**Why**: Catch both infrastructure and application failures
- Infrastructure (SSM): Instance connectivity
- Application (CloudWatch): Service health
- 2-minute wait for alarm evaluation
- Prevents false positives

### CloudWatch Alarm Integration
**Why**: Application-layer monitoring without new infrastructure
- Existing AWS primitive
- Dimension-based filtering (InstanceId)
- 2-minute evaluation period
- Integrated with rollback workflow

### OUT_OF_SCOPE Pattern
**Why**: Enforce separation of concerns
- Specialists reject wrong queries
- Supervisor retries with fallback
- Improves routing accuracy
- Reduces token waste

### Keyword-Based Routing
**Why**: Simple, fast, transparent
- No additional LLM calls
- Easy to debug
- 18-36% performance improvement
- OUT_OF_SCOPE provides safety net

### Built-in use_aws Tool
**Why**: Consistency and maintainability
- Standard library tool from strands_tools
- No custom implementation to maintain
- Only Supervisor has access
- Enforces specialist domain boundaries

### Helper Module Organization
**Why**: Clear separation of concerns
- Agent files at top level (4 agents)
- Helper utilities in subdirectory
- Easy to navigate and maintain
- Cleaner imports

---

## Memory & State Management

### AgentCore Memory Integration
- Shared memory across all agents via `helper/globals.py`
- Session-based conversation context
- Command ID tracking for async operations
- Phased rollout state persistence

### Memory Hook
**File**: `helper/memory_hooks.py`
- Automatic conversation history storage
- Cross-agent context sharing
- Session continuity across invocations
- Actor-based memory (per agent)

### Global State
**File**: `helper/globals.py`
```python
MEMORY_ID = None  # Shared across agents
SESSION_ID = None  # Per conversation
```

---

## Deployment Architecture

### AgentCore Runtime
- Managed service (no infrastructure to maintain)
- Automatic scaling
- Built-in observability
- CloudWatch Logs integration
- X-Ray tracing

### CDK Stacks (4 stacks)
1. **Networking**: VPC, subnets, NAT gateway
2. **Security**: Security groups, IAM roles, AWS Config, S3 bucket, SQS FIFO queue
3. **AgentCore Permissions**: IAM policies for agent (EC2, SSM, Inspector, Config, S3, SQS)
4. **Demo Instances**: 15 EC2 instances with tags

### Deployment Script
**File**: `deploy.sh`
- Modular targets: env, infra, agent, all
- Fast deployment (~2-3 minutes for agent)
- Automatic dependency management
- AWS profile support

---

## Testing & Validation

### Chat Client
**File**: `chat.sh` (wrapper for `chatbot-client/src/patch_client.py`)
- Direct boto3 integration (no CLI subprocess)
- Server-Sent Events (SSE) streaming
- Real-time tool indicators
- Rich terminal UI with markdown
- Session management

### Test Patterns
1. **Information queries**: "What is CVE-X?"
2. **AWS service queries**: "List Lambda functions"
3. **Patching workflow**: "Handle CVE-X in dev"
4. **Health checks**: "Check health of dev instances"
5. **Rollback**: "Rollback dev patches"
6. **Compliance**: "Show SLA breaches this week"

---

## Web UI Architecture

### Overview
Browser-based operations console replacing the terminal chat client. Dashboard with vulnerability cards, environment status, and embedded conversational agent.

### Stack
- **Frontend**: React + TypeScript + Vite + Cloudscape Design System
- **Backend**: FastAPI (Python) — thin proxy, not a backend
- **Streaming**: Server-Sent Events (SSE) for agent responses + workflow events
- **Font**: Inter (consistent across all components)

### API Endpoints
```
POST /api/chat              # Send message, returns SSE stream
GET  /api/dashboard         # All dashboard data (concurrent fetches, 30s cache)
GET  /api/health            # Config validation
```

### Layout
Two-column flex layout: Dashboard (72%, min 500px) on left, Chat on right. Collapse/expand with smooth CSS transition. Chat never moves to bottom — always stays on the right.

### Components (16 total)
- **DashboardPanel**: Tabbed layout (KPI bar + environment cards + vulnerability table + activity + compliance + explainability)
- **ChatPanel**: Message list + input + streaming indicators + approval buttons
- **WorkflowTracker**: Real-time patching progress (EnvPipeline + StepProgress + InstanceStatusTable + WorkflowHeader + WorkflowActions)
- **MessageBubble**: Markdown rendering + action button extraction
- **ErrorBoundary**: Isolated crash recovery per panel

### Hooks
- **useAgentStream**: SSE connection, parses text + tool_start + workflow events, forwards workflow events to dispatch
- **useWorkflowState**: Reducer managing workflow lifecycle (idle → active → complete), step tracking, ETA estimation
- **useDashboard**: Dashboard data fetching with 60s auto-refresh and manual refresh

### Workflow Event Flow
```
Agent (WorkflowTrackingHook) → SQS FIFO → FastAPI (drain) → SSE → useAgentStream → useWorkflowState → WorkflowTracker
```

---

## Testing & Validation

### Chat Client
**File**: `chat.sh` (wrapper for `chatbot-client/src/patch_client.py`)
- Direct boto3 integration (no CLI subprocess)
- Server-Sent Events (SSE) streaming
- Real-time tool indicators
- Rich terminal UI with markdown
- Session management

### Test Patterns
1. **Information queries**: "What is CVE-X?"
2. **AWS service queries**: "List Lambda functions"
3. **Patching workflow**: "Handle CVE-X in dev"
4. **Health checks**: "Check health of dev instances"
5. **Rollback**: "Rollback dev patches"
6. **Compliance**: "Show SLA breaches this week"

### Test Plan
See `doc/TEST_PLAN.md` — 74 test cases across 6 areas: dashboard data accuracy, workflow tracker event flow, agent chat interactions, dashboard↔agent consistency, UI/UX, and infrastructure.

---

## Future Enhancements

### Potential Improvements
1. **LLM-based routing** - If keyword routing shows >10% misrouting
2. **Multi-region support** - Currently single region (us-east-1)
3. **Windows support** - Currently Linux-only (yum-based rollback)
4. **Automated rollback** - Currently requires user approval
5. **Custom patch baselines** - For CVE-specific filtering
6. **Slack/Teams integration** - For notifications and approvals

### Monitoring Improvements
1. **Routing accuracy metrics** - Track OUT_OF_SCOPE frequency
2. **SLA compliance dashboard** - Real-time breach tracking
3. **Cost tracking** - Token usage per query type
4. **Performance profiling** - Tool execution times
