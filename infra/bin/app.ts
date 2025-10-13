#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { VulnerabilityDrivenPatchingStack } from '../lib/vulnerability-driven-patching-stack';
import { AgentCorePermissionsStack } from '../lib/agentcore-permissions-stack';

const app = new cdk.App();

// Environment configuration
const env = {
  account: '125140434174', // ri25-demo account
  region: 'us-east-1'
};

// Core networking infrastructure (foundational - keep deployed)
const networkingStack = new NetworkingStack(app, 'PatchAutomationNetworking', {
  env,
  description: 'VPC, subnets, and networking for Intelligent Patch Automation'
});

// Security groups and IAM roles (foundational - keep deployed)
const securityStack = new SecurityStack(app, 'PatchAutomationSecurity', {
  env,
  vpc: networkingStack.vpc,
  description: 'Security groups and IAM roles for Intelligent Patch Automation'
});

// AgentCore permissions (foundational - keep deployed)
const agentCorePermissionsStack = new AgentCorePermissionsStack(app, 'PatchAutomationAgentCorePermissions', {
  env,
  agentCoreRoleArn: 'arn:aws:iam::125140434174:role/AmazonBedrockAgentCoreSDKRuntime-us-east-1-a154b39750',
  description: 'IAM permissions for AgentCore patch automation runtime'
});

// Vulnerability-Driven Patching Stack (idempotent - can destroy/recreate for demos)
const vulnerabilityPatchingStack = new VulnerabilityDrivenPatchingStack(app, 'PatchAutomationVulnerabilityDriven', {
  env,
  vpc: networkingStack.vpc,
  securityGroup: securityStack.instanceSecurityGroup,
  instanceRole: securityStack.instanceRole,
  description: 'Vulnerability-driven patch automation: 15 instances for phased rollout (dev/staging/prod)'
});

// Add dependencies
securityStack.addDependency(networkingStack);
vulnerabilityPatchingStack.addDependency(securityStack);
