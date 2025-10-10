#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DemoInstancesStack } from '../lib/demo-instances-stack';
import { Scenario1DemoStack } from '../lib/scenario1-demo-stack';
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

// Scenario 1 Demo Stack (idempotent - can destroy/recreate for demos)
const scenario1Stack = new Scenario1DemoStack(app, 'PatchAutomationScenario1', {
  env,
  vpc: networkingStack.vpc,
  securityGroup: securityStack.instanceSecurityGroup,
  instanceRole: securityStack.instanceRole,
  description: 'Scenario 1: CVE-to-Action Intelligence - 15 instances for phased rollout demo'
});

// Legacy demo instances stack (commented out - use Scenario1 instead)
// const demoInstancesStack = new DemoInstancesStack(app, 'PatchAutomationInstances', {
//   env,
//   vpc: networkingStack.vpc,
//   securityGroup: securityStack.instanceSecurityGroup,
//   instanceRole: securityStack.instanceRole,
//   description: 'Demo EC2 instances for patch automation demonstration (legacy)'
// });

// Add dependencies
securityStack.addDependency(networkingStack);
scenario1Stack.addDependency(securityStack);
