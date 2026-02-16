#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { NetworkingStack } from '../lib/networking-stack';
import { CoreStack } from '../lib/core-stack';
import { SampleEnvironmentStack } from '../lib/sample/sample-environment-stack';
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
  console.warn('agentCoreRoleArn not set. Patchy-Core will deploy but not attach to a role.');
  console.warn('  Pass: cdk deploy -c agentCoreRoleArn=arn:aws:iam::<account>:role/<role-name>');
}
const resolvedRoleArn = (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') ? agentCoreRoleArn : undefined;

// ── VPC ────────────────────────────────────────────────────────────
// EXISTING_VPC_ID → use customer's VPC, skip Patchy-Network
// Not set         → create a new VPC (standalone mode)

const existingVpcId = process.env.EXISTING_VPC_ID;
let vpc: ec2.IVpc;
let networkingStack: NetworkingStack | undefined;

if (existingVpcId) {
  const lookupStack = new cdk.Stack(app, 'Patchy-VpcLookup', { env });
  vpc = ec2.Vpc.fromLookup(lookupStack, 'ImportedVPC', { vpcId: existingVpcId });
} else {
  networkingStack = new NetworkingStack(app, 'Patchy-Network', {
    env,
    description: 'Patchy: VPC, subnets, and networking',
  });
  vpc = networkingStack.vpc;
}

// ── Core (always deployed) ─────────────────────────────────────────
// S3 compliance bucket, AWS Config recorder, AgentCore IAM policy
new CoreStack(app, 'Patchy-Core', {
  env,
  agentCoreRoleArn: resolvedRoleArn,
  description: 'Patchy: S3 compliance reports, Config recorder, AgentCore IAM policy',
});

// ── UI (deploy separately with ./deploy.sh ui) ────────────────────
const uiStack = new UIStack(app, 'Patchy-UI', {
  env,
  vpc,
  agentCoreRoleArn: resolvedRoleArn,
  certificateArn: process.env.ACM_CERTIFICATE_ARN || undefined,
  description: 'Patchy: Web UI (Fargate + internal ALB)',
});
if (networkingStack) uiStack.addDependency(networkingStack);

// ── Sample Environment (optional: --with-sample-env) ───────────────
// Self-contained: SG, instance role, 15 EC2s, maintenance windows,
// ALBs, patch baselines, CloudWatch alarms.
const sampleStack = new SampleEnvironmentStack(app, 'Patchy-SampleEnv', {
  env,
  vpc,
  description: 'Patchy: Sample environment with 15 EC2 instances (optional)',
});
if (networkingStack) sampleStack.addDependency(networkingStack);