# Compelling Demo Flow for Intelligent Patch Automation

## Core Narrative

**Problem Statement**: "We have 50 instances across 3 environments. CVE-2025-CRITICAL just dropped with active exploitation. What do we do?"

**Old Way**: 
- Manually check each instance for vulnerability
- Coordinate maintenance windows across teams
- Execute patches hoping nothing breaks
- No visibility into dependencies
- React to failures after the fact

**Our Way**: 
- Agent analyzes CVE and infrastructure automatically
- Makes risk-based decisions using maintenance windows and CVSS scores
- Executes phased rollout with dependency awareness
- Monitors application health (not just infrastructure)
- Detects failures and orchestrates rollback

---

## Demo Scenarios

### Scenario 1: Happy Path - Intelligent Scheduling (5 minutes)

**Setup**: CVE-2025-CRITICAL (CVSS 9.8) affecting nginx, active exploitation

**Flow**:
1. **Discovery**: "Handle CVE-2025-CRITICAL affecting nginx"
   - Agent queries Inspector/inventory
   - Identifies 15 affected instances (5 dev, 5 staging, 5 prod)
   - Shows organizational context (teams, compliance frameworks)

2. **Risk Analysis**: Agent checks maintenance windows
   - Dev: daily window in 12 hours
   - Staging: weekly window in 5 days  
   - Prod: monthly window in 20 days
   - CVSS 9.8 requires action within 24 hours

3. **Decision**: Emergency patching authorized
   - Dev: Within acceptable delay (12h < 24h) but critical severity → patch immediately
   - Staging: 5 days exceeds acceptable delay → emergency mode
   - Prod: 20 days unacceptable → emergency mode
   - Agent provides clear justification for each decision

4. **Execution**: Phased rollout
   - Patches dev → reports command ID
   - User: "check status" → Agent verifies success
   - User: "proceed" → Patches staging
   - User: "check status" → Agent verifies success
   - User: "proceed" → Patches prod

5. **Completion**: Compliance report generated
   - Saved to S3 with audit trail
   - Shows decision rationale, SLA compliance, affected systems

**Key Takeaway**: Agent makes intelligent, risk-based decisions and maintains audit trail

---

### Scenario 2: Failure Detection & Rollback (7 minutes)

**IMPORTANT NOTES FROM TESTING:**
- Agent requires explicit prompt: "check if httpd service is running" to detect application failures
- For full rollback, say "rollback dev environment" (not just "rollback dev too")
- Use unique session ID for each demo run to avoid context pollution

**Setup**: CVE-2025-APP-BREAK affecting httpd, patch causes application failure

**Flow**:
1. **Discovery & Decision**: Same as Scenario 1
   - Agent identifies affected instances
   - Makes emergency patching decision

2. **Dev Patching**: Executes successfully
   - Patches 5 dev instances
   - Infrastructure health check passes (SSM connectivity OK)
   - Application health check passes (httpd responding)

3. **Staging Patching**: Executes but causes failure
   - Patches 5 staging instances
   - Infrastructure health check passes
   - **Application health check FAILS** (httpd not responding on 3/5 instances)
   - Agent detects: "Staging health check failed: 3/5 instances unhealthy"

4. **Dependency Analysis** (NEW CAPABILITY):
   - Agent queries AWS Config for dependencies
   - Identifies: API Gateway → ALB → Staging instances → RDS
   - Shows impact: "API Gateway endpoints degraded, customer-facing APIs affected"

5. **Rollback Decision**:
   - Agent: "Staging patching resulted in application failures. Recommend rollback."
   - Shows failure details: httpd service stopped on i-xxx, i-yyy, i-zzz
   - User: "rollback staging"
   - Agent executes: `yum history undo` on all 5 staging instances

6. **Verification**:
   - Agent re-checks application health
   - All instances healthy after rollback
   - Agent: "Rollback successful. Staging environment restored."

7. **Root Cause & Next Steps**:
   - Agent: "Patching halted at staging. Dev remains patched, prod untouched."
   - Recommendation: "Investigate staging failures before proceeding to prod"
   - Compliance report documents the failure and rollback decision

**Key Takeaway**: Agent detects application-level failures, understands dependencies, and orchestrates safe rollback

