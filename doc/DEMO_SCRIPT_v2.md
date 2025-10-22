# Live Demo Script (10 minutes) - v2

## Pre-Demo Setup (5 min before presentation)

```bash
cd /Users/bhatprav/Documents/Workspace/ContentCreation/reinvent25-cop407-agentic-patch-management

# Verify deployment
./deploy.sh agent

# Start chat client
./chat.sh

# CRITICAL: Check if you have active vulnerabilities
# If none, be prepared to explain hypothetically
```

**Verify:** Chat client starts, agent responds to "hello"

---

## Demo Modes

**FAST MODE (Default):** User says "proceed" → agent moves to next environment immediately
- Best for: Demos, dev/staging environments
- Speed: ~2 minutes per environment
- Use when: Time is limited, showing workflow

**PRODUCTION MODE (Optional):** User says "verify and proceed" → agent waits for completion + health checks
- Best for: Production environments, safety-critical demos
- Speed: ~12 minutes per environment (includes 10 min patching + 2 min alarm evaluation)
- Use when: Showing production-ready behavior, emphasizing safety

**This demo uses FAST MODE by default.** To use production mode, say "verify and proceed" instead of "proceed".

---

## Opening (30 seconds)

**Say:** "Traditional patching: 15 browser tabs, 3 CLI tools, 45 minutes per CVE. Let me show you intelligent orchestration instead."

**Show:** Terminal with `./chat.sh` running

---

## Scene 1: Compliance Context (2 minutes)

**Say:** "First, let's see what we're protecting..."

**Type:** 
```
Show me prod instances and their compliance frameworks
```

**Expected Output:**
- 🛠️ Consulting Patch Manager...
- Table with 5 prod instances
- Compliance frameworks: PCI-DSS, SOC2, HIPAA
- Instance details

**Talking Points:**
- "These tags drive everything - PCI-DSS has strictest requirements"
- "Each framework has different SLA requirements for patching"
- "Agent reads this context automatically - no configuration needed"

---

## Scene 2: SLA-Aware Decision Logic (2.5 minutes)

**Say:** "Now watch how compliance requirements drive patching decisions..."

**Type:**
```
Check the SLA requirement for prod instances with CRITICAL severity and show me the maintenance window timing
```

**Expected Output:**
- Analysis of compliance frameworks
- "SLA: 12 hours (PCI-DSS - strictest)"
- Maintenance window timing
- EMERGENCY vs SCHEDULED decision logic

**Talking Points:**
- "Agent calculates strictest SLA across all frameworks"
- "PCI-DSS: 12 hours, SOC2: 24 hours, HIPAA: 24 hours → 12 hours wins"
- "Compares maintenance window timing vs SLA deadline"
- "If window is 96 hours away but SLA is 12 hours → EMERGENCY patching"
- "This is policy-driven - JSON config file, easy to customize"

**Show (if time):** Quick peek at `agent/config/config.json`

---

## Scene 3: Real Vulnerability Analysis (2 minutes)

**Say:** "Let's check what vulnerabilities we actually have..."

**Type:**
```
What vulnerabilities do we have in dev environment?
```

**Expected Output:**
- 🔍 Consulting Vulnerability Analyst...
- List of findings (or "No critical vulnerabilities")

**If vulnerabilities found:**
- Show CVE details, CVSS scores, affected instances
- Point out SLA calculation in output

**If no vulnerabilities:**
**Say:** "Great! No active vulnerabilities. In a real scenario, you'd see CVE details, CVSS scores, and automatic SLA calculation based on those compliance tags we saw earlier."

**Talking Points:**
- "Agent routes to security specialist automatically"
- "Real data from AWS Inspector"
- "SLA appears in output when analyzing affected instances"

---

## Scene 4: Intelligent Routing (1.5 minutes)

**Say:** "The supervisor knows when to use specialists vs direct AWS access..."

**Type:**
```
List my Lambda functions
```

**Expected Output:**
- ⚙️ Querying AWS...
- List of Lambda functions

**Talking Points:**
- "Query didn't match security or patching patterns"
- "Supervisor used fallback tool directly - no specialist needed"
- "This is keyword-based routing with OUT_OF_SCOPE fallback"
- "18-36% faster by eliminating wrong specialist calls"

---

## Scene 5: Patching Workflow (2 minutes) - OPTIONAL

**Only do this if you have actual vulnerabilities. Otherwise skip to Closing.**

**Say:** "Let me show you the patching workflow..."

**Type:**
```
Patch [CVE-ID] in dev environment
```

**Expected Output:**
- SLA calculation and decision
- Patching initiated
- Command ID
- Instructions for next steps

**Say:** "In production, patching takes 5-10 minutes. The workflow is:
1. Agent starts patch → returns command ID
2. You check status when ready
3. Agent verifies health (infrastructure + CloudWatch alarms)
4. You approve proceeding to staging, then prod
5. Phased rollout with approval gates at each stage"

**Talking Points:**
- "Asynchronous by design - no timeouts"
- "Two-layer health checks: SSM connectivity + CloudWatch alarms"
- "Human approval between environments"
- "For this demo, I'm showing the workflow without waiting 10 minutes"

---

## Scene 5: Executive Compliance Reporting (2 minutes) - OPTIONAL

**Say:** "Let me show you the compliance analyst - built for CISOs and risk officers..."

**Type:**
```
Show me all SLA breaches this month
```

**Expected Output:**
- 📊 Consulting Compliance Analyst...
- Summary: "X reports found, Y SLA breaches (Z%)"
- Statistics breakdown
- List of breached reports with details

