#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkingStack } from '../lib/networking-stack';
import { SecurityStack } from '../lib/security-stack';
import { DemoInstancesStack } from '../lib/demo-instances-stack';
import { AgentCorePermissionsStack } from '../lib/agentcore-permissions-stack';

const app = new cdk.App();

// Environment configuration
const env = {
  account: '125140434174', // ri25-demo account
  region: 'us-east-1'
};

// Core networking infrastructure
const networkingStack = new NetworkingStack(app, 'PatchAutomationNetworking', {
  env,
  description: 'VPC, subnets, and networking for Intelligent Patch Automation'
});

// Security groups and IAM roles
const securityStack = new SecurityStack(app, 'PatchAutomationSecurity', {
  env,
  vpc: networkingStack.vpc,
  description: 'Security groups and IAM roles for Intelligent Patch Automation'
});

// Demo EC2 instances for patch automation
const demoInstancesStack = new DemoInstancesStack(app, 'PatchAutomationInstances', {
  env,
  vpc: networkingStack.vpc,
  securityGroup: securityStack.instanceSecurityGroup,
  instanceRole: securityStack.instanceRole,
  description: 'Demo EC2 instances for patch automation demonstration'
});

// AgentCore permissions for patch automation
const agentCorePermissionsStack = new AgentCorePermissionsStack(app, 'PatchAutomationAgentCorePermissions', {
  env,
  description: 'IAM permissions for AgentCore patch automation runtime'
});

// Add dependencies
securityStack.addDependency(networkingStack);
demoInstancesStack.addDependency(securityStack);