---

## Implementation Requirements

### 1. Application Health Checks (CRITICAL GAP)

**Current State**: 
- `check_instance_health()` only verifies SSM connectivity
- No application-layer validation

**Required Enhancement**:
```python
@tool
def check_application_health(
    instance_ids: list,
    service_name: str = "httpd"  # or nginx, tomcat, etc.
) -> dict:
    """
    Check application health on instances.
    
    Verifies:
    1. Service is running (systemctl status)
    2. Service is responding (curl localhost:80)
    3. No errors in recent logs
    
    Returns health status per instance with details.
    """
    # Implementation:
    # 1. Use SSM Run Command to check service status
    # 2. Run: systemctl is-active httpd
    # 3. Run: curl -f http://localhost:80 (or appropriate health endpoint)
    # 4. Run: journalctl -u httpd -n 50 --no-pager | grep -i error
    # 5. Aggregate results per instance
```

**Why This Matters**: 
- Infrastructure health (SSM) != Application health (service responding)
- Patches can break applications even if OS is healthy
- Need app-layer checks to detect real failures

---

### 2. Dependency Awareness (ENHANCEMENT)

**Current State**:
- Agent patches instances in isolation
- No visibility into what depends on these instances
- Can't assess blast radius of failures

**Required Enhancement**:
```python
@tool
def analyze_dependencies(
    instance_ids: list
) -> dict:
    """
    Analyze infrastructure and application dependencies.
    
    Uses AWS Config to identify:
    1. Load balancers targeting these instances
    2. API Gateways routing to these instances
    3. Databases these instances connect to
    4. Other services depending on these instances
    
    Returns dependency graph and impact assessment.
    """
    # Implementation:
    # 1. Query AWS Config: get-resource-config-history
    # 2. Find relationships: LoadBalancer -> TargetGroup -> Instances
    # 3. Find relationships: APIGateway -> VPCLink -> NLB -> Instances
    # 4. Build dependency tree
    # 5. Assess impact: "If these instances fail, X services affected"
```

**Why This Matters**:
- Understand blast radius before patching
- Prioritize rollback when customer-facing services affected
- Make informed risk decisions

---

### 3. Simulating Application Failure (FOR DEMO)

**Option A: Break Service Manually**
```bash
# After patching staging, run this on 3 instances:
aws ssm send-command \
  --instance-ids i-xxx i-yyy i-zzz \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["systemctl stop httpd"]'
```

**Option B: Install Bad Configuration**
```bash
# Create a "patch" that breaks httpd config
aws ssm send-command \
  --instance-ids <staging-instances> \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["echo \"InvalidDirective\" >> /etc/httpd/conf/httpd.conf", "systemctl restart httpd"]'
```

**Option C: Simulate High Error Rate**
```bash
# Make httpd return 500 errors
aws ssm send-command \
  --instance-ids <staging-instances> \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["echo \"ErrorDocument 500 /error.html\" >> /etc/httpd/conf/httpd.conf", "systemctl reload httpd"]'
```

**Recommendation**: Option A (stop service) - cleanest, most obvious failure

---

## Demo Script

### Scenario 1: Happy Path (5 min)

```
[SETUP: All instances healthy, maintenance windows configured]

YOU: "We just received an alert about CVE-2025-CRITICAL with CVSS 9.8 
      and active exploitation affecting nginx. Handle this immediately."

AGENT: [Analyzes CVE, checks maintenance windows, makes emergency decision]
       "Emergency patching authorized. Patching dev environment..."
       [Shows command ID, affected instances]

YOU: "check status"

AGENT: "Dev patching complete: 5/5 instances successful"

YOU: "proceed"

AGENT: "Patching staging environment..."
       [Shows command ID]

YOU: "check status"

AGENT: "Staging patching complete: 5/5 instances successful"

YOU: "proceed"

AGENT: "Patching production environment..."
       [Shows command ID]

YOU: "check status"

AGENT: "Production patching complete: 5/5 instances successful"
       "Compliance report generated: s3://patch-compliance-reports/.../CVE-2025-CRITICAL.json"

[SHOW: S3 report with decision rationale, timeline, affected systems]
```

**Time**: 5 minutes (assuming instant status checks for demo)

---

