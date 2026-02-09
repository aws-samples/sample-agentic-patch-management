# Technology Stack

## Core Technologies

### AWS Strands SDK (Python)

**Version**: 1.4.0+

**Purpose**: Framework for building model-first applications with agentic AI

**Key Features**:
- Agent abstraction layer
- Tool definition and execution
- Memory integration
- Conversation management
- Built-in AWS service integration

**Why Chosen**:
- AWS-native framework (first-party support)
- Designed for Bedrock AgentCore deployment
- Simplifies agent development
- Handles agentic loop automatically
- Production-ready patterns

**Usage in Project**:
```python
from strands_agents import Agent, Tool
from strands_agents_tools import use_aws

# Define agent
agent = Agent(
    name="patch_manager",
    instructions="You are a patch management specialist...",
    tools=[execute_patch_operation, check_instance_health],
    model="anthropic.claude-sonnet-4-5-v2:0"
)

# Define tool
@Tool
def execute_patch_operation(instance_ids: list[str]) -> dict:
    """Execute patches on EC2 instances"""
    # Implementation
```

**Dependencies**:
- `strands-agents>=1.4.0`
- `strands-agents-tools>=0.2.3`

---

### Amazon Bedrock AgentCore

**Purpose**: Managed runtime for deploying and scaling agents

**Key Features**:
- Serverless agent hosting
- Automatic scaling
- Built-in observability (CloudWatch + X-Ray)
- Memory management (STM/LTM)
- HTTPS API endpoints
- Container-based deployment

**Why Chosen**:
- No infrastructure to manage
- Production-grade scaling
- Native AWS integration
- Built-in security (IAM, VPC)
- Cost-effective (pay per request)

**Configuration**:
```yaml
# .bedrock_agentcore.yaml
default_agent: intelligent_patch_automation
agents:
  intelligent_patch_automation:
    entrypoint: supervisor.py
    platform: linux/arm64
    container_runtime: finch
    network_mode: PUBLIC
    memory:
      mode: STM_ONLY
      event_expiry_days: 7
    observability:
      enabled: true
```

**Deployment**:
```bash
# Deploy agent
agentcore launch --auto-update-on-conflict

# Check status
agentcore status

# Invoke
agentcore invoke '{"prompt": "Hello"}'
```

---

### Claude Sonnet 4.5 (Amazon Bedrock)

**Model ID**: `anthropic.claude-sonnet-4-5-v2:0`

**Purpose**: Foundation model for reasoning and decision-making

**Key Capabilities**:
- Natural language understanding
- Tool selection and execution
- Multi-turn conversations
- Context retention (200K tokens)
- Function calling

**Why Chosen**:
- Best-in-class reasoning
- Excellent tool use
- Large context window
- Fast response times
- Cost-effective

**Usage Pattern**:
```python
# Agent uses Claude for reasoning
agent = Agent(
    name="supervisor",
    model="anthropic.claude-sonnet-4-5-v2:0",
    instructions="You are an intelligent orchestrator...",
    tools=[consult_vulnerability_analyst, consult_patch_manager]
)
```

**Token Usage**:
- Average query: 500-1000 tokens
- Patching workflow: 2000-3000 tokens
- Cost: ~$0.003 per 1K input tokens, ~$0.015 per 1K output tokens

---

## AWS Services

### AWS Systems Manager (SSM)

**Purpose**: Patch execution, compliance tracking, maintenance windows

**Key APIs Used**:
- `send_command()` - Execute patches asynchronously
- `get_command_invocation()` - Check patch status
- `describe_instance_patch_states()` - Get compliance data
- `describe_instance_information()` - Check connectivity
- `describe_maintenance_windows()` - Query schedules
- `create_maintenance_window()` - Create schedules

**Why Chosen**:
- Native AWS patch management
- Asynchronous execution
- Built-in compliance tracking
- Maintenance window support
- No agent installation required (SSM agent pre-installed)

