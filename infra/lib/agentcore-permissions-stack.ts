import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class AgentCorePermissionsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Create comprehensive IAM policy for AgentCore runtime
    const agentCorePolicy = new iam.ManagedPolicy(this, 'AgentCorePatchAutomationPolicy', {
      managedPolicyName: 'AgentCorePatchAutomationPolicy',
      description: 'Comprehensive permissions for AgentCore patch automation runtime',
      statements: [
        // EC2 Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:DescribeInstances',
            'ec2:DescribeTags',
            'ec2:DescribeInstanceStatus',
            'ec2:DescribeImages',
            'ec2:DescribeSecurityGroups',
            'ec2:DescribeVpcs',
            'ec2:DescribeSubnets'
          ],
          resources: ['*']
        }),
        
        // Systems Manager Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:DescribeInstanceInformation',
            'ssm:ListComplianceItems',
            'ssm:SendCommand',
            'ssm:DescribeMaintenanceWindows',
            'ssm:CreatePatchBaseline',
            'ssm:GetCommandInvocation',
            'ssm:DescribeInstancePatches',
            'ssm:DescribePatchBaselines',
            'ssm:GetPatchBaseline',
            'ssm:ListCommandInvocations',
            'ssm:DescribeInstancePatchStates'
          ],
          resources: ['*']
        }),
        
        // Inspector Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'inspector2:ListFindings',
            'inspector2:GetFindingDetails',
            'inspector2:BatchGetFreeTrialInfo',
            'inspector2:DescribeOrganizationConfiguration',
            'inspector2:GetConfiguration'
          ],
          resources: ['*']
        }),
        
        // Config Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'config:GetComplianceDetailsByConfigRule',
            'config:GetComplianceDetailsByResource',
            'config:DescribeConfigRules',
            'config:DescribeComplianceByConfigRule',
            'config:DescribeComplianceByResource'
          ],
          resources: ['*']
        }),
        
        // CloudWatch Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cloudwatch:GetMetricStatistics',
            'cloudwatch:ListMetrics',
            'cloudwatch:GetMetricData',
            'logs:DescribeLogGroups',
            'logs:DescribeLogStreams',
            'logs:GetLogEvents'
          ],
          resources: ['*']
        })
      ]
    });

    // Output the policy ARN for reference
    new cdk.CfnOutput(this, 'AgentCorePolicyArn', {
      value: agentCorePolicy.managedPolicyArn,
      description: 'ARN of the AgentCore patch automation policy',
      exportName: 'AgentCorePatchAutomationPolicyArn'
    });
  }
}
