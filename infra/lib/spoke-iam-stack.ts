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
  /** Read-only role for the dashboard — must match SPOKE_READ_ROLE (default: PatchySpokeReadRole). */
  readRoleName?: string;
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
    const readRoleName = props.readRoleName || 'PatchySpokeReadRole';

    // Write role trust: the hub's AgentCore runtime role only. With the exact
    // ARN, no other hub principal can assume it, whatever its name. Without
    // it (spoke deployed before the agent), fall back to the runtime role
    // name patterns and warn, because any hub principal that can create a
    // role with a matching name could then assume this role.
    // The SSM service principal is always trusted (Automation execution).
    const runtimeTrust = props.agentCoreRoleArn
      ? { ArnEquals: { 'aws:PrincipalArn': props.agentCoreRoleArn } }
      : {
          ArnLike: {
            'aws:PrincipalArn': [
              `arn:aws:iam::${props.hubAccountId}:role/AgentCore-*`,
              `arn:aws:iam::${props.hubAccountId}:role/AmazonBedrockAgentCoreSDKRuntime-*`,
            ],
          },
        };
    if (!props.agentCoreRoleArn) {
      console.warn(
        '[Patchy-SpokeIam] agentCoreRoleArn not set: PatchySpokeRole trusts any hub role ' +
        'named AgentCore-* or AmazonBedrockAgentCoreSDKRuntime-*. Deploy the agent first, ' +
        'or pass -c agentCoreRoleArn=<arn>, to trust the runtime role only.'
      );
    }
    const spokeRole = new iam.Role(this, 'SpokeRole', {
      roleName,
      assumedBy: new iam.CompositePrincipal(
        new iam.ArnPrincipal(`arn:aws:iam::${props.hubAccountId}:root`).withConditions(runtimeTrust),
        new iam.ServicePrincipal('ssm.amazonaws.com'),
      ),
      maxSessionDuration: cdk.Duration.hours(1),
      description: 'Cross-account role for Patchy patch automation',
    });

    // Read role for the dashboard (Patchy-UI-* task role). It carries the
    // read statements below and nothing that can change an instance, so a
    // compromise of the internet-facing UI can't patch or run commands.
    const readRole = new iam.Role(this, 'SpokeReadRole', {
      roleName: readRoleName,
      assumedBy: new iam.ArnPrincipal(`arn:aws:iam::${props.hubAccountId}:root`).withConditions({
        ArnLike: { 'aws:PrincipalArn': `arn:aws:iam::${props.hubAccountId}:role/Patchy-UI-*` },
      }),
      maxSessionDuration: cdk.Duration.hours(1),
      description: 'Read-only cross-account role for the Patchy dashboard',
    });
    const bothRoles = [spokeRole, readRole];

    // EC2 — read-only for instance discovery and tag resolution.
    // ec2:Describe* actions do not support resource-level permissions (AWS limitation).
    for (const role of bothRoles) role.addToPolicy(new iam.PolicyStatement({
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
    for (const role of bothRoles) role.addToPolicy(new iam.PolicyStatement({
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

    // SSM — Automation execution, limited to Patchy's own runbooks so the
    // role can't start AWS-owned or other documents in the spoke.
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMAutomation',
      actions: [
        'ssm:StartAutomationExecution',
      ],
      resources: [`arn:aws:ssm:*:${cdk.Aws.ACCOUNT_ID}:automation-definition/Patchy-*:*`],
    }));

    // SSM — SendCommand on documents (which docs can be invoked)
    spokeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SSMSendCommandDocs',
      actions: ['ssm:SendCommand'],
      resources: [
        `arn:aws:ssm:*::document/AWS-RunPatchBaseline`,
        // Rollback runs through the Patchy-YumUndo Command document, so the
        // role has no general shell document on tagged instances.
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
          // Scope tag must track the configured key/value (matches core-stack).
          // Hardcoding it here would leave the spoke blast-radius boundary keyed
          // to 'PatchAutomation' even when SSM_SCOPE_TAG_KEY is reconfigured.
          [`ssm:resourceTag/${process.env.SSM_SCOPE_TAG_KEY || 'PatchAutomation'}`]:
            process.env.SSM_SCOPE_TAG_VALUE || 'enabled',
        },
      },
    }));

    // Inspector — read-only. inspector2:ListFindings and GetConfiguration
    // do not support resource-level permissions (AWS service requirement).
    for (const role of bothRoles) role.addToPolicy(new iam.PolicyStatement({
      sid: 'InspectorReadOnly',
      actions: [
        'inspector2:ListFindings',
        'inspector2:GetConfiguration',
      ],
      resources: ['*'],
    }));

    // CloudWatch — read-only for health checks. DescribeAlarms and
    // GetMetricData do not support resource-level permissions (AWS limitation).
    for (const role of bothRoles) role.addToPolicy(new iam.PolicyStatement({
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
    new cdk.CfnOutput(this, 'SpokeReadRoleArn', {
      value: readRole.roleArn,
      description: 'ARN of the Patchy read-only spoke role used by the dashboard',
    });
  }
}
