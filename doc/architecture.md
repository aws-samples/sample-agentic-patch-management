# Technical Architecture

## Recent Improvements

### SLA-Aware Patching
**Feature**: Compliance framework-based SLA calculation and enforcement

**Implementation**:
- External policy file: `agent/config/sla_policy.json` defines SLA hours by framework and severity
- Helper function: `_calculate_sla_requirement()` in tools.py (DRY principle)
- Instance tagging: `ComplianceFrameworks` tag drives SLA calculation (e.g., "PCI-DSS,SOC2")
- Strictest SLA wins: If instance has multiple frameworks, minimum SLA hours applies
- Decision logic: Compare maintenance window hours vs SLA hours → EMERGENCY or SCHEDULED

**SLA Policy Structure**:
```json
{
  "frameworks": {
    "PCI-DSS": {"CRITICAL": 12, "HIGH": 48, "MEDIUM": 168, "LOW": 720},
    "SOC2": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720},
    "HIPAA": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}
  },
  "default": {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}
}
```

**Tools Enhanced**:
- `get_instance_inventory(severity=X)`: Returns `sla_requirement` dict when severity provided
- `generate_compliance_report()`: Includes SLA tracking with breach detection

### Prompt Optimization
**Achievement**: 71% reduction in total prompt size (425 → 124 lines)

**Changes**:
- Patch Manager: 280 → 61 lines (removed redundant examples, defensive over-specification)
- Vulnerability Agent: 120 → 38 lines (flexible workflow vs rigid 4-step process)
- Supervisor: Already optimal at 25 lines
- Removed: Step-by-step instructions, multiple workflow examples, demo mode explanations
- Kept: Essential scope, tools, workflow, output format

**Philosophy**: Trust the model, provide guidance not instructions

### Code Quality Improvements
**DRY Refactoring**:
- Extracted `_calculate_sla_requirement()` helper function
- Eliminated duplicate SLA logic in `get_instance_inventory()` and `generate_compliance_report()`
- Single source of truth for SLA calculation

**Output Format**:
- Terminal-friendly: Emoji, markdown tables, clean sections
- No rigid formatting rules (removed "NO ## headers" restrictions)
- Trust model to format clearly and readably

**Chat Client**:
- Fixed streaming: Proper newlines when switching between tools
- Reset `response_started` flag on tool switch
- No more merged output lines

---

## Agent System Design

### Supervisor Agent
**Role**: Intelligent routing and workflow orchestration

**Tools**:
- `consult_vulnerability_analyst()` - Delegate to security specialist
- `consult_patch_manager()` - Delegate to operations specialist  
- `use_aws()` - Fallback for AWS services outside specialist scope

**Routing Logic**:
- Security queries (CVE, vulnerability, CVSS, threat) → Vulnerability Analyst
- Infrastructure queries (patch, instance, health, SSM) → Patch Manager
- AWS service queries (Lambda, S3, CloudWatch) → use_aws (direct)
- OUT_OF_SCOPE responses → Retry with use_aws fallback

**Performance**: 18-36% faster via keyword-based classification (eliminates wrong specialist calls)

---

### Vulnerability Analyst Agent
**Role**: Security threat analysis and risk assessment

**Tools**:
- `get_vulnerability_findings()` - Query AWS Inspector
- `use_aws()` - Fallback for non-security queries

**Behavior**:
- CVSS scoring and threat classification
- Returns OUT_OF_SCOPE for infrastructure/patching queries
- Provides security-focused analysis only

---

### Patch Manager Agent
**Role**: Infrastructure operations and patch execution

**Tools**:
- `get_instance_inventory()` - Discover EC2 instances
- `execute_patch_operation()` - Execute patches via SSM (async, returns command_id)
- `get_command_status()` - Check async operation status
- `check_instance_health()` - Verify SSM connectivity (infrastructure layer)
- `check_cloudwatch_alarms()` - Monitor application health via CloudWatch alarms
- `check_service_status()` - Direct service check via SSM (optional)
- `rollback_patches()` - Restore via yum history undo
- `get_resource_dependencies()` - Analyze blast radius via AWS Config
- `get_maintenance_windows()` - Query maintenance schedules
- `generate_compliance_report()` - Create audit trails
- `use_aws()` - Fallback for non-patching queries

**Behavior**:
- Handles ONE environment per invocation (phased rollout)
- Asynchronous workflow: execute → wait → check status → check health → proceed
- Two-layer health checks: infrastructure (SSM) + application (CloudWatch alarms)
- Waits 2 minutes after patching for CloudWatch alarm evaluation
- Returns OUT_OF_SCOPE for security/CVE analysis queries

---

## Workflow Patterns

### Information Query
```
User: "What is CVE-2025-12345?"
  ↓
Supervisor → Vulnerability Analyst
  ↓
Analyst queries Inspector
  ↓
Returns analysis
```

### Asynchronous Patch Workflow
```
User: "Handle CVE-2025-12345 in dev"
  ↓
Supervisor → Patch Manager
  ↓
Patch Manager: execute_patch_operation() → command_id
  ↓
User: "check status"
  ↓
Patch Manager: get_command_status(command_id)
  ↓
IF Success:
  User: "check health" (after 2 min wait)
  ↓
  Patch Manager: check_instance_health()
                 check_cloudwatch_alarms(instance_ids, "httpd")
  ↓
  IF Healthy:
    User: "proceed"
    ↓
    [Repeat for staging → prod]
```