**Talking Points:**
- "Compliance analyst queries reports from S3"
- "Automatic aggregation - breach count, breach rate, team breakdown"
- "This is what CISOs need for board reporting"

**Type:**
```
Which teams have the most SLA breaches?
```

**Expected Output:**
- Team breakdown with counts
- Analysis of which teams need support
- Recommendations

**Talking Points:**
- "Identifies accountability gaps"
- "Helps with resource allocation decisions"
- "Shows which teams need training or more resources"

**Type:**
```
What's our SLA compliance rate this month?
```

**Expected Output:**
- Overall compliance percentage
- Trend analysis (if multiple months of data)
- Executive summary

**Talking Points:**
- "Board-level metric"
- "Shows overall security posture"
- "Tracks improvement over time"
- "Perfect for quarterly business reviews"

**Say:** "These reports feed into your data lake - Athena queries, QuickSight dashboards, executive reporting. The agent provides the intelligence layer on top of your compliance data."

---

## Closing (1 minute)

**Say:** 
"This is intelligent orchestration, not just automation:

**Compliance-aware:** Reads PCI-DSS, SOC2, HIPAA requirements from instance tags
**Risk-based:** Compares maintenance windows vs SLA deadlines
**Operationally safe:** Phased rollout, health checks, approval gates

Built entirely with AWS native services:
- Bedrock AgentCore for orchestration
- Systems Manager for patching
- Inspector for vulnerability data
- CloudWatch for health monitoring

No new infrastructure. No rigid scripts. Just natural language and intelligent decisions."

---

## Q&A Preparation

### Q: How does it handle rollbacks?
**A:** "Two-layer health checks catch issues. If CloudWatch alarms fire, agent recommends rollback. You approve, it executes `yum history undo`, waits for recovery, verifies alarms cleared. Default is rollback only the failed environment - preserves lower environments for comparison."

### Q: What if the agent makes a mistake?
**A:** "Approval gates between environments. Human in the loop. For production, add circuit breakers - stop if >10% instances fail health checks."

### Q: Does it work with Windows?
**A:** "Demo is Linux with yum. Pattern extends to Windows Update via SSM. Same agent logic, different patching tools."

### Q: How much does this cost?
**A:** "Bedrock AgentCore charges per request. For 1000 instances patched monthly: ~$50/month. Compare to manual: 40 hours × $100/hr = $4000/month. 98% cost reduction."

### Q: Can it integrate with ITSM tools?
**A:** "Yes. Agent can be extended with custom tools for ServiceNow, JIRA, PagerDuty. Webhook support for notifications."

### Q: What about multi-cloud?
**A:** "Agent pattern is cloud-agnostic. Swap the tools layer - same supervisor logic, different cloud APIs. Azure Update Management, Google OS Config."

### Q: Is this production-ready?
**A:** "This demonstrates production-grade patterns. To productionize: add multi-region support, sophisticated approval workflows, deeper monitoring. Architecture is sound - extend the tool set for your needs."

### Q: How do you customize SLA policies?
**A:** "Edit `agent/config/config.json` - simple JSON file. Define hours by framework and severity, plus environment aliases. Agent reads it automatically. No code changes needed."

---

## Troubleshooting

### No vulnerabilities in environment
- **Expected:** This is actually good!
- **Response:** "Great security posture! Let me explain what you'd see..." (describe hypothetically)
- **Backup:** Show Scene 2 (hypothetical SLA analysis) instead

### Agent times out
- **Cause:** Complex query, AWS API rate limiting
- **Fix:** Simplify query, check AWS service health
- **Backup:** Show pre-recorded demo

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

1. **Screen recording:** Have pre-recorded successful run ready
2. **Static examples:** Show example queries/responses in slides
3. **Architecture walkthrough:** Focus on design patterns
4. **Code walkthrough:** Show SLA policy JSON, tool implementations

**Always have:** Backup recording ready to play

---

## Practice Checklist

Before demo day:

- [ ] Run through script 3 times
- [ ] Time each section (should be ~10 min total)
- [ ] Check if you have active vulnerabilities (adjust script accordingly)
- [ ] Test in fresh terminal window
- [ ] Verify SSM agents are connected
- [ ] Record backup video
- [ ] Prepare answers to Q&A
- [ ] Have architecture diagram ready
- [ ] Print SLA policy JSON for reference
- [ ] Test with different queries

---

## Demo Day Checklist

- [ ] Laptop fully charged + backup power adapter
- [ ] Screen recording ready
- [ ] Terminal font size increased (audience can read)
- [ ] Close unnecessary applications
- [ ] Disable notifications
- [ ] Test internet connection
- [ ] AWS credentials valid
- [ ] Agent deployed and responding
- [ ] Chat client tested
- [ ] Backup laptop ready (if available)
- [ ] SLA policy JSON printed/accessible

---

## Key Differences from v1

**Safer:**
- Doesn't assume vulnerabilities exist
- No fake CVE IDs
- Honest about timing (explains workflow vs simulating)
- Compliance report removed (disconnected from flow)

**More Compelling:**
- Leads with compliance context (sets up SLA story)
- Shows decision logic before execution
- Emphasizes policy-driven approach
- Better narrative flow

**More Coherent:**
- Each scene builds on previous
- Compliance tags → SLA calculation → Decision logic → Routing
- No disconnected scenes

**Total Time:** 9-10 minutes (flexible based on vulnerabilities)
**Confidence Level:** High (works even with no vulnerabilities)