**Usage Example**:
```python
import boto3

ssm = boto3.client('ssm')

# Execute patches
response = ssm.send_command(
    InstanceIds=['i-1234567890abcdef0'],
    DocumentName='AWS-RunPatchBaseline',
    Parameters={'Operation': ['Install']}
)
command_id = response['Command']['CommandId']

# Check status
status = ssm.get_command_invocation(
    CommandId=command_id,
    InstanceId='i-1234567890abcdef0'
)
```

---

### AWS Inspector

**Purpose**: Vulnerability discovery and CVE tracking

**Key APIs Used**:
- `list_findings()` - Query active vulnerabilities
- Filter by: severity, CVE ID, resource tags, finding status

**Why Chosen**:
- Continuous vulnerability scanning
- CVE database integration
- CVSS scoring
- Environment-based filtering (tags)
- No additional infrastructure

**Usage Example**:
```python
import boto3

inspector = boto3.client('inspector2')

# Query critical vulnerabilities
response = inspector.list_findings(
    filterCriteria={
        'severity': [{'comparison': 'EQUALS', 'value': 'CRITICAL'}],
        'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}],
        'resourceTags': [{'comparison': 'EQUALS', 'key': 'Environment', 'value': 'dev'}]
    },
    sortCriteria={'field': 'SEVERITY', 'sortOrder': 'DESC'},
    maxResults=50
)
```

---

### AWS Config

**Purpose**: Resource dependency tracking and blast radius analysis

**Key APIs Used**:
- `get_resource_config_history()` - Get resource relationships
- `describe_configuration_recorders()` - Verify Config enabled

**Why Chosen**:
- Tracks resource relationships (EC2 → Target Group → Load Balancer)
- Historical configuration data
- Compliance tracking
- No additional setup (enabled in security stack)

**Usage Example**:
```python
import boto3

config = boto3.client('config')

# Get resource dependencies
response = config.get_resource_config_history(
    resourceType='AWS::EC2::Instance',
    resourceId='i-1234567890abcdef0',
    limit=1
)

# Extract relationships
relationships = response['configurationItems'][0].get('relationships', [])
```

---

### Amazon CloudWatch

**Purpose**: Application health monitoring and logging

**Key APIs Used**:
- `describe_alarms()` - Query alarm state
- `put_log_events()` - Agent logging (automatic)
- Filter by: StateValue, Dimensions (InstanceId)

**Why Chosen**:
- Application-layer health monitoring
- Existing AWS primitive
- Dimension-based filtering
- Integrated with rollback workflow
- No new infrastructure

**Usage Example**:
```python
import boto3

cloudwatch = boto3.client('cloudwatch')

# Check for firing alarms
response = cloudwatch.describe_alarms(
    StateValue='ALARM',
    MaxRecords=100
)

# Filter by instance
instance_alarms = [
    alarm for alarm in response['MetricAlarms']
    if any(d['Name'] == 'InstanceId' and d['Value'] == 'i-123' 
           for d in alarm.get('Dimensions', []))
]
```

---

### Amazon S3

**Purpose**: Compliance report storage and audit trail

**Key APIs Used**:
- `put_object()` - Store compliance reports
- `list_objects_v2()` - Query reports by date
- `get_object()` - Download reports

