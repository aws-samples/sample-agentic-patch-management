# Scenario 1 Infrastructure Deployment Guide

## Overview

This guide covers deploying the idempotent Scenario 1 demo infrastructure:
- 15 EC2 instances (5 dev, 5 staging, 5 prod)
- Maintenance windows for staging and prod
- Patch baselines for each environment
- All instances tagged for easy identification

## Prerequisites

- AWS CLI configured with `ri25-demo` profile
- Node.js and npm installed
- CDK dependencies installed (`npm install` in infra directory)
- Foundational stacks already deployed (Networking, Security, AgentCore Permissions)

## Stack Architecture

```
Foundational Stacks (keep deployed):
├─ PatchAutomationNetworking (VPC, subnets)
├─ PatchAutomationSecurity (security groups, IAM roles)
└─ PatchAutomationAgentCorePermissions (AgentCore IAM)

Demo Stack (idempotent - can destroy/recreate):
└─ PatchAutomationScenario1 (15 instances + maintenance windows)
```

## Deployment Steps

### 1. Verify Foundational Stacks

```bash
cd infra

# Check existing stacks
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `PatchAutomation`)].StackName' \
  --profile ri25-demo
```

**Expected output:**
- PatchAutomationNetworking
- PatchAutomationSecurity
- PatchAutomationAgentCorePermissions

If missing, deploy foundational stacks first:
```bash
cdk deploy PatchAutomationNetworking PatchAutomationSecurity PatchAutomationAgentCorePermissions \
  --profile ri25-demo
```

### 2. Deploy Scenario 1 Stack

```bash
# Synthesize to check for errors
cdk synth PatchAutomationScenario1 --profile ri25-demo

# Deploy
cdk deploy PatchAutomationScenario1 --profile ri25-demo
```

**Deployment time:** ~5-7 minutes

**Resources created:**
- 15 EC2 t3.micro instances
- 2 maintenance windows (staging, prod)
- 3 patch baselines (dev, staging, prod)
- 3 maintenance window targets

### 3. Verify Deployment

```bash
# Check stack status
aws cloudformation describe-stacks \
  --stack-name PatchAutomationScenario1 \
  --query 'Stacks[0].StackStatus' \
  --profile ri25-demo

# List created instances
aws ec2 describe-instances \
  --filters "Name=tag:Demo,Values=Scenario1" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`Name`].Value|[0],Tags[?Key==`Environment`].Value|[0]]' \
  --output table \
  --profile ri25-demo
```

**Expected output:** 15 instances (5 dev, 5 staging, 5 prod)

### 4. Verify SSM Agent Connectivity

```bash
# Check SSM managed instances
aws ssm describe-instance-information \
  --filters "Key=tag:Demo,Values=Scenario1" \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,PlatformName]' \
  --output table \
  --profile ri25-demo
```

**Expected:** All 15 instances with `PingStatus=Online`

**If instances not showing:**
- Wait 2-3 minutes for SSM Agent to register
- Check instance user data logs: `aws ssm send-command --document-name "AWS-RunShellScript" --parameters 'commands=["tail -50 /var/log/cloud-init-output.log"]' --instance-ids <instance-id>`

### 5. Verify Apache Installation

```bash
# Check Apache on one instance
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Environment,Values=dev" "Name=tag:Demo,Values=Scenario1" \
  --query 'Reservations[0].Instances[0].InstanceId' \
  --output text \
  --profile ri25-demo)

aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["systemctl status httpd","httpd -v"]' \
  --instance-ids $INSTANCE_ID \
  --profile ri25-demo
```

### 6. Enable Inspector Scanning

```bash
# Enable Inspector for EC2 scanning
aws inspector2 enable \
  --resource-types EC2 \
  --profile ri25-demo

# Verify Inspector is enabled
aws inspector2 batch-get-account-status \
  --profile ri25-demo
```

**Note:** Initial scan takes 24 hours. For immediate demo, you can:
- Install known vulnerable packages
- Use synthetic CVE data in agent tools