### Scenario 2: Failure & Rollback (7 min)

```
[SETUP: Staging instances will fail after patching - use inject-failure.sh script]

YOU: "Handle CVE-2025-APP-BREAK affecting httpd in all environments"

AGENT: "Emergency patching authorized. Patching dev environment..."

YOU: "proceed to staging"

AGENT: "Patching staging environment..."

[BEHIND THE SCENES: Run ./demo-scripts/inject-failure.sh to stop httpd on 3 instances]

YOU: "check if httpd service is running on staging instances"

AGENT: "⚠️ Application health check FAILED: 3/5 instances not responding"
       "Affected instances: i-xxx, i-yyy, i-zzz"
       "Service httpd is not running on these instances"
       "RECOMMENDED ACTION: ROLLBACK STAGING ENVIRONMENT"

YOU: "What's the impact?"

AGENT: [Queries AWS Config]
       "Dependency analysis:"
       "- ALB staging-alb has 3/5 unhealthy targets"
       "- API Gateway api-prod routes to this ALB"
       "- Customer-facing APIs degraded"
       "Recommendation: Rollback staging immediately"

YOU: "rollback staging"

AGENT: "Executing rollback on 5 staging instances..."
       [Shows command ID]
       "Rollback complete. Verifying health..."
       "✅ All staging instances healthy after rollback"
       "Dev environment remains patched. Prod environment untouched."
       
       "Rolling back staging only. Dev remains patched for comparison."
       "If you believe the patch itself is fundamentally broken, say 'rollback dev environment'."

[OPTIONAL EXTENSION: Demonstrate full rollback if patch is fundamentally broken]

YOU: "rollback dev environment"

AGENT: "Executing rollback on 5 dev instances..."
       [Shows command ID]
       "Rollback complete. Verifying health..."
       "✅ All dev instances healthy after rollback"
       "Rolled back staging and dev. All environments restored to pre-patch state."
       "Recommendation: Investigate root cause before attempting to patch again"

[SHOW: Compliance report documenting failure and rollback decision]
```

**Time**: 7 minutes

---

## Key Messages for Audience

1. **Intelligence Over Automation**: 
   - Not just executing scripts
   - Making risk-based decisions using CVSS, maintenance windows, compliance requirements

2. **Safety Through Verification**:
   - Application-layer health checks catch real failures
   - Dependency awareness shows blast radius
   - Automated rollback restores service quickly

3. **Audit Trail & Compliance**:
   - Every decision documented with rationale
   - S3 reports queryable for compliance audits
   - SLA tracking per compliance framework

4. **Integration with Existing Processes**:
   - Respects maintenance windows
   - Works with AWS Config for dependencies
   - Generates reports for existing tools to consume

---

## Technical Gaps to Address

### Priority 1: Application Health Checks
- [ ] Implement `check_application_health()` tool
- [ ] Support httpd, nginx service checks
- [ ] Add HTTP endpoint validation (curl localhost:80)
- [ ] Parse service logs for errors
- [ ] Update `check_instance_health()` to call application checks

### Priority 2: Dependency Analysis
- [ ] Implement `analyze_dependencies()` tool
- [ ] Query AWS Config for resource relationships
- [ ] Build dependency graph (ALB → instances, API Gateway → ALB)
- [ ] Calculate blast radius (X services affected)
- [ ] Add to agent prompt: check dependencies before patching

### Priority 3: Demo Infrastructure
- [ ] Script to break httpd on staging instances (for Scenario 2)
- [ ] Verify rollback works with broken services
- [ ] Test full flow: patch → break → detect → rollback → verify

### Priority 4: Enhanced Reporting
- [ ] Add dependency information to compliance reports
- [ ] Include health check results in reports
- [ ] Document rollback decisions with root cause

---

## Testing Notes & Known Issues

### Tested on: 2025-10-15

**What Works:**
- ✅ Emergency patching decision based on CVSS and maintenance windows
- ✅ Phased rollout (dev → staging) with demo mode
- ✅ Failure injection script successfully breaks httpd on staging
- ✅ Application health check detects httpd service failures
- ✅ Rollback staging environment successfully
- ✅ Agent offers option to rollback lower environments

