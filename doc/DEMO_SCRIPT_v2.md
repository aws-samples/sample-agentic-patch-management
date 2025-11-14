# Live Demo Script (10 minutes)

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

## Opening (30 seconds)

**Say:** "Traditional patching means juggling 15 browser tabs, 3 CLI tools, and 45 minutes of manual work per CVE. What if you could just ask an agent to handle it?"

**Show:** Terminal with `./chat.sh` running

**Say:** "This is a multi-agent system that understands your infrastructure, makes risk-based decisions, and executes patches safely. Let me show you."

---

## Scene 1: Understanding Your Infrastructure (2 minutes)

**Say:** "First, the agent needs to understand what it's protecting."

**Type:** 
```
Show me prod instances and their compliance frameworks
```

**What Happens:**
- Agent routes to Patch Manager specialist
- Returns table with 5 production instances
- Shows compliance tags: PCI-DSS, SOC2, HIPAA

**Say While Results Appear:**
"The agent reads compliance requirements directly from instance tags. PCI-DSS has the strictest patching SLAs - 48 hours for critical vulnerabilities. These tags drive every decision the agent makes."

**Key Point:** "No configuration files. No hardcoded policies. Just tags on your existing infrastructure."

---

## Scene 2: Discovering Threats (2 minutes)

**Say:** "Now let's see what vulnerabilities exist."

**Type:** 
```
What are the highest vulnerabilities in my environment?
```

**What Happens:**
- Agent routes to Vulnerability Analyst specialist
- Shows top priority CVE with CVSS score
- Displays table of top 10 vulnerabilities
- Data comes from AWS Inspector

**Say While Results Appear:**
"Notice how the agent leads with what matters most - the highest priority threat. This is real data from AWS Inspector, analyzed and prioritized for you."

**Key Point:** "The agent doesn't just list vulnerabilities. It tells you which one to worry about first."

---

## Scene 3: Making Intelligent Decisions (2.5 minutes)

**Say:** "Here's where it gets interesting. Watch how the agent uses context from our previous conversation."

**Type:**
```
Build a deployment plan for production for the top 3 CVEs
```

**What Happens:**
- Agent remembers which CVEs you discussed
- Checks maintenance windows
- Calculates risk vs SLA requirements
- Proposes phased rollout: dev → staging → prod

**Say While Results Appear:**
"The agent didn't ask 'which CVEs?' - it remembered from our previous message. It's checking maintenance windows right now... and it's comparing the SLA deadline against the next scheduled window."

**When Results Show:**
"See this? The agent decided this is SCHEDULED patching, not emergency. Why? Because the next maintenance window is within the PCI-DSS 48-hour requirement. It's making a risk-based decision."

**Key Point:** "This isn't a script following rules. It's an agent reasoning about your specific situation."

---

## Scene 4: Intelligent Routing (1.5 minutes)

**Say:** "The system knows when to use specialists and when to go direct."

**Type:**
```
List my Lambda functions
```

**What Happens:**
- Agent skips specialists
- Goes directly to AWS API
- Returns Lambda function list

**Say While Results Appear:**
"No security keywords, no patching context - the supervisor went straight to AWS. It doesn't waste time routing to specialists when a simple API call will do."

**Key Point:** "Smart routing means faster responses and lower costs."

---

## Scene 5: Safe Execution (2 minutes) - OPTIONAL

**Only show if you have actual vulnerabilities and want to demonstrate execution.**

**Say:** "Let me show you what happens when you actually execute a patch."

**Type:**
```
Start emergency patching for CVE-2023-52935 in dev
```

**What Happens:**
- Agent patches dev environment
- Returns command ID
- Waits for your approval to continue

**Say While Results Appear:**
"The agent just patched the dev environment. Now it's waiting for me to say 'proceed' before touching staging. This is the approval gate - human in the loop."

