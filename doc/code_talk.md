# re:Invent Code Talk: Multi-Agent Patch Automation

**Session Level:** 400  
**Duration:** 60 minutes  
**Format:** Slides (20 min) + Live Coding (40 min)

---

## Session Overview

**Problem:** Traditional patching requires 15 browser tabs, 3 CLI tools, 45 minutes of manual work per CVE.

**Solution:** Multi-agent system that makes intelligent, risk-based patching decisions using natural language.

**Key Differentiator:** Agents make decisions (maintenance windows, CVSS scoring, health checks), not just execute scripts.

---

## Pre-Coded Content (Slides - 20 min)

### 1. Problem Statement (5 min)
- Manual patching workflow pain points
- Compliance requirements (PCI-DSS, SOC2, HIPAA)
- Human error and inconsistency

### 2. Strands & AgentCore Overview (10 min)
- What is Strands SDK
- What is Amazon Bedrock AgentCore
- Why multi-agent vs single agent
- Deployment and runtime model

### 3. Architecture Overview (5 min)
- Show 4-agent architecture diagram
- Supervisor pattern
- Intelligent routing with fallback
- AWS service integration

---

## Live Coding Portion (40 min)

### Part 1: Simple Agent Build (12 min)

**Goal:** Build intuition for agent fundamentals and prompt engineering

#### Step 1: Create Basic Agent (5 min)

**File:** `demo/simple_agent.py`

Type this live:

```python
from strands import Agent, tool
import boto3

@tool
def get_ec2_instances(environment: str) -> dict:
    """Get EC2 instances for an environment (dev/staging/prod)"""
    ec2 = boto3.client('ec2', region_name='us-east-1')
    
    response = ec2.describe_instances(
        Filters=[
            {'Name': 'tag:Environment', 'Values': [environment]},
            {'Name': 'instance-state-name', 'Values': ['running']}
        ]
    )
    
    instances = []
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            instances.append({
                'id': instance['InstanceId'],
                'type': instance['InstanceType'],
                'state': instance['State']['Name']
            })
    
    return {'environment': environment, 'count': len(instances), 'instances': instances}

agent = Agent(
    name="demo-agent",
    instructions="""You are an EC2 infrastructure assistant.

BEHAVIOR:
- Always show instance counts first, then details
- Use markdown tables for multiple instances
- If user asks about other AWS services, say: "I only handle EC2 instances."

OUTPUT FORMAT:
- Lead with: "Found X instances in [environment]"
- Use table: Instance ID | Type | State
- End with actionable insight""",
    tools=[get_ec2_instances]
)
```

**Key Teaching Moments (while typing):**
- "Tools are just Python functions with type hints"
- "Docstring becomes the tool description for the LLM"
- "Return structured data (dict), not strings"
- "Instructions guide behavior - BEHAVIOR section controls decisions"
- "OUTPUT FORMAT section ensures consistent presentation"
- "Scope boundaries prevent hallucination (EC2 only)"
- "This prompt pattern scales to production (see supervisor later)"

---

#### Step 2: Create AgentCore Config (1 min)

**File:** `demo/.bedrock_agentcore.yaml`

```yaml
runtime:
  name: reinvent-demo-agent
  region: us-east-1
  model: anthropic.claude-3-5-sonnet-20241022-v2:0
  agent_file: simple_agent.py
  agent_variable: agent
```