### Health Check Pattern
```
Patch Manager: check_instance_health(instance_ids)
               ↓ (Infrastructure: SSM connectivity)
               
Patch Manager: check_cloudwatch_alarms(instance_ids, "httpd")
               ↓ (Application: CloudWatch alarms)
               
IF alarms firing:
  Patch Manager: get_resource_dependencies(failed_instance)
                 ↓ (Blast radius analysis)
  Patch Manager: Recommend rollback
ELSE:
  Patch Manager: Proceed to next environment
```

### Rollback Pattern
```
Patch Manager: Detects alarms firing
  ↓
Patch Manager: Analyzes blast radius
  ↓
User: Approves rollback
  ↓
Patch Manager: rollback_patches(instance_ids)
  ↓
Patch Manager: Waits 2 min
  ↓
Patch Manager: check_cloudwatch_alarms() (verify recovery)
```

### Intelligent Routing
```
User: "List my Lambda functions"
  ↓
Supervisor: Detects AWS service query
  ↓
Supervisor: use_aws(query) (no specialist needed)
  ↓
Returns result
```

---

## Infrastructure Components

### Networking Stack
- VPC with public/private subnets
- Internet Gateway
- NAT Gateway
- Route tables

### Security Stack
- Security groups for EC2
- IAM roles for EC2 (SSM permissions)
- IAM roles for AgentCore (read-only + SSM execution)
- AWS Config for resource tracking
- S3 bucket for compliance reports
- CloudWatch alarms for application monitoring

### Demo Instances Stack
- 15 EC2 instances (Amazon Linux 2): 5 dev, 5 staging, 5 prod
- Tagged by environment
- SSM agent pre-installed
- Apache httpd with CloudWatch alarms

### Permissions Stack
- AgentCore IAM policies
- Broad read-only access (List*, Get*, Describe*) for 25+ AWS services
- SSM execution permissions (patching, rollback)
- No admin access (least privilege)

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

### Blast Radius Analysis
```
AWS Config GetResourceConfigHistory
  ↓
Extract relationships (target groups, load balancers)
  ↓
ELBv2 DescribeTargetHealth
  ↓
Impact assessment
```

---

## Tool Implementation Details

### check_cloudwatch_alarms()
**Purpose**: Query CloudWatch alarms for application health

**Implementation**:
- Single API call: `describe_alarms(StateValue='ALARM')`
- Filters by InstanceId dimension (not alarm name)
- Supports optional alarm_name_pattern filter
- Limits to 5 alarms per instance (token optimization)
- Returns: alarm count, alarm details, health status, recommendation

**Performance**: O(1) API calls regardless of instance count

### execute_patch_operation()
**Purpose**: Execute patches asynchronously via SSM

**Implementation**:
- Calls `ssm_client.send_command()`
- Returns command_id immediately
- Actual patching takes 5-10 minutes
- User checks status with get_command_status()

### get_resource_dependencies()
**Purpose**: Analyze blast radius via AWS Config

**Implementation**:
- Queries AWS Config for resource relationships
- Extracts load balancer associations
- Checks target group health status
- Returns: dependencies, load balancer health, blast radius assessment

---

## Security Model

### IAM Permissions
- Read-only access to most AWS services (List*, Get*, Describe*)
- Write access only for SSM commands
- No infrastructure modification permissions
- No admin access

### Approval Gates
- Human approval required between environments
- Rollback requires explicit user confirmation
- User controls workflow pace

### Audit Trail
- CloudWatch Logs: All agent actions
- SSM Command History: Patch operations
- S3: Compliance reports

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
- 18-36% faster via intelligent routing
- Eliminates wrong specialist calls
- OUT_OF_SCOPE fallback prevents retry loops

---

## Monitoring

### CloudWatch Logs
- Agent conversations
- Tool invocations
- Error messages with stack traces
- Performance metrics (ENABLE_PROFILING flag)

### CloudWatch Alarms
- Application health (httpd service)
- Per-instance with InstanceId dimension
- 2 evaluation periods (2 min to trigger)

### SSM Command History
- Patch execution logs
- Command status
- Rollback operations

### AWS Config
- Resource configuration history
- Relationship tracking
- Blast radius data source

---

## Key Technical Decisions

### Asynchronous Patching
- SSM commands take 5-10 minutes
- Returns command_id immediately
- User checks status when ready
- Prevents timeout issues

### Two-Layer Health Checks
- Infrastructure (SSM): Instance connectivity
- Application (CloudWatch): Service health
- Catches both infrastructure and application failures

### CloudWatch Alarm Integration
- Application-layer monitoring
- Existing AWS primitive (no new infrastructure)
- 2-minute evaluation period
- Dimension-based filtering (InstanceId)

### OUT_OF_SCOPE Pattern
- Specialists reject wrong queries
- Supervisor retries with fallback
- Improves routing accuracy
- Reduces token waste
