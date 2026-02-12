#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { VulnerabilityDrivenPatchingStack } from '../lib/vulnerability-driven-patching-stack';
import { AgentCorePermissionsStack } from '../lib/agentcore-permissions-stack';
import { UIStack } from '../lib/ui-stack';

const app = new cdk.App();

// Environment configuration — reads from CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION
// (set by `aws configure` or environment variables)
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT || process.env.AWS_ACCOUNT_ID,
  region: process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION || 'us-east-1'
};

if (!env.account) {
  throw new Error('AWS account not resolved. Run: aws configure, or set CDK_DEFAULT_ACCOUNT / AWS_ACCOUNT_ID');
}

// ── Solution Infrastructure (required) ─────────────────────────────
// Core stacks deployed by: ./deploy.sh

// Core networking infrastructure
const networkingStack = new NetworkingStack(app, 'PatchAutomationNetworking', {
  env,
  description: 'VPC, subnets, and networking for Intelligent Patch Automation'
});

// Security groups, IAM roles, S3 buckets, SQS queue, AWS Config
const securityStack = new SecurityStack(app, 'PatchAutomationSecurity', {
  env,
  vpc: networkingStack.vpc,
  description: 'Security groups, IAM roles, and compliance infrastructure for Intelligent Patch Automation'
});

// AgentCore IAM permissions
const agentCoreRoleArn = app.node.tryGetContext('agentCoreRoleArn');
if (!agentCoreRoleArn || agentCoreRoleArn === 'placeholder') {
  console.warn('⚠️  agentCoreRoleArn not set. AgentCore permissions will deploy but not attach to a role.');
  console.warn('   Pass: cdk deploy -c agentCoreRoleArn=arn:aws:iam::<account>:role/<role-name>');
}

new AgentCorePermissionsStack(app, 'PatchAutomationAgentCorePermissions', {
  env,
  agentCoreRoleArn: (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') ? agentCoreRoleArn : undefined,
  description: 'IAM permissions for AgentCore patch automation runtime'
});

// ── Sample Environment (optional) ──────────────────────────────────
// 15 EC2 instances across dev/staging/prod with maintenance windows,
// ALBs, patch baselines, and CloudWatch alarms.
// Deploy with: ./deploy.sh --with-demo  |  Destroy with: ./deploy.sh destroy --demo-only

const vulnerabilityPatchingStack = new VulnerabilityDrivenPatchingStack(app, 'PatchAutomationVulnerabilityDriven', {
  env,
  vpc: networkingStack.vpc,
  securityGroup: securityStack.instanceSecurityGroup,
  instanceRole: securityStack.instanceRole,
  description: 'Sample environment: 15 EC2 instances for testing patch automation (optional)'
});

// Dependencies
securityStack.addDependency(networkingStack);
vulnerabilityPatchingStack.addDependency(securityStack);

// ── Web UI ──────────────────────────────────────────────────────────
// Fargate container serving the operations console (React + FastAPI).
// Deploy with: ./deploy.sh ui (after agent is deployed — Docker build
// copies agent/.bedrock_agentcore.yaml into the container image).

const uiStack = new UIStack(app, 'PatchAutomationUI', {
  env,
  vpc: networkingStack.vpc,
  agentCoreRoleArn: (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') ? agentCoreRoleArn : undefined,
  certificateArn: process.env.ACM_CERTIFICATE_ARN || undefined,
  description: 'Web UI for Intelligent Patch Automation (Fargate + ALB)',
});
uiStack.addDependency(networkingStack);
