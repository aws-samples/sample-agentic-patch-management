# Infrastructure (CDK)

AWS CDK stacks for Intelligent Patch Automation.

## Stacks

### Solution (required)

| Stack | Resources |
|-------|-----------|
| `PatchAutomationNetworking` | VPC (10.0.0.0/16), 3 AZs, public/private/isolated subnets, 2 NAT gateways, VPC flow logs |
| `PatchAutomationSecurity` | Security groups, instance IAM role + profile, S3 compliance bucket, SQS FIFO queue, AWS Config recorder |
| `PatchAutomationAgentCorePermissions` | Managed IAM policy for AgentCore runtime (EC2, SSM, Inspector, Config, CloudWatch, S3, SQS) |
| `PatchAutomationUI` | ECS Fargate service (256 CPU/512 MB), internal ALB (300s idle timeout for SSE), CloudWatch log group |

### Sample Environment (optional)

| Stack | Resources |
|-------|-----------|
| `PatchAutomationVulnerabilityDriven` | 15 t3.micro instances (5 dev/5 staging/5 prod), 3 ALBs, 3 maintenance windows, 3 patch baselines, CloudWatch alarms |

## Deploy

```bash
# From project root (recommended)
./deploy.sh infra         # All 5 stacks
./deploy.sh solution      # Solution stacks only (4 stacks + agent)
./deploy.sh ui            # UI stack only

# Or directly with CDK
cd infra
npm ci
npx cdk deploy --all -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"

# Solution stacks only
npx cdk deploy PatchAutomationNetworking PatchAutomationSecurity PatchAutomationAgentCorePermissions PatchAutomationUI \
  -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"

# Skip Config recorder (if account already has one)
npx cdk deploy --all -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN" -c skipConfigRecorder=true
```

## Destroy

```bash
# Sample environment only (keeps solution infra)
./deploy.sh destroy-demo

# Everything
./deploy.sh destroy-all
```

## Context Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agentCoreRoleArn` | Yes | ARN of the AgentCore runtime IAM role |
| `skipConfigRecorder` | No | Set to `true` if account already has an AWS Config recorder |
