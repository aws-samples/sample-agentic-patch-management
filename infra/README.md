# Infrastructure (CDK)

AWS CDK stacks for Patchy — Intelligent Patch Automation.

## Stacks

### Solution (always deployed)

| Stack | Resources |
|-------|-----------|
| `Patchy-Network` | VPC (10.0.0.0/16), 3 AZs, public/private/isolated subnets, 2 NAT gateways, VPC flow logs. Skipped when `EXISTING_VPC_ID` is set. |
| `Patchy-Core` | S3 compliance reports bucket, AgentCore IAM policy |
| `Patchy-UI` | ECS Fargate service (256 CPU/512 MB), internal ALB (300s idle timeout for SSE), CloudWatch log group |

### Sample Environment (optional)

| Stack | Resources |
|-------|-----------|
| `Patchy-SampleEnv` | 15 t3.micro instances (5 dev/5 staging/5 prod), instance SG + IAM role, 3 ALBs, 3 maintenance windows, 3 patch baselines, CloudWatch alarms |

## Deploy

```bash
# From project root (recommended)
./deploy.sh               # Full solution
./sample-env.sh deploy    # Sample EC2 environment (separate)
./deploy.sh ui            # UI stack only (after agent is deployed)

# Or directly with CDK
cd infra && npm ci
npx cdk deploy Patchy-Network Patchy-Core -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
npx cdk deploy Patchy-UI -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
```

## Destroy

```bash
./deploy.sh destroy        # Solution only (sample env preserved)
./sample-env.sh destroy    # Sample environment only
```

## Context Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agentCoreRoleArn` | Yes | ARN of the AgentCore runtime IAM role (auto-detected by deploy.sh) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `EXISTING_VPC_ID` | Use an existing VPC instead of creating one. Skips Patchy-Network. |
| `ACM_CERTIFICATE_ARN` | Enables HTTPS on the UI ALB. |
