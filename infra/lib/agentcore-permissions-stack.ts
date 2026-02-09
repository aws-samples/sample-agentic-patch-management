import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface AgentCorePermissionsStackProps extends cdk.StackProps {
  agentCoreRoleArn?: string;
}

export class AgentCorePermissionsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: AgentCorePermissionsStackProps) {
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
            'ssm:GetCommandInvocation',
            'ssm:ListCommandInvocations',
            'ssm:DescribeInstancePatches',
            'ssm:DescribeInstancePatchStates',
            'ssm:DescribeMaintenanceWindows',
            'ssm:GetMaintenanceWindow',
            'ssm:DescribeMaintenanceWindowTargets',
            'ssm:RegisterTaskWithMaintenanceWindow',
            'ssm:DescribeMaintenanceWindowTasks',
            'ssm:DescribePatchBaselines',
            'ssm:GetPatchBaseline',
            'ssm:CreatePatchBaseline',
            'ssm:UpdatePatchBaseline',
            'ssm:DeletePatchBaseline',
            'ssm:ListInventoryEntries'
          ],
          resources: ['*']
        }),
        
        // S3 Permissions for Compliance Reports
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3:PutObject',
            's3:GetObject',
            's3:ListBucket'
          ],
          resources: [
            `arn:aws:s3:::patch-compliance-reports-${this.account}`,
            `arn:aws:s3:::patch-compliance-reports-${this.account}/*`
          ]
        }),
        
        // Inspector Permissions
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'inspector2:ListFindings',
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
            'cloudwatch:DescribeAlarms',
            'cloudwatch:DescribeAlarmHistory',
            'logs:DescribeLogGroups',
            'logs:DescribeLogStreams',
            'logs:GetLogEvents'
          ],
          resources: ['*']
        }),
        
        // SSM Service-Linked Role (required for maintenance window task registration)
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'iam:CreateServiceLinkedRole'
          ],
          resources: ['arn:aws:iam::*:role/aws-service-role/ssm.amazonaws.com/AWSServiceRoleForAmazonSSM'],
          conditions: {
            'StringLike': {
              'iam:AWSServiceName': 'ssm.amazonaws.com'
            }
          }
        }),
        
        // SSM PassRole for maintenance window task execution
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'iam:PassRole'
          ],
          resources: ['*'],
          conditions: {
            'StringEquals': {
              'iam:PassedToService': 'ssm.amazonaws.com'
            }
          }
        }),
        
        // Broad Read Permissions for use_aws tool (demo purposes)
        // Allows querying Lambda, S3, RDS, and other AWS services
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'lambda:List*',
            'lambda:Get*',
            'lambda:Describe*',
            's3:List*',
            's3:Get*',
            'rds:Describe*',
            'dynamodb:List*',
            'dynamodb:Describe*',
            'ecs:List*',
            'ecs:Describe*',
            'eks:List*',
            'eks:Describe*',
            'elasticloadbalancing:Describe*',
            'autoscaling:Describe*',
            'cloudformation:List*',
            'cloudformation:Describe*',
            'cloudformation:Get*',
            'iam:List*',
            'iam:Get*',
            'sns:List*',
            'sns:Get*',
            'sqs:List*',
            'sqs:Get*',
            'apigateway:GET',
            'route53:List*',
            'route53:Get*',
            'elasticache:Describe*',
            'elasticbeanstalk:Describe*',
            'kinesis:List*',
            'kinesis:Describe*',
            'firehose:List*',
            'firehose:Describe*',
            'glue:Get*',
            'glue:List*',
            'athena:List*',
            'athena:Get*',
            'states:List*',
            'states:Describe*',
            'events:List*',
            'events:Describe*',
            'backup:List*',
            'backup:Describe*',
            'tag:Get*'
          ],
          resources: ['*']
        })
      ]
    });

    // Attach policy to existing AgentCore role if ARN provided
    if (props?.agentCoreRoleArn) {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRole', props.agentCoreRoleArn);
      agentCorePolicy.attachToRole(agentCoreRole);
    }

    // Output the policy ARN for reference
    new cdk.CfnOutput(this, 'AgentCorePolicyArn', {
      value: agentCorePolicy.managedPolicyArn,
      description: 'ARN of the AgentCore patch automation policy',
      exportName: 'AgentCorePatchAutomationPolicyArn'
    });
  }
}
