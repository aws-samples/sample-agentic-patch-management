# Technology Stack

## Core

| Technology | Purpose | Version |
|-----------|---------|---------|
| AWS Strands SDK | Agent framework | 1.4.0+ |
| Amazon Bedrock AgentCore | Managed agent runtime | Latest |
| Claude Sonnet 4.5 | Foundation model (`anthropic.claude-sonnet-4-5-v2:0`) | Latest |
| Python | Agent code | 3.9+ |
| AWS CDK (TypeScript) | Infrastructure as Code | Latest |
| Docker/Finch | Container runtime (ARM64) | Latest |

## AWS Services

| Service | Usage |
|---------|-------|
| Systems Manager | Patch execution, compliance, maintenance windows |
| Inspector | Vulnerability discovery, CVE findings |
| Config | Resource dependency tracking, blast radius |
| CloudWatch | Alarms (app health), Logs (agent + decisions), X-Ray |
| S3 | Compliance reports + decision audit trail |
| EC2 | Target instances (15 across dev/staging/prod) |

## Key Dependencies

```
# Agent (Python)
bedrock-agentcore-starter-toolkit
bedrock-agentcore
strands-agents>=1.4.0
strands-agents-tools>=0.2.3
boto3
croniter

# Chat client
boto3, pyyaml, rich

# Infrastructure (Node.js)
aws-cdk-lib, constructs
```

## Configuration Files

| File | Purpose |
|------|---------|
| `agent/.bedrock_agentcore.yaml` | AgentCore runtime config (ARM64, STM_ONLY, observability) |
| `agent/config/config.json` | SLA policies by framework, environment aliases |
| `.env` | AWS profile, region, account (not committed) |

## Deployment

```bash
./deploy.sh env    # Setup Python venv
./deploy.sh infra  # Deploy 4 CDK stacks
./deploy.sh agent  # Deploy to AgentCore (CodeBuild ARM64, ~2-3 min)
./deploy.sh all    # Everything
./deploy.sh destroy # Remove EC2 stack only (S3 bucket retained)
```

## Development

```bash
./chat.sh                    # Interactive chat (maintains session)
./chat.sh -m "query"         # Single message
./chat.sh -p ri25-demo       # Specific AWS profile
agentcore invoke '{"prompt": "..."}' # Stateless single invoke
agentcore status             # Check agent
```

## Observability

- CloudWatch Logs: `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT`
- GenAI Dashboard: CloudWatch Console → Gen AI Observability → Agent Core
- X-Ray: Distributed tracing
- Decision logs: `DECISION:` prefix in CloudWatch, `decisions/` prefix in S3
- Compliance reports: `YYYY/MM/DD/` prefix in S3

## Cost (~$150-200/month for demo environment)
- EC2: ~$100 (15 × t3.micro)
- NAT Gateway: ~$32
- Bedrock: ~$30 (AgentCore + Claude tokens)
- CloudWatch/S3: ~$6
