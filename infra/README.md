# Patch Automation Foundations Infrastructure

Enterprise-grade AWS infrastructure for Intelligent Patch Automation demonstration.

## Architecture

### Networking
- **VPC**: 10.0.0.0/16 with 3 AZs
- **Public Subnets**: /24 subnets for internet-facing resources
- **Private Subnets**: /24 subnets with NAT Gateway egress
- **Isolated Subnets**: /28 subnets for databases
- **NAT Gateways**: 2 for high availability
- **VPC Flow Logs**: CloudWatch monitoring

### Security
- **Security Groups**: Least privilege access
- **IAM Roles**: Instance and agent permissions
- **Instance Profile**: Systems Manager integration
- **AgentCore Permissions**: Comprehensive policy for patch automation

### Demo Infrastructure
- **Production**: 2 web servers (high criticality)
- **Staging**: 1 web server (medium criticality)  
- **Development**: 2 web servers (low criticality)
- **Patch Baselines**: Environment-specific policies
- **Maintenance Windows**: Scheduled patching

## Deployment

### Prerequisites
```bash
# Install AWS CDK
npm install -g aws-cdk

# Set your AWS profile
export AWS_PROFILE=<your-profile>

# Create EC2 Key Pair for instances
aws ec2 create-key-pair --key-name patch-automation-demo
```

### Deploy Infrastructure
```bash
# Install dependencies
npm install

# Bootstrap CDK (first time only)
cdk bootstrap

# Deploy all stacks
npm run deploy

# Or deploy individually
cdk deploy PatchAutomationNetworking
cdk deploy PatchAutomationSecurity
cdk deploy PatchAutomationInstances
cdk deploy PatchAutomationAgentCorePermissions
```

### AgentCore Integration
```bash
# Get the policy ARN from stack outputs
POLICY_ARN=$(aws cloudformation describe-stacks \
  --stack-name PatchAutomationAgentCorePermissions \
  --query 'Stacks[0].Outputs[?OutputKey==`AgentCorePolicyArn`].OutputValue' \
  --output text)

# Attach to AgentCore runtime role
aws iam attach-role-policy \
  --role-name AmazonBedrockAgentCoreSDKRuntime-<region>-<suffix> \
  --policy-arn $POLICY_ARN
```

### Cleanup
```bash
# Destroy all infrastructure
npm run destroy
```

## Post-Deployment Setup

### Enable Inspector
```bash
# Enable Inspector for vulnerability scanning
aws inspector2 enable --resource-types ECR EC2
```

### Verify Systems Manager
```bash
# Check managed instances
aws ssm describe-instance-information
```

## Infrastructure Outputs

- **VPC ID**: For reference in other stacks
- **Subnet IDs**: Public and private subnet references
- **Security Group ID**: Instance security group
- **IAM Role ARNs**: Instance and agent roles
- **AgentCore Policy ARN**: For runtime role attachment

## AgentCore Permissions

The AgentCore permissions stack creates a comprehensive IAM policy with:

- **EC2**: Instance discovery, tags, status, security groups, VPCs
- **SSM**: Patch management, compliance, command execution, maintenance windows
- **Inspector**: Vulnerability findings and details
- **Config**: Compliance rules and resource assessment
- **CloudWatch**: Metrics, logs, monitoring data

## Cost Optimization

- **Instance Types**: t3.micro (free tier eligible)
- **NAT Gateways**: 2 for HA (can reduce to 1 for cost savings)
- **EBS Volumes**: gp3 for cost efficiency

## Security Considerations

- **Private Subnets**: Instances not directly internet accessible
- **Security Groups**: Minimal required ports
- **IAM Policies**: Least privilege access
- **VPC Flow Logs**: Network monitoring and security
