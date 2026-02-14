# Infrastructure (CDK)

AWS CDK stacks for Patchy — Intelligent Patch Automation.

## Stacks

### Solution (always deployed)

| Stack | Resources |
|-------|-----------|
| `Patchy-Network` | VPC (10.0.0.0/16), 3 AZs, public/private/isolated subnets, 2 NAT gateways, VPC flow logs. Skipped when `EXISTING_VPC_ID` is set. |
| `Patchy-Core` | S3 compliance reports bucket, AWS Config recorder, AgentCore IAM policy |
| `Patchy-UI` | ECS Fargate service (256 CPU/512 MB), internal ALB (300s idle timeout for SSE), CloudWatch log group |

### Sample Environment (optional)

| Stack | Resources |
|-------|-----------|
| `Patchy-SampleEnv` | 15 t3.micro instances (5 dev/5 staging/5 prod), instance SG + IAM role, 3 ALBs, 3 maintenance windows, 3 patch baselines, CloudWatch alarms |

## Deploy

```bash
# From project root (recommended)
./deploy.sh               # Full solution
./deploy.sh --with-demo   # Full solution + sample environment
./deploy.sh ui            # UI stack only (after agent is deployed)

# Or directly with CDK
cd infra && npm ci
npx cdk deploy Patchy-Network Patchy-Core -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
npx cdk deploy Patchy-UI -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"

# Skip Config recorder (if account already has one)
npx cdk deploy Patchy-Core -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN" -c skipConfigRecorder=true
```

## Destroy

```bash
./deploy.sh destroy              # Everything
./deploy.sh destroy --demo-only  # Sample environment only
```

## Context Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agentCoreRoleArn` | Yes | ARN of the AgentCore runtime IAM role (auto-detected by deploy.sh) |
| `skipConfigRecorder` | No | Set to `true` if account already has an AWS Config recorder |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `EXISTING_VPC_ID` | Use an existing VPC instead of creating one. Skips Patchy-Network. |
| `ACM_CERTIFICATE_ARN` | Enables HTTPS on the UI ALB. |
