# Architecture Documentation

## Design Principles

### Strands Pattern Implementation

**Rich System Prompts**: Each agent has comprehensive prompts with methodologies, decision frameworks, and examples. The intelligence is in the prompts, not hardcoded logic.

**Simple Tools**: Tools are thin wrappers around AWS APIs. They don't contain business logic - agents decide when and how to use them.

**Agent-Driven Logic**: LLMs make decisions about tool usage, workflow sequencing, and error handling based on context.

**Natural Language Coordination**: Agents communicate through natural language, not rigid APIs or message formats.

## Agent Architecture

### Supervisor Agent
**Purpose**: Strategic coordination and workflow orchestration

**Responsibilities**:
- Route queries to appropriate specialist agents
- Coordinate multi-step workflows
- Track phased rollout progress across invocations
- Manage approval gates between environments

**Tools**: 
- `consult_vulnerability_analyst()` - Delegate to security specialist
- `consult_patch_manager()` - Delegate to operations specialist

**Key Behaviors**:
- Detects query type (information vs complete workflow)
- Maintains conversation context across invocations
- Handles "proceed" commands for phased rollout continuation

### Vulnerability Analyst Agent
**Purpose**: Security threat analysis and risk assessment

**Responsibilities**:
- CVE validation and enrichment
- CVSS scoring and threat classification
- Exploitation probability assessment
- Business impact evaluation

**Tools**:
- `query_inspector_findings()` - Get vulnerabilities from AWS Inspector
- `get_cve_details()` - Enrich CVE information

**Key Behaviors**:
- Provides security-focused analysis without operational concerns
- Assesses urgency and criticality
- Recommends response priority

### Patch Manager Agent
**Purpose**: Infrastructure operations and patch execution

**Responsibilities**:
- EC2 instance discovery and classification
- Environment segregation (dev/staging/prod)
- Patch execution via SSM
- Health monitoring and rollback

**Tools**:
- `get_instance_inventory()` - Discover EC2 instances
- `execute_patch_operation()` - Execute patches via SSM
- `get_patch_compliance()` - Check compliance status
- `check_instance_health()` - Verify instance health
- `rollback_patches()` - Restore previous packages
- `get_resource_dependencies()` - Get resource relationships

**Key Behaviors**:
- Handles ONE environment per invocation for phased rollout
- Uses memory to track which environments are complete
- Recommends rollback but requires user approval
- Provides demo mode for faster testing

## Workflow Patterns

### Information Query Pattern
```
User: "What is CVE-2025-12345?"
  ↓
Supervisor → Vulnerability Analyst
  ↓
Analyst queries Inspector, enriches CVE data
  ↓
Returns analysis to user
```

**Characteristics**:
- Single agent consultation
- No state changes
- Direct answer format

### Complete Workflow Pattern
```
User: "Handle CVE-2025-12345"
  ↓
Supervisor → Patch Manager
  ↓
Patch Manager: Patches dev environment
  ↓
Patch Manager: "Say 'proceed' to continue"
  ↓
User: "proceed"
  ↓
Supervisor → Patch Manager (continuation)
  ↓
Patch Manager: Patches staging environment
  ↓
[Repeat for production]
```

**Characteristics**:
- Multi-invocation workflow
- State maintained in memory
- Approval gates between phases
- Structured output format

### Rollback Pattern
```
Patch Manager: Executes patches
  ↓
Patch Manager: Checks health
  ↓
IF health < 80%:
  Patch Manager: "Rollback recommended"
  ↓
  User: "Rollback dev"
  ↓
  Patch Manager: Executes rollback
```

**Characteristics**:
- Human-in-the-loop decision
- Agent recommends, user approves
- Halts phased rollout on failure

## Infrastructure Architecture

### Networking Stack
- VPC with public and private subnets
- Internet Gateway for public access
- NAT Gateway for private subnet internet access
- Route tables and associations

### Security Stack
- Security groups for EC2 instances
- IAM roles for EC2 (SSM permissions)
- IAM roles for AgentCore
- AWS Config for resource tracking
- S3 bucket for Config snapshots

### Demo Instances Stack
- 15 EC2 instances (Amazon Linux 2)
- 5 dev, 5 staging, 5 prod
- Tagged by environment
- SSM agent pre-installed
- Apache web server for demo

### Permissions Stack
- AgentCore IAM policies
- EC2, SSM, Inspector, Config permissions
- Least privilege access

## Data Flow

### Vulnerability Discovery
```
AWS Inspector → Inspector API → Vulnerability Analyst → User
```

### Instance Discovery
```
EC2 API → Patch Manager → User
SSM API → Patch Manager → User
Config API → Patch Manager → User (for dependencies)
```

### Patch Execution
```
User → Patch Manager → SSM SendCommand → EC2 Instances
                    ↓
              Command Status → Patch Manager → User
```

### Health Monitoring
```
SSM DescribeInstanceInformation → Patch Manager
                                      ↓
                              Health Score Calculation
                                      ↓
                              Recommendation → User
```

## Security Model

### Least Privilege
- AgentCore role has minimal required permissions
- No write access to infrastructure (except SSM commands)
- Read-only access to Inspector, Config, EC2

### Approval Gates
- Human approval required between environments
- Rollback requires explicit user confirmation
- No automatic production changes

### Audit Trail
- All agent actions logged to CloudWatch
- SSM command history maintained
- Tool invocations tracked

## Scalability Considerations

### Current Limitations
- Single region (us-east-1)
- Manual approval doesn't scale to 100s of environments
- No parallel patching across environments
- Memory limited to single session

### Future Enhancements
- Multi-region support
- Automated approval for non-prod
- Parallel execution within environment
- Persistent memory across sessions

## Extension Points

### Adding New Agents
1. Create agent file in `agent/` directory
2. Define system prompt with role and capabilities
3. Import relevant tools
4. Add consultation tool to supervisor
5. Update supervisor routing logic

### Adding New Tools
1. Add function to `tools.py` with `@tool` decorator
2. Import in relevant agent file
3. Document in agent's system prompt
4. Test with agent invocations

### Adding New Workflows
1. Define workflow pattern in agent prompt
2. Add routing logic to supervisor
3. Implement state management if multi-invocation
4. Add testing examples

## Technology Choices

### Why Strands SDK?
- Prompt-driven pattern fits agentic workflows
- Simple tool registration
- Built-in memory management
- AgentCore integration

### Why AgentCore?
- Managed runtime (no infrastructure to maintain)
- Built-in observability
- Memory persistence
- AWS service integration

### Why CDK?
- Type-safe infrastructure definition
- Reusable constructs
- Easy to version and review
- Native AWS support

## Performance Characteristics

### Typical Response Times
- Information query: 3-5 seconds
- Patch execution: 5-10 minutes (actual patching)
- Health check: 2-3 seconds
- Rollback: 3-5 minutes

### Resource Usage
- AgentCore: Serverless (pay per invocation)
- EC2 instances: 15 t3.micro instances
- SSM: No additional cost
- CloudWatch Logs: ~1GB/month

## Monitoring and Observability

### CloudWatch Logs
- Agent conversations
- Tool invocations
- Error messages
- Performance metrics

### SSM Command History
- Patch execution logs
- Command status
- Output and errors

### AWS Config
- Resource configuration history
- Relationship tracking
- Compliance timeline
