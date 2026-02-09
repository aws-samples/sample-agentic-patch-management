# Project Overview

## Why This Project Exists

Traditional vulnerability-driven patch management requires security teams to manually monitor vulnerability feeds, assess CVE severity, check compliance SLA deadlines, coordinate maintenance windows, execute patches across environments, verify health, and generate compliance reports. This takes ~45 minutes per CVE across 15 browser tabs and 3 CLI tools.

## What This Project Does

Multi-agent patch automation system built for AWS re:Invent 2025 session COP407. Natural language interface for the entire vulnerability-driven patching workflow.

**Built with:**
- AWS Strands SDK — agent framework
- Amazon Bedrock AgentCore — managed runtime
- Claude Sonnet 4.5 — foundation model
- AWS Systems Manager — patch execution
- AWS Inspector — vulnerability discovery
- AWS Config — resource dependency tracking

### Core Capabilities

1. **Intelligent Vulnerability Analysis** — Inspector integration, CVSS scoring, blast radius via Config
2. **SLA-Aware Patch Management** — PCI-DSS/SOC2/HIPAA compliance detection from instance tags, EMERGENCY vs SCHEDULED decisions based on maintenance window timing vs SLA deadline
3. **Phased Rollout** — dev → staging → prod with human approval gates
4. **Two-Layer Health Monitoring** — SSM connectivity + CloudWatch alarm checks
5. **Compliance Reporting** — Automated audit trails in S3 (date-partitioned, queryable metadata)
6. **Structured Error Recovery** — Tools return classified errors (RETRYABLE/SKIP/ABORT), agent follows recovery guidance
7. **Decision Audit Trail** — Every routing, SLA, and error recovery decision logged to CloudWatch + S3

## How It Works

```
User: "Handle CVE-2025-38527 in dev environment"

Supervisor → routes to Patch Manager (keyword: "CVE", "dev")
  Step 1: get_patch_compliance(dev, CRITICAL) → 5 instances, SLA=12hr (PCI-DSS)
  Step 2: get_maintenance_windows(dev) → next window 36hr away
  Step 3: 36hr > 12hr → EMERGENCY decision
  Step 4: execute_patch_operation() → command_id (async)
  Step 5: check_instance_health() → all connected
  Step 6: check_cloudwatch_alarms() → no alarms
  Step 7: generate_compliance_report() → S3 audit trail

User: "proceed" → repeats for staging, then prod
```

## Architecture

4 agents: Supervisor (routing + fallback), Vulnerability Analyst (Inspector + Config), Patch Manager (11 tools, 7-step workflow), Compliance Analyst (S3 report queries).

Supervisor uses keyword-based routing (18-36% faster than LLM routing). OUT_OF_SCOPE fallback catches misrouted queries.

## Key Design Decisions

- **Keyword routing** over LLM routing — faster, transparent, debuggable
- **Asynchronous patching** — SSM commands take 5-10 min, return command_id immediately
- **Two-layer health checks** — infrastructure (SSM) + application (CloudWatch alarms)
- **Tools classify their own errors** — no separate error recovery framework
- **Agents produce data, customers analyze it** — decisions stored in standard formats (JSON in CloudWatch + S3), queryable with existing tools (Athena, Splunk, Datadog)
- **Determinism through prompts** — 7-step mandatory workflow enforced via system prompt, not external validation
- **SSM handles constraints** — maintenance windows, patch baselines. No custom constraints.json needed.

## Environment

- AWS Profile: `ri25-demo`, Region: `us-east-1`, Account: `125140434174`
- 15 EC2 instances (5 dev, 5 staging, 5 prod) with maintenance windows
- S3 bucket: `patch-compliance-reports-125140434174` (reports + decisions)
- Deploy: `./deploy.sh agent` (CodeBuild ARM64)
- Chat: `./chat.sh` (interactive, maintains session)
- `agentcore invoke` is stateless — each invoke is a new session

## Constraints

- AWS-only, Linux-only (yum rollback), single region (us-east-1)
- Manual approval between environments
- CloudWatch alarms must be configured for application health checks
