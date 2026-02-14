#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { NetworkingStack } from '../lib/networking-stack';
import { ComplianceStack } from '../lib/compliance-stack';
import { SampleEnvironmentStack } from '../lib/sample/vulnerability-driven-patching-stack';
import { AgentCorePermissionsStack } from '../lib/agentcore-permissions-stack';
import { UIStack } from '../lib/ui-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT || process.env.AWS_ACCOUNT_ID,
  region: process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION || 'us-east-1',
};

if (!env.account) {
  throw new Error('AWS account not resolved. Run: aws configure, or set CDK_DEFAULT_ACCOUNT / AWS_ACCOUNT_ID');
}

const agentCoreRoleArn = app.node.tryGetContext('agentCoreRoleArn');
if (!agentCoreRoleArn || agentCoreRoleArn === 'placeholder') {
  console.warn('⚠️  agentCoreRoleArn not set. AgentCore permissions will deploy but not attach to a role.');
  console.warn('   Pass: cdk deploy -c agentCoreRoleArn=arn:aws:iam::<account>:role/<role-name>');
}
const resolvedRoleArn = (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') ? agentCoreRoleArn : undefined;

// ── VPC Resolution ─────────────────────────────────────────────────
// EXISTING_VPC_ID set → use customer's VPC, skip NetworkingStack
// Not set            → create a new VPC via NetworkingStack (demo path)

const existingVpcId = process.env.EXISTING_VPC_ID;
let vpc: ec2.IVpc;
let networkingStack: NetworkingStack | undefined;

if (existingVpcId) {
  const lookupStack = new cdk.Stack(app, 'PatchAutomationVpcLookup', {
    env,
    description: 'VPC lookup for Intelligent Patch Automation (existing VPC mode)',
  });
  vpc = ec2.Vpc.fromLookup(lookupStack, 'ImportedVPC', { vpcId: existingVpcId });
} else {
  networkingStack = new NetworkingStack(app, 'PatchAutomationNetworking', {
    env,
    description: 'VPC, subnets, and networking for Intelligent Patch Automation',
  });
  vpc = networkingStack.vpc;
}

// ── Solution Infrastructure (always deployed) ──────────────────────

// S3 compliance reports bucket + AWS Config recorder
const complianceStack = new ComplianceStack(app, 'PatchAutomationCompliance', {
  env,
  agentCoreRoleArn: resolvedRoleArn,
  description: 'S3 compliance reports and AWS Config for Intelligent Patch Automation',
});

// AgentCore IAM permissions
new AgentCorePermissionsStack(app, 'PatchAutomationAgentCorePermissions', {
  env,
  agentCoreRoleArn: resolvedRoleArn,
  description: 'IAM permissions for AgentCore patch automation runtime',
});

// Web UI (Fargate + internal ALB)
const uiStack = new UIStack(app, 'PatchAutomationUI', {
  env,
  vpc,
  agentCoreRoleArn: resolvedRoleArn,
  certificateArn: process.env.ACM_CERTIFICATE_ARN || undefined,
  description: 'Web UI for Intelligent Patch Automation (internal ALB + SSM port forwarding)',
});
if (networkingStack) uiStack.addDependency(networkingStack);

// ── Sample Environment (optional, --with-demo) ────────────────────
// Self-contained: creates its own SG, instance role, 15 EC2 instances,
// maintenance windows, ALBs, patch baselines, and CloudWatch alarms.
// Deploy: ./deploy.sh --with-demo  |  Destroy: ./deploy.sh destroy --demo-only

const sampleStack = new SampleEnvironmentStack(app, 'PatchAutomationSampleEnv', {
  env,
  vpc,
  description: 'Sample environment: 15 EC2 instances for testing patch automation (optional)',
});
if (networkingStack) sampleStack.addDependency(networkingStack);