## Stack Outputs

After deployment, get important values:

```bash
# Get all outputs
aws cloudformation describe-stacks \
  --stack-name PatchAutomationScenario1 \
  --query 'Stacks[0].Outputs' \
  --profile ri25-demo

# Get specific instance IDs
aws cloudformation describe-stacks \
  --stack-name PatchAutomationScenario1 \
  --query 'Stacks[0].Outputs[?contains(OutputKey, `instance`)].{Environment:OutputKey,InstanceId:OutputValue}' \
  --output table \
  --profile ri25-demo
```

## Idempotent Operations

### Destroy and Recreate Demo Stack

```bash
# Destroy Scenario 1 stack (keeps foundational stacks)
cdk destroy PatchAutomationScenario1 --profile ri25-demo

# Recreate
cdk deploy PatchAutomationScenario1 --profile ri25-demo
```

**Use cases:**
- Reset demo environment
- Test infrastructure changes
- Clean up between demos

### Update Existing Stack

```bash
# Make changes to scenario1-demo-stack.ts
# Then deploy
cdk deploy PatchAutomationScenario1 --profile ri25-demo
```

CDK will show a diff and only update changed resources.

## Cost Optimization

### Stop Instances When Not Demoing

```bash
# Stop all Scenario 1 instances
aws ec2 stop-instances \
  --instance-ids $(aws ec2 describe-instances \
    --filters "Name=tag:Demo,Values=Scenario1" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' \
    --output text) \
  --profile ri25-demo

# Start instances before demo
aws ec2 start-instances \
  --instance-ids $(aws ec2 describe-instances \
    --filters "Name=tag:Demo,Values=Scenario1" \
    --query 'Reservations[].Instances[].InstanceId' \
    --output text) \
  --profile ri25-demo
```

### Estimated Costs

**Running 24/7:**
- 15 x t3.micro instances: ~$3.80/day (~$114/month)
- Data transfer: ~$5/month
- **Total: ~$119/month**

**Running 8 hours/day (demo hours):**
- 15 x t3.micro instances: ~$1.27/day (~$38/month)
- Data transfer: ~$2/month
- **Total: ~$40/month**

**Recommendation:** Stop instances when not actively demoing.

## Troubleshooting

### Issue: Instances not showing in SSM

**Solution:**
```bash
# Check instance IAM role
aws ec2 describe-instances \
  --instance-ids <instance-id> \
  --query 'Reservations[0].Instances[0].IamInstanceProfile' \
  --profile ri25-demo

# Check SSM Agent logs
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo systemctl status amazon-ssm-agent","sudo tail -50 /var/log/amazon/ssm/amazon-ssm-agent.log"]' \
  --instance-ids <instance-id> \
  --profile ri25-demo
```

### Issue: Apache not installed

**Solution:**
```bash
# Check user data execution
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["tail -100 /var/log/cloud-init-output.log"]' \
  --instance-ids <instance-id> \
  --profile ri25-demo

# Manually install Apache
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo yum install -y httpd","sudo systemctl enable httpd","sudo systemctl start httpd"]' \
  --instance-ids <instance-id> \
  --profile ri25-demo
```

### Issue: CDK deployment fails

**Solution:**
```bash
# Check CDK bootstrap
cdk bootstrap aws://125140434174/us-east-1 --profile ri25-demo

# Check for resource limits
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-1216C47A \
  --profile ri25-demo

# Clean up failed stack
cdk destroy PatchAutomationScenario1 --force --profile ri25-demo
```

## Next Steps

After infrastructure is deployed:

1. **Test tools.py** - Verify `get_instance_inventory` returns 15 instances
2. **Update agent prompts** - Use new phased rollout prompts
3. **Deploy agent** - `agentcore launch` with updated code
4. **Test workflow** - Run end-to-end patch automation

See `PIVOT_PLAN.md` for complete implementation steps.