**Explain (Don't Execute):**
"In production, this workflow continues:
- Dev → you approve → Staging → you approve → Production
- Health checks after each environment
- Automatic rollback if CloudWatch alarms fire
- Compliance report saved to S3 when complete

The whole process takes about 30 minutes for all three environments with automated verification."

**Key Point:** "Safety through phased rollout and approval gates."

---

## Closing (1 minute)

**Say:** 
"What you just saw:

**Conversational** - No rigid commands, just natural language
**Context-aware** - Agents remember what you discussed
**Risk-based** - Decisions driven by SLA requirements and maintenance windows
**Safe** - Phased rollout with approval gates

This is built entirely on AWS native services:
- Bedrock AgentCore for orchestration
- Systems Manager for patching
- Inspector for vulnerability data
- CloudWatch for health monitoring

No new infrastructure to manage. No complex integrations. Just intelligent agents working with the AWS services you already use."

**Final Point:** "The future of operations isn't about writing better scripts. It's about having conversations with intelligent agents that understand your infrastructure and make decisions for you."

---

## Q&A Preparation

### Q: How does the agent remember context?
**A:** "AgentCore provides conversation memory within a session. When you ask 'build deployment plan for production' after discussing CVEs, the agent knows which CVEs you mean. It's not magic - it's the agent reading previous messages in the conversation."

### Q: What if something goes wrong during patching?
**A:** "Two-layer health checks catch issues. Infrastructure checks verify SSM connectivity. Application checks monitor CloudWatch alarms. If alarms fire, the agent recommends rollback. You approve, it executes the rollback, and verifies recovery."

### Q: Can this work with Windows?
**A:** "Absolutely. The demo uses Linux with yum, but the pattern works with Windows Update via Systems Manager. Same agent logic, different patching tools."

### Q: How much does this cost?
**A:** "Bedrock AgentCore charges per request. For patching 1000 instances monthly, you're looking at roughly $50/month in agent costs. Compare that to 40 hours of manual work at $100/hour - that's $4000/month. 98% cost reduction."

### Q: Can it integrate with ServiceNow or JIRA?
**A:** "Yes. The agent can be extended with custom tools for any ITSM platform. Add a tool that calls ServiceNow's API, and the agent can create tickets, update status, or query existing incidents."

### Q: Is this production-ready?
**A:** "This demonstrates production-grade patterns. To productionize, you'd add multi-region support, more sophisticated approval workflows, and deeper monitoring. The architecture is sound - extend the tool set for your specific needs."

### Q: How do you customize SLA policies?
**A:** "Edit a simple JSON config file. Define hours by compliance framework and severity. The agent reads it automatically - no code changes needed."

### Q: What about multi-cloud?
**A:** "The agent pattern is cloud-agnostic. Swap the tools layer - same supervisor logic, different cloud APIs. Azure Update Management, Google OS Config, whatever you need."

---

## Troubleshooting

### No vulnerabilities in environment
- **Response:** "Great security posture! Let me walk you through what you'd see..." (describe hypothetically using Scene 2)

### Agent times out
- **Cause:** Complex query or AWS API rate limiting
- **Fix:** Simplify query, check AWS service health
- **Backup:** Show pre-recorded demo

### Chat client crashes
- **Fix:** `pip install -r chatbot-client/requirements.txt`
- **Backup:** Use slides with example outputs

---

## Backup Plan

**If live demo fails:**
1. Show pre-recorded video
2. Walk through example outputs in slides
3. Focus on architecture and design patterns

**Always have:** Backup recording ready to play

---

## Practice Checklist

- [ ] Run through script 3 times
- [ ] Time each section (~10 min total)
- [ ] Check for active vulnerabilities (adjust script if none)
- [ ] Test in fresh terminal
- [ ] Record backup video
- [ ] Prepare Q&A answers
- [ ] Have architecture diagram ready

---

## Demo Day Checklist

- [ ] Laptop charged + backup power
- [ ] Screen recording ready
- [ ] Terminal font size increased
- [ ] Close unnecessary apps
- [ ] Disable notifications
- [ ] Test internet connection
- [ ] AWS credentials valid
- [ ] Agent deployed and responding
- [ ] Chat client tested

---

**Total Time:** 10 minutes
**Focus:** What the audience experiences, not how it was built
