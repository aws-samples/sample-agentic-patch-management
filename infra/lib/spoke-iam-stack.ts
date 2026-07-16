import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface SpokeIamStackProps extends cdk.StackProps {
  /** Hub account ID that is allowed to assume this role. */
  hubAccountId: string;
  /** AgentCore role ARN in hub — tightens trust to this principal only. */
  agentCoreRoleArn?: string;
  /** Role name — must match SPOKE_EXECUTION_ROLE in .env (default: PatchySpokeRole). */
  roleName?: string;
}

/**
 * Patchy-SpokeIam: cross-account IAM role only.
 *
 * IAM roles are global — this stack must be deployed exactly once per spoke
 * account, not per region. The deploy script targets this StackSet at the
 * primary region only.
 *
 */
export class SpokeIamStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SpokeIamStackProps) {
    super(scope, id, props);

    const roleName = props.roleName || 'PatchySpokeRole';

    // Trust hub account, scoped to the role patterns Patchy actually uses.
    // The ArnLike condition restricts who in the hub can assume this role:
    //   - AgentCore-* / AmazonBedrockAgentCoreSDKRuntime-* — runtime patch operations
    //   - Patchy-UI-* — dashboard cross-account queries
    // SSM service principal is always trusted (Automation document execution).
    //
    // Sample-environment deploys (./sample-env.sh deploy) use CloudFormation
    // StackSets, which do not assume the spoke role — they use AWS-managed
    // execution roles in the spoke account directly. So tightening this trust
    // policy does not break sample-env deployment.
    const spokeRole = new iam.Role(this, 'SpokeRole', {
      roleName,
      assumedBy: new iam.CompositePrincipal(
        new iam.ArnPrincipal(`arn:aws:iam::${props.hubAccountId}:root`).withConditions({
          ArnLike: {
            'aws:PrincipalArn': [
              `arn:aws:iam::${props.hubAccountId}:role/AgentCore-*`,
              `arn:aws:iam::${props.hubAccountId}:role/AmazonBedrockAgentCoreSDKRuntime-*`,
              `arn:aws:iam::${props.hubAccountId}:role/Patchy-UI-*`,
            ],
          },
        }),
        new iam.ServicePrincipal('ssm.amazonaws.com'),
      ),
      maxSessionDuration: cdk.Duration.hours(1),
      description: 'Cross-account role for Patchy patch automation',
    });

    // EC2 — read-only for instance discovery and tag resolution.
    // ec2:Describe* actions do not support resource-level permissions (AWS limitation).
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EC2ReadOnly',
      actions: [
        'ec2:DescribeInstances',
        'ec2:DescribeTags',
        'ec2:DescribeInstanceStatus',
      ],
      resources: ['*'],
    }));

    // SSM — Read-only operations. Describe/Get/List actions do not support
    // resource-level permissions (AWS limitation).
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMReadOnly',
      actions: [
        'ssm:GetAutomationExecution',
        'ssm:DescribeAutomationExecutions',
        'ssm:GetCommandInvocation',
        'ssm:ListCommandInvocations',
        'ssm:ListCommands',
        'ssm:DescribeInstanceInformation',
        'ssm:DescribeInstancePatches',
        'ssm:DescribeInstancePatchStates',
        'ssm:GetDocument',
        'ssm:ListAssociations',
        'ssm:DescribeAssociation',
        // Maintenance window read — needed by get_maintenance_windows tool
        // when fanning out across spoke accounts to evaluate SLA window fit.
        'ssm:DescribeMaintenanceWindows',
        'ssm:GetMaintenanceWindow',
        'ssm:DescribeMaintenanceWindowTargets',
        'ssm:DescribeMaintenanceWindowTasks',
      ],
      resources: ['*'],
    }));

    // SSM — Automation execution (targets documents, not instances)
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMAutomation',
      actions: [
        'ssm:StartAutomationExecution',
      ],
      resources: ['*'],
    }));

    // SSM — SendCommand on documents (which docs can be invoked)
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMSendCommandDocs',
      actions: ['ssm:SendCommand'],
      resources: [
        `arn:aws:ssm:*::document/AWS-RunPatchBaseline`,
        `arn:aws:ssm:*::document/AWS-RunShellScript`,
        `arn:aws:ssm:*:${cdk.Aws.ACCOUNT_ID}:document/Patchy-*`,
      ],
    }));

    // SSM — SendCommand on instances (scoped to tagged instances only)
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMSendCommandInstances',
      actions: ['ssm:SendCommand'],
      resources: [`arn:aws:ec2:*:${cdk.Aws.ACCOUNT_ID}:instance/*`],
      conditions: {
        StringEquals: {
          'ssm:resourceTag/PatchAutomation': 'enabled',
        },
      },
    }));

    // Inspector — read-only. inspector2:ListFindings and GetConfiguration
    // do not support resource-level permissions (AWS service requirement).
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InspectorReadOnly',
      actions: [
        'inspector2:ListFindings',
        'inspector2:GetConfiguration',
      ],
      resources: ['*'],
    }));

    // CloudWatch — read-only for health checks. DescribeAlarms and
    // GetMetricData do not support resource-level permissions (AWS limitation).
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchReadOnly',
      actions: [
        'cloudwatch:DescribeAlarms',
        'cloudwatch:GetMetricData',
      ],
      resources: ['*'],
    }));

    // S3 — read baseline override files from hub's compliance bucket
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3BaselineOverrides',
      actions: ['s3:GetObject'],
      resources: [
        `arn:aws:s3:::patch-compliance-reports-${props.hubAccountId}/baseline-overrides/*`,
      ],
    }));

    new cdk.CfnOutput(this, 'SpokeRoleArn', {
      value: spokeRole.roleArn,
      description: 'ARN of the Patchy spoke role',
    });
  }
}