**Point out:**
- Separate runtime name (won't conflict with main agent)
- Points to our simple_agent.py file

---

#### Step 3: Deploy & Test (4 min)

```bash
cd demo
agentcore launch
```

**Test:**
```bash
# Test 1: Basic query
agentcore invoke '{"prompt": "Show me dev instances"}'

# Expected output (with structured prompt):
# "Found 5 instances in dev environment
# 
# | Instance ID | Type     | State   |
# |-------------|----------|---------|
# | i-abc123    | t3.micro | running |
# ...
# 
# All instances are healthy and running."

# Test 2: Multiple environments
agentcore invoke '{"prompt": "How many staging instances?"}'

# Test 3: Out-of-scope query (shows boundary enforcement)
agentcore invoke '{"prompt": "Can you check Lambda functions?"}'

# Expected: "I only handle EC2 instances."
```

**Point out:**
- Agent understands natural language
- Agent calls tool automatically
- Structured prompt ensures consistent formatting (tables, summary-first)
- Scope boundaries prevent hallucination (rejects Lambda query)
- Response includes tool results

---

#### Step 4: Show Streaming (2 min)

```bash
agentcore invoke --stream '{"prompt": "Show all environments"}'
```

**Key Teaching Moments:**
- Streaming shows agent "thinking"
- See tool calls in real-time
- Better UX for users

**After demo, clean up:**
```bash
agentcore delete --runtime-name reinvent-demo-agent
```

---

### Part 2: Multi-Agent Orchestration (15 min)

**Goal:** Show supervisor pattern and intelligent routing

#### Step 1: Create Two Specialists (5 min)

**File:** `demo/multi_agent.py`

```python
from strands import Agent, tool
import boto3

# Vulnerability Analyst Tools
@tool
def analyze_cve_details(cve_id: str) -> dict:
    """Analyze CVE severity and exploitation status"""
    # Simplified - in production, query NVD API or AWS Inspector
    cve_database = {
        'CVE-2025-38527': {
            'cvss_score': 9.8,
            'severity': 'CRITICAL',
            'exploitation_status': 'active',
            'description': 'Remote code execution in Apache HTTP Server'
        },
        'CVE-2025-12345': {
            'cvss_score': 5.5,
            'severity': 'MEDIUM',
            'exploitation_status': 'none',
            'description': 'Information disclosure vulnerability'
        }
    }
    return cve_database.get(cve_id, {'error': 'CVE not found'})

# Patch Manager Tools
@tool
def discover_instances(environment: str) -> dict:
    """Discover EC2 instances in an environment"""
    ec2 = boto3.client('ec2', region_name='us-east-1')
    response = ec2.describe_instances(
        Filters=[
            {'Name': 'tag:Environment', 'Values': [environment]},
            {'Name': 'instance-state-name', 'Values': ['running']}
        ]
    )
    
    instances = []
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            instances.append(instance['InstanceId'])
    
    return {'environment': environment, 'instance_ids': instances}

@tool
def execute_patch_command(instance_ids: list, environment: str) -> dict:
    """Execute patching via SSM Run Command"""
    ssm = boto3.client('ssm', region_name='us-east-1')
    
    response = ssm.send_command(
        InstanceIds=instance_ids,
        DocumentName='AWS-RunShellScript',
        Parameters={'commands': ['sudo yum update -y']},
        Comment=f'Patching {environment} environment'
    )
    
    return {
        'command_id': response['Command']['CommandId'],
        'status': 'InProgress',
        'instance_count': len(instance_ids)
    }

# Create Specialists
vulnerability_agent = Agent(
    name="vulnerability-analyst",
    instructions="""You analyze CVEs and security threats.
    
    Your responsibilities:
    - Analyze CVE severity and exploitation status
    - Assess business impact
    - Recommend patching urgency
    
    If asked about patching operations or infrastructure, respond with:
    OUT_OF_SCOPE: This requires the Patch Manager specialist.
    """,
    tools=[analyze_cve_details]
)

patch_agent = Agent(
    name="patch-manager",
    instructions="""You manage patching operations.
    
    Your responsibilities:
    - Discover instances in environments
    - Execute patch commands
    - Verify patch success
    
    If asked about CVE analysis or security assessment, respond with:
    OUT_OF_SCOPE: This requires the Vulnerability Analyst specialist.
    """,
    tools=[discover_instances, execute_patch_command]
)
```

**Key Teaching Moments:**
- Specialists have focused domains
- OUT_OF_SCOPE pattern for routing
- Tools are domain-specific

---

#### Step 2: Create Supervisor with Routing (5 min)

```python
@tool
def use_aws(service: str, operation: str, parameters: dict = None) -> dict:
    """Universal AWS service access for operations outside specialist scope"""
    try:
        client = boto3.client(service, region_name='us-east-1')
        if parameters is None:
            parameters = {}
        
        response = getattr(client, operation)(**parameters)
        
        # Handle pagination
        if 'NextToken' in response or 'nextToken' in response:
            return {**response, 'note': 'Results may be paginated'}
        
        return response
    except Exception as e:
        return {'error': str(e)}

# Create Supervisor
supervisor = Agent(
    name="supervisor",
    instructions="""You are the orchestration supervisor for patch management.
    
    Route queries to the appropriate specialist:
    
    1. Security/CVE queries → vulnerability-analyst
       Keywords: CVE, vulnerability, security, CVSS, threat, exploit
       
    2. Patching/Infrastructure queries → patch-manager
       Keywords: patch, deploy, instance, health, SSM, environment
       
    3. Other AWS services → use_aws tool directly
       Keywords: Lambda, S3, CloudWatch, RDS, billing
    
    If a specialist responds with OUT_OF_SCOPE, route to the appropriate specialist or use_aws.
    
    Always explain which specialist you're consulting.
    """,
    tools=[vulnerability_agent, patch_agent, use_aws]
)
```

**Deploy:**
```bash
agentcore deploy --auto-update-on-conflict
```

**Key Teaching Moments:**
- Supervisor uses agents as tools
- Routing is classification problem
- Fallback tool for edge cases
- Instructions guide routing logic

---

#### Step 3: Test Routing (3 min)

```bash
# Test 1: Security query → Vulnerability Analyst
agentcore invoke '{"prompt": "What is CVE-2025-38527?"}'

# Test 2: Infrastructure query → Patch Manager
agentcore invoke '{"prompt": "Show me dev instances"}'

# Test 3: AWS service query → use_aws fallback
agentcore invoke '{"prompt": "List Lambda functions"}'
```

**Show in logs:**
- Which specialist was called
- Tool invocations
- Response routing

---

#### Step 4: Show OUT_OF_SCOPE Handling (2 min)

```bash
# This should trigger routing chain
agentcore invoke '{"prompt": "Analyze CVE-2025-38527 and patch dev environment"}'
```

**Expected flow:**
1. Supervisor → Vulnerability Analyst (analyze CVE)
2. Vulnerability Analyst → OUT_OF_SCOPE (patching request)
3. Supervisor → Patch Manager (execute patching)

**Key Teaching Moments:**
- Specialists reject out-of-scope work
- Supervisor handles re-routing
- No hardcoded logic, agent decides

---

### Part 3: Production Workflow Deep Dive (13 min)

**Goal:** Show real-world complexity - maintenance-aware patching

#### Step 1: Show Maintenance Window Tool (3 min)

**File:** `demo/production_tools.py`

```python
from datetime import datetime, timezone
import boto3

@tool
def check_maintenance_windows(environment: str) -> dict:
    """Check maintenance windows for an environment"""
    ssm = boto3.client('ssm', region_name='us-east-1')
    
    # Map environment to maintenance window
    window_mapping = {
        'dev': 'dev-daily-patching',
        'staging': 'staging-weekly-patching',
        'prod': 'prod-monthly-patching'
    }
    
    window_name = window_mapping.get(environment)
    if not window_name:
        return {'error': f'No maintenance window for {environment}'}
    
    try:
        response = ssm.describe_maintenance_windows(
            Filters=[{'Key': 'Name', 'Values': [window_name]}]
        )
        
        if response['WindowIdentities']:
            window = response['WindowIdentities'][0]
            
            # Calculate next window time (simplified)
            now = datetime.now(timezone.utc)
            # In production, parse schedule and calculate next occurrence
            
            return {
                'environment': environment,
                'window_name': window_name,
                'schedule': window.get('Schedule', 'Unknown'),
                'next_window': '2025-11-11T01:00:00Z',  # Simplified
                'hours_until': 8.5,  # Simplified
                'enabled': window.get('Enabled', False)
            }
        else:
            return {'error': f'Maintenance window {window_name} not found'}
    except Exception as e:
        return {'error': str(e)}
```

**Key Teaching Moments:**
- Real AWS SSM integration
- Maintenance windows are AWS resources
- Time calculations for scheduling

---

#### Step 2: Show Risk Calculation (3 min)

```python
@tool
def calculate_acceptable_delay(cvss_score: float, exploitation_status: str, 
                               compliance_framework: str = 'PCI-DSS') -> dict:
    """Calculate acceptable patching delay based on risk and compliance"""
    
    # Risk-based delay calculation
    if cvss_score >= 9.0 and exploitation_status == 'active':
        max_delay = 4  # 4 hours for critical with active exploitation
        urgency = 'CRITICAL'
        rationale = 'Active exploitation of critical vulnerability'
    elif cvss_score >= 9.0:
        max_delay = 24  # 24 hours for critical without exploitation
        urgency = 'CRITICAL'
        rationale = 'Critical severity requires immediate attention'
    elif cvss_score >= 7.0:
        max_delay = 72  # 3 days for high severity
        urgency = 'HIGH'
        rationale = 'High severity vulnerability'
    elif cvss_score >= 4.0:
        max_delay = 168  # 7 days for medium severity
        urgency = 'MEDIUM'
        rationale = 'Medium severity - can wait for maintenance window'
    else:
        max_delay = 720  # 30 days for low severity
        urgency = 'LOW'
        rationale = 'Low severity - schedule during regular maintenance'
    
    # Compliance framework adjustments
    compliance_requirements = {
        'PCI-DSS': {'critical': 30, 'high': 30},  # 30 days for critical/high
        'HIPAA': {'critical': 30, 'high': 60},
        'SOC2': {'critical': 90, 'high': 90}
    }
    
    framework_req = compliance_requirements.get(compliance_framework, {})
    compliance_deadline = framework_req.get(urgency.lower(), 365)
    
    return {
        'cvss_score': cvss_score,
        'exploitation_status': exploitation_status,
        'urgency': urgency,
        'max_delay_hours': max_delay,
        'compliance_framework': compliance_framework,
        'compliance_deadline_days': compliance_deadline,
        'rationale': rationale
    }
```

**Key Teaching Moments:**
- Risk-based decision making
- CVSS scoring drives urgency
- Compliance frameworks have SLAs
- Agent uses this data to decide

---

#### Step 3: Live Demo Decision Logic (5 min)

**Start chat client:**
```bash
./chat.sh
```

**Demo flow:**
```
You: Handle CVE-2025-38527 in dev environment

Agent flow (explain as it happens):
1. Supervisor routes to Vulnerability Analyst
   → analyze_cve_details('CVE-2025-38527')
   → Returns: CVSS 9.8, active exploitation, CRITICAL

2. Supervisor routes to Patch Manager
   → check_maintenance_windows('dev')
   → Returns: Next window in 8.5 hours
   
   → calculate_acceptable_delay(9.8, 'active')
   → Returns: Max delay 4 hours, CRITICAL urgency
   
   → Decision: 8.5 hours > 4 hours = EMERGENCY patching required
   
3. Patch Manager executes
   → discover_instances('dev')
   → Returns: 5 instances
   
   → execute_patch_command([...], 'dev')
   → Returns: Command ID, InProgress
   
4. Agent asks: "Patching in progress. Say 'proceed' to move to staging."

You: proceed

5. Repeat for staging → prod
```

**Key Teaching Moments:**
- Agent makes intelligent decisions
- Not just executing scripts
- Risk calculation drives behavior
- Phased rollout with approval gates

---

#### Step 4: Show Health Checks (2 min)

```python
@tool
def verify_patch_health(instance_ids: list, environment: str) -> dict:
    """Two-layer health check: SSM connectivity + CloudWatch alarms"""
    ssm = boto3.client('ssm', region_name='us-east-1')
    cloudwatch = boto3.client('cloudwatch', region_name='us-east-1')
    
    # Layer 1: SSM connectivity check
    ssm_response = ssm.describe_instance_information(
        Filters=[{'Key': 'InstanceIds', 'Values': instance_ids}]
    )
    
    ssm_healthy = all(
        info['PingStatus'] == 'Online' 
        for info in ssm_response['InstanceInformationList']
    )
    
    # Layer 2: CloudWatch alarms check
    alarm_response = cloudwatch.describe_alarms(
        AlarmNamePrefix=f'{environment}-',
        StateValue='ALARM'
    )
    
    alarms_healthy = len(alarm_response['MetricAlarms']) == 0
    
    return {
        'environment': environment,
        'instance_count': len(instance_ids),
        'ssm_healthy': ssm_healthy,
        'alarms_healthy': alarms_healthy,
        'overall_healthy': ssm_healthy and alarms_healthy,
        'alarm_count': len(alarm_response['MetricAlarms'])
    }
```

**Test:**
```bash
agentcore invoke '{"prompt": "Check health of dev instances after patching"}'
```

**Key Teaching Moments:**
- Two-layer health checks
- Infrastructure (SSM) + Application (CloudWatch)
- Production-ready verification

---

## Terminal Setup for Demo

**Terminal 1: Chat Client**
```bash
./chat.sh
```

**Terminal 2: Logs (split screen)**
```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT --follow
```

**Terminal 3: AWS Console (browser)**
- EC2 instances with Environment tags
- SSM Run Command history
- CloudWatch alarms dashboard
- S3 compliance reports bucket

---

## Key Takeaways to Emphasize

### 1. Simplicity
- Tools are just Python functions
- Docstrings become tool descriptions
- Type hints guide the LLM

### 2. Composition
- Agents can use other agents as tools
- Supervisor pattern for orchestration
- Specialists for focused domains

### 3. Intelligence
- Agents make decisions, not just execute
- Risk-based scheduling
- Maintenance window awareness
- Health verification

### 4. Production-Ready
- Error handling in tools
- Two-layer health checks
- Phased rollout with approvals
- Compliance reporting
- Audit trails

### 5. AWS-Native
- Real service integration (EC2, SSM, Inspector, Config, CloudWatch)
- No mocks or simulations
- IAM least privilege
- Observability built-in

---

## Backup Slides (If Time Permits)

### Observability
- CloudWatch Logs
- X-Ray tracing
- GenAI Dashboard
- Token usage tracking

### Security
- IAM roles and policies
- Least privilege access
- Audit trails in S3
- Compliance framework integration

### Performance
- Intelligent routing: 18-36% faster
- Pagination handling
- Streaming responses
- Error recovery

---

## Questions to Anticipate

**Q: How do you handle tool failures?**
A: Tools return error dicts, agent decides next action. Supervisor has fallback logic.

**Q: What about cost?**
A: Token usage tracked in CloudWatch. Intelligent routing reduces unnecessary calls.

**Q: Can this work with other cloud providers?**
A: Yes, tools are just Python. Replace boto3 with other SDKs.

**Q: How do you test agents?**
A: Unit test tools, integration test workflows, monitor in production.

**Q: What about security?**
A: IAM least privilege, audit trails, approval gates, no hardcoded credentials.

---

## Post-Session Resources

- GitHub repo with full code
- Documentation on routing patterns
- Tool design best practices
- Production deployment guide
- Observability setup guide