**Why Chosen**:
- Durable storage (11 9's)
- Date partitioning (YYYY/MM/DD/)
- Versioning support
- Lifecycle policies
- Cost-effective

**Usage Example**:
```python
import boto3
import json
from datetime import datetime

s3 = boto3.client('s3')

# Store compliance report
report = {
    'cve_id': 'CVE-2025-38527',
    'environment': 'dev',
    'timestamp': datetime.utcnow().isoformat(),
    'decision': 'EMERGENCY'
}

date_prefix = datetime.utcnow().strftime('%Y/%m/%d')
s3.put_object(
    Bucket='patch-compliance-reports-123456789012',
    Key=f'{date_prefix}/dev-CVE-2025-38527.json',
    Body=json.dumps(report)
)
```

---

### Amazon EC2

**Purpose**: Target instances for patching

**Key APIs Used**:
- `describe_instances()` - Query instances by tags
- `describe_tags()` - Get instance metadata

**Why Chosen**:
- Standard compute platform
- Tag-based filtering (Environment, ComplianceFrameworks)
- SSM agent pre-installed (Amazon Linux 2)
- CloudWatch integration

**Instance Configuration**:
- AMI: Amazon Linux 2
- Instance Type: t3.micro (demo), t3.medium+ (production)
- Tags: Environment, Team, Product, ComplianceFrameworks, Criticality
- User Data: Install httpd, configure CloudWatch agent

---

## Development Tools

### Python 3.9+

**Purpose**: Primary development language

**Why Chosen**:
- Strands SDK is Python-based
- Excellent AWS SDK (boto3)
- Rich ecosystem for data processing
- Type hints for better tooling
- Async/await support

**Key Libraries**:
```python
# requirements.txt
bedrock-agentcore-starter-toolkit
bedrock-agentcore
strands-agents>=1.4.0
strands-agents-tools>=0.2.3
boto3
croniter  # For maintenance window calculations
```

---

### AWS CDK (TypeScript)

**Version**: Latest

**Purpose**: Infrastructure as Code

**Why Chosen**:
- Type-safe infrastructure definitions
- Reusable constructs
- CloudFormation under the hood
- Excellent AWS service support
- Fast deployment

**Stack Structure**:
```typescript
// infra/bin/app.ts
const app = new cdk.App();

new NetworkingStack(app, 'PatchAutomationNetworking');
new SecurityStack(app, 'PatchAutomationSecurity');
new AgentCorePermissionsStack(app, 'PatchAutomationAgentCorePermissions');
new VulnerabilityDrivenPatchingStack(app, 'PatchAutomationVulnerabilityDriven');
```

**Dependencies**:
```json
{
  "dependencies": {
    "aws-cdk-lib": "^2.x",
    "constructs": "^10.x"
  }
}
```

---

### Docker + Finch

**Purpose**: Container runtime for AgentCore deployment

**Why Finch**:
- Open-source Docker alternative
- Native macOS support (M1/M2)
- Faster builds on ARM
- Compatible with Docker images

**Dockerfile**:
```dockerfile
FROM public.ecr.aws/docker/library/python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "supervisor.py"]
```

---

### Git

**Purpose**: Version control

**Repository Structure**:
```
.git/
├── main branch (production)
├── reporting-capability (feature branch)
└── performance_and_bug_fixes (merged)
```

**Key Commits**:
- `de0eda6` - Minor changes (HEAD)
- `88e7d7b` - Updates routing priority
- `3dbc963` - Refactor: Move helper modules to helper/ folder
- `23e3a5f` - Merge reporting-capability

---

## Configuration Management

### Environment Variables

**File**: `.env` (not committed, use `.env.example`)

```bash
# AWS Configuration
AWS_PROFILE=ri25-demo
AWS_REGION=us-east-1
AWS_ACCOUNT_ID=125140434174

# Agent Configuration
AGENT_NAME=intelligent-patch-automation
EXECUTION_ROLE_NAME=IntelligentPatchAutomation

# AgentCore Configuration
BYPASS_TOOL_CONSENT=true
```

---

### SLA Policy Configuration

**File**: `agent/config/config.json`

```json
{
  "frameworks": {
    "PCI-DSS": {
      "CRITICAL": 12,
      "HIGH": 48,
      "MEDIUM": 168,
      "LOW": 720
    },
    "SOC2": {
      "CRITICAL": 24,
      "HIGH": 72,
      "MEDIUM": 168,
      "LOW": 720
    }
  },
  "default": {
    "CRITICAL": 24,
    "HIGH": 72,
    "MEDIUM": 168,
    "LOW": 720
  }
}
```

**Why JSON**:
- Easy to modify without code changes
- Supports comments (via description fields)
- Validated at runtime
- Can be externalized to S3/Parameter Store

---

### AgentCore Configuration

**File**: `agent/.bedrock_agentcore.yaml`

```yaml
default_agent: intelligent_patch_automation
agents:
  intelligent_patch_automation:
    entrypoint: supervisor.py
    platform: linux/arm64
    memory:
      mode: STM_ONLY
      event_expiry_days: 7
    observability:
      enabled: true
```

---

## Development Setup

### Prerequisites

1. **Python 3.9+**
   ```bash
   python3 --version
   ```

2. **Node.js 18+** (for CDK)
   ```bash
   node --version
   npm --version
   ```

3. **AWS CLI**
   ```bash
   aws --version
   aws configure --profile ri25-demo
   ```

4. **Bedrock AgentCore CLI**
   ```bash
   pip install bedrock-agentcore
   agentcore --version
   ```

5. **Docker or Finch**
   ```bash
   finch --version
   # or
   docker --version
   ```

---

### Local Development Workflow

1. **Setup Environment**
   ```bash
   ./deploy.sh env
   source venv/bin/activate
   ```

2. **Deploy Infrastructure**
   ```bash
   ./deploy.sh infra
   ```

3. **Deploy Agent**
   ```bash
   ./deploy.sh agent
   ```

4. **Test Agent**
   ```bash
   ./chat.sh
   # or
   agentcore invoke '{"prompt": "Hello"}'
   ```

5. **View Logs**
   ```bash
   aws logs tail /aws/bedrock-agentcore/runtimes/intelligent_patch_automation-TzHdGwD6sO-DEFAULT --follow
   ```

---

## Testing Tools

### Chat Client

**File**: `chatbot-client/src/patch_client.py`

**Purpose**: Interactive testing with rich terminal UI

**Features**:
- Server-Sent Events (SSE) streaming
- Real-time tool indicators
- Markdown rendering
- Session management
- AWS profile support

**Usage**:
```bash
# Interactive mode
./chat.sh

# Single message
./chat.sh -m "What vulnerabilities do we have?"

# Custom profile
./chat.sh -p ri25-demo
```

**Dependencies**:
```python
# chatbot-client/requirements.txt
boto3
pyyaml
rich  # Terminal UI
```

---

### AgentCore CLI

**Purpose**: Direct agent invocation for testing

**Commands**:
```bash
# Deploy agent
agentcore launch --auto-update-on-conflict

# Check status
agentcore status

# Invoke agent
agentcore invoke '{"prompt": "List EC2 instances"}'

# View logs
agentcore logs --follow

# Delete agent
agentcore delete
```

---

## Observability Stack

### CloudWatch Logs

**Log Group**: `/aws/bedrock-agentcore/runtimes/intelligent_patch_automation-TzHdGwD6sO-DEFAULT`

**What's Logged**:
- Agent conversations
- Tool invocations
- Error messages
- Performance metrics

**Query Examples**:
```bash
# Tail logs
aws logs tail /aws/bedrock-agentcore/runtimes/intelligent_patch_automation-TzHdGwD6sO-DEFAULT --follow

# Filter errors
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/intelligent_patch_automation-TzHdGwD6sO-DEFAULT" \
  --filter-pattern "ERROR"

# Performance query (CloudWatch Insights)
fields @timestamp, @message 
| filter @message like /Request completed/ 
| parse @message "Request completed in *ms" as duration
| stats count() as requests, avg(duration) as avg_ms
```

---

### AWS X-Ray

**Purpose**: Distributed tracing

**What's Traced**:
- Agent invocations
- Tool executions
- AWS API calls
- Latency breakdown

**Access**: CloudWatch Console → X-Ray Traces

---

### GenAI Observability Dashboard

**Purpose**: Agent-specific metrics

**URL**: `https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-observability/agent-core`

**Metrics**:
- Request/response times
- Token usage (input/output)
- Tool invocation frequency
- Error rates
- Cost tracking

---

## Deployment Pipeline

### Manual Deployment (Current)

```bash
# Full deployment
./deploy.sh all

# Incremental updates
./deploy.sh agent  # Agent code only (2-3 min)
./deploy.sh infra  # Infrastructure only (5-10 min)
```

---

### Future: CI/CD Pipeline

**Proposed Stack**:
- **Source**: GitHub
- **Build**: AWS CodeBuild
- **Deploy**: AWS CodePipeline
- **Test**: Automated integration tests

**Pipeline Stages**:
1. Source: Git push triggers pipeline
2. Build: Docker image build + push to ECR
3. Test: Run integration tests
4. Deploy: AgentCore launch with new image
5. Verify: Health checks + smoke tests

---

## Known Constraints

### Technical Constraints

1. **Python 3.9+ Required**: Strands SDK dependency
2. **ARM64 Platform**: Optimized for AWS Graviton
3. **us-east-1 Only**: Single region deployment
4. **Linux Only**: yum-based rollback (Amazon Linux/RHEL)
5. **SSM Agent Required**: Pre-installed on Amazon Linux 2

### Service Limits

1. **SSM Concurrent Commands**: 50 per account (can be increased)
2. **Inspector Findings**: 10,000 per list_findings call
3. **CloudWatch Alarms**: 5,000 per region (can be increased)
4. **S3 Objects**: Unlimited (but list_objects_v2 returns max 1,000)
5. **AgentCore Memory**: 7-day event expiry (STM_ONLY mode)

### Cost Considerations

1. **Bedrock AgentCore**: Pay per request (~$0.01 per 1K requests)
2. **Claude Sonnet 4.5**: ~$0.003 per 1K input tokens, ~$0.015 per 1K output tokens
3. **EC2 Instances**: 15 × t3.micro = ~$100/month (demo)
4. **NAT Gateway**: ~$32/month + data transfer
5. **S3 Storage**: Negligible (compliance reports)
6. **CloudWatch Logs**: ~$0.50/GB ingested

**Total Monthly Cost (Demo)**: ~$150-200

---

## Development Best Practices

### Code Organization

```
agent/
├── supervisor.py              # Top-level agents
├── vulnerability_agent.py
├── patch_manager.py
├── compliance_analyst.py
├── helper/                    # Utilities
│   ├── tools.py
│   ├── agent_factory.py
│   ├── memory_hooks.py
│   └── globals.py
└── config/                    # Configuration
    └── config.json
```

**Rationale**:
- Agents at top level (easy to find)
- Utilities in helper/ (clear separation)
- Configuration externalized

---

### Tool Definition Pattern

```python
from strands_agents import Tool

@Tool
def execute_patch_operation(
    instance_ids: list[str],
    operation: str = "Install"
) -> dict:
    """
    Execute patches on EC2 instances via Systems Manager.
    
    Args:
        instance_ids: List of EC2 instance IDs to patch
        operation: Patch operation (Install, Scan)
    
    Returns:
        dict: Command ID and status
    """
    # Implementation
    return {
        "command_id": "abc-123",
        "status": "InProgress",
        "message": "Patching initiated"
    }
```

**Best Practices**:
- Type hints for all parameters
- Docstring with Args and Returns
- Structured return values (dict)
- Error handling with try/except

---

### Error Handling Pattern

```python
import boto3
from botocore.exceptions import ClientError

def get_vulnerability_findings(cve_id: str = None) -> dict:
    try:
        inspector = boto3.client('inspector2')
        response = inspector.list_findings(...)
        return {"findings": response['findings']}
    
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'AccessDeniedException':
            return {"error": "Insufficient permissions"}
        else:
            return {"error": f"AWS API error: {error_code}"}
    
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}
```

**Best Practices**:
- Catch specific exceptions first
- Return structured errors (dict)
- Include error context
- Log errors for debugging

---

## Security Best Practices

1. **Never Commit Secrets**
   - Use `.env` for local development
   - Use AWS Secrets Manager for production
   - Add `.env` to `.gitignore`

2. **Least Privilege IAM**
   - Read-only access by default
   - Write access only where needed
   - No admin permissions

3. **Input Validation**
   - Validate all tool parameters
   - Sanitize user input
   - Use type hints

4. **Audit Trail**
   - Log all agent actions
   - Store compliance reports
   - Enable CloudWatch Logs

5. **Network Security**
   - Private subnets for EC2
   - Security groups restrict traffic
   - NAT Gateway for outbound only
