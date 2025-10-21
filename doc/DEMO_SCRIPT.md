# Live Demo Script (10 minutes)

## Pre-Demo Setup (5 min before presentation)

```bash
cd /Users/bhatprav/Documents/Workspace/ContentCreation/reinvent25-cop407-agentic-patch-management

# Verify deployment
./deploy.sh agent

# Start chat client
./chat.sh
```

**Verify:** Chat client starts without errors

---

## Opening (30 seconds)

**Say:** "Traditional patching requires 15 browser tabs, 3 CLI tools, and 45 minutes of manual work. Let me show you a different approach."

**Show:** Terminal with `./chat.sh` running

---

## Scene 1: Discovery (2 minutes)

**Say:** "Let's start by asking what vulnerabilities we have..."

**Type:** 
```
What critical vulnerabilities do we have?
```

**Expected Output:**
- 🔍 Consulting Vulnerability Analyst...
- List of CVEs with CVSS scores
- Affected instance counts

**Talking Points:**
- Agent automatically routed to Vulnerability Analyst specialist
- Real data from AWS Inspector
- Shows severity, exploitability, affected resources

**If it fails:** "Let me show you a pre-recorded example..." (have backup)

---

## Scene 2: Intelligent Decision Making (3 minutes)

**Say:** "Now let's handle that critical CVE. Watch how the agent makes risk-based decisions using SLA policies..."

**Type:**
```
Handle CVE-2025-38527 in dev environment
```

**Expected Output:**
- 🛠️ Consulting Patch Manager...
- SLA calculation: "SLA: 12hr (PCI-DSS) | Window: 24hr | Decision: SCHEDULED"
- OR "SLA: 12hr (PCI-DSS) | Window: 96hr | Decision: EMERGENCY"
- Patching initiated with command ID
- Instructions to check status

**Talking Points:**
- Agent automatically checks SLA requirements from compliance frameworks (PCI-DSS, SOC2, HIPAA)
- Compares maintenance window timing vs SLA deadline
- Risk calculation: If window exceeds SLA → EMERGENCY patching
- Phased rollout: dev → staging → prod
- Patching is asynchronous (returns command ID)
- SLA policies externalized in JSON config (easy to customize)

**Type when prompted:**
```
check status
```

**Expected:** Patch status (Success/InProgress/Failed), instructions to check health

**Type:**
```
check health
```

**Expected Output:**
- Infrastructure health: SSM connectivity
- Application health: CloudWatch alarms
- Health check results (healthy/unhealthy)
- Request for approval to proceed

**Talking Points:**
- Two-layer health checks: infrastructure + application
- CloudWatch alarms monitor httpd service
- Agent waits for alarms to evaluate (2 min)
- Only proceeds if healthy

**Type when prompted:**
```
proceed
```

**Expected:** Agent patches staging, repeats health check workflow

**Type:**
```
proceed
```

**Expected:** Agent patches prod

---

## Scene 3: Intelligent Routing (2 minutes)

**Say:** "The supervisor intelligently routes queries to the right specialist. Watch what happens with a non-security query..."

**Type:**
```
List my Lambda functions
```

**Expected Output:**
- ⚙️ Querying AWS...
- List of Lambda functions

**Talking Points:**
- Query didn't match security or patching patterns
- Supervisor used fallback tool (use_aws) directly
- No specialist needed for simple AWS queries
- Shows intelligent routing logic

---

## Scene 4: Compliance (2 minutes)

**Say:** "Every decision is traceable for auditors..."

**Type:**
```
Generate compliance report
```

**Expected Output:**
- Formatted compliance report
- S3 storage location

**Talking Points:**
- Automatic audit trail generation
- S3 storage for long-term retention
- Maps to compliance frameworks (PCI-DSS, HIPAA, SOC2)
- Shows what was patched, when, and why

---

## Closing (1 minute)

**Say:** 
"This isn't just automation. It's intelligent orchestration that understands:
- Compliance requirements (PCI-DSS, SOC2, HIPAA SLA policies)
- Business context (maintenance windows, risk tolerance)
- Operational safety (phased rollout, health checks)

Built entirely with AWS native services. No new infrastructure to manage."

**Show:** Architecture diagram (if you have one)

---

## Q&A Preparation

### Q: How does it handle rollbacks?
**A:** "Agent can rollback via `yum history undo`. In production, you'd add automated health checks and canary rollback - rollback 1 instance, verify, then all."

### Q: What if the agent makes a mistake?
**A:** "Approval gates between environments. Human in the loop for prod. In production, add circuit breakers - stop if more than 10% of instances fail health checks."

### Q: Does it work with Windows?
**A:** "Demo is Linux with yum. The pattern extends to Windows Update via SSM. Multi-OS support is straightforward - same agent logic, different patching tools."

### Q: How much does this cost?
**A:** "Bedrock AgentCore charges per request. For 1000 instances patched monthly, roughly $50/month. Compare to manual patching: 40 hours/month × $100/hr = $4000. ROI is clear."

### Q: Can it integrate with our ITSM tools?
**A:** "Yes. Webhook support for ServiceNow, JIRA. Slack/PagerDuty notifications. The agent can be extended with custom tools for your specific workflows."

### Q: What about multi-cloud?
**A:** "Demo is AWS-focused. The agent pattern works anywhere - you'd swap the tools layer. Same supervisor logic, different cloud APIs."

### Q: Is this production-ready?
**A:** "This demonstrates production-grade patterns. For production, you'd add: multi-region support, more sophisticated approval workflows, deeper monitoring integration. The architecture is sound - it's about extending the tool set."

---

## Troubleshooting

### Agent times out
- **Cause:** Complex query, AWS API rate limiting
- **Fix:** Simplify query, check AWS service health
- **Backup:** Show pre-recorded demo

### No findings shown
- **Cause:** No active Inspector findings in account
- **Fix:** Show example from documentation
- **Backup:** Explain what would happen with real findings

### Routing fails
- **Cause:** Supervisor logic issue
- **Fix:** Enable debug mode: `./chat.sh -d`
- **Backup:** Explain routing decision tree from architecture

### Chat client crashes
- **Cause:** Python dependency issue
- **Fix:** Reinstall: `pip install -r chatbot-client/requirements.txt`
- **Backup:** Use direct agentcore invoke

---

## Backup Plan

**If live demo completely fails:**

1. **Screen recording:** Have a pre-recorded successful run
2. **Static examples:** Show example queries/responses in slides
3. **Architecture walkthrough:** Focus on design patterns instead of live demo
4. **Code walkthrough:** Show supervisor routing logic, tool implementations

**Always have:** Backup recording ready to play

---

## Practice Checklist

Before demo day:

- [ ] Run through script 3 times
- [ ] Time each section (should be ~10 min total)
- [ ] Test in fresh terminal window
- [ ] Verify AWS Inspector has findings
- [ ] Check SSM agents are connected
- [ ] Record backup video
- [ ] Prepare answers to Q&A
- [ ] Have architecture diagram ready
- [ ] Test with different queries
- [ ] Verify chat.sh works from any directory

---

## Demo Day Checklist

- [ ] Laptop fully charged
- [ ] Backup power adapter
- [ ] Screen recording ready
- [ ] Terminal font size increased (audience can read)
- [ ] Close unnecessary applications
- [ ] Disable notifications
- [ ] Test internet connection
- [ ] AWS credentials valid
- [ ] Agent deployed and responding
- [ ] Chat client tested
- [ ] Backup laptop ready (if available)

---

**Total Time:** 10 minutes (with buffer for questions)
**Confidence Level:** High (with practice and backup plan)