**Known Issues:**
1. **Application health check not automatic** - Must explicitly say "check if httpd service is running"
   - Workaround: Updated prompt to make it automatic (needs testing)
   
2. **"rollback dev too" not recognized** - Agent doesn't understand this phrase
   - Workaround: Say "rollback dev environment" instead
   - Updated prompt to recognize multiple variations

3. **Context pollution** - Agent memory gets confused after multiple interactions
   - Workaround: Use unique session ID for each demo run: `--session-id demo-$(uuidgen)`

### Demo Preparation Checklist

Before running demo:
- [ ] Deploy latest agent code: `agentcore launch --auto-update-on-conflict`
- [ ] Verify staging instances are running and have httpd installed
- [ ] Test failure injection script: `./demo-scripts/inject-failure.sh`
- [ ] Prepare unique session ID: `SESSION_ID="demo-$(uuidgen | tr '[:upper:]' '[:lower:]')"`
- [ ] Have AWS console open to show S3 compliance reports

### Exact Commands for Demo

```bash
# Start demo with unique session
SESSION_ID="demo-$(uuidgen | tr '[:upper:]' '[:lower:]')"

# Step 1: Patch dev
agentcore invoke '{"prompt": "Handle CVE-2025-DEMO (CVSS 9.0) affecting httpd in dev"}' --session-id "$SESSION_ID"

# Step 2: Proceed to staging
agentcore invoke '{"prompt": "proceed to staging"}' --session-id "$SESSION_ID"

# Step 3: Inject failure (in separate terminal)
./demo-scripts/inject-failure.sh

# Step 4: Check application health
agentcore invoke '{"prompt": "check if httpd service is running on staging instances"}' --session-id "$SESSION_ID"

# Step 5: Rollback staging
agentcore invoke '{"prompt": "rollback staging"}' --session-id "$SESSION_ID"

# Step 6 (Optional): Rollback dev too
agentcore invoke '{"prompt": "rollback dev environment"}' --session-id "$SESSION_ID"
```

---

## Questions to Answer

1. **Application Health Checks**: 
   - What services do we check? (httpd, nginx, tomcat?)
   - What constitutes "healthy"? (service running + responding to requests?)
   - How do we handle partial failures? (3/5 instances unhealthy)

2. **Dependency Analysis**:
   - How deep do we go? (direct dependencies only or transitive?)
   - What AWS resources do we track? (ALB, API Gateway, RDS, what else?)
   - How do we present this to the user? (text description or visual graph?)

3. **Rollback Scope**:
   - Do we rollback all instances or just failed ones?
   - Do we rollback previous environments? (if staging fails, rollback dev too?)
   - What if rollback fails?

4. **Demo Logistics**:
   - Do we run both scenarios or just one?
   - How do we handle timing? (5-10 min patch waits)
   - Do we pre-record or run live?

---

## Success Metrics

**Scenario 1 Success**: 
- Agent makes correct emergency vs scheduled decision
- Phased rollout completes without errors
- Compliance report generated with full audit trail

**Scenario 2 Success**:
- Agent detects application failure (not just infrastructure)
- Agent identifies dependencies and impact
- Rollback executes successfully
- System restored to healthy state

**Overall Demo Success**:
- Audience understands the intelligence (not just automation)
- Clear differentiation from "just run a script"
- Compelling narrative with real-world relevance
- Technical depth without overwhelming complexity

---

## Next Steps

1. **Implement application health checks** (Priority 1)
2. **Test rollback with broken services** (validate Scenario 2)
3. **Implement dependency analysis** (Priority 2)
4. **Script the demo flow** (practice timing)
5. **Prepare failure injection** (staging break script)
6. **Rehearse both scenarios** (5 min + 7 min = 12 min total)

---

## Alternative: Single Unified Scenario (10 min)

If time is limited, combine both scenarios:

1. Start with CVE announcement (1 min)
2. Agent analyzes and decides emergency patching (1 min)
3. Patch dev → verify → proceed (2 min)
4. Patch staging → **failure detected** → show dependencies (3 min)
5. Rollback staging → verify → explain next steps (3 min)

**This shows both intelligence AND safety in one flow.**

Recommendation: **Use unified scenario for main demo, keep separate scenarios for deep-dive Q&A.**
