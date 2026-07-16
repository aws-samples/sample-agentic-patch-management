import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface CoreStackProps extends cdk.StackProps {
  agentCoreRoleArn?: string;
}

/**
 * Patchy-Core: S3 compliance bucket and AgentCore IAM policy.
 * Always deployed. No VPC dependency.
 */
export class CoreStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: CoreStackProps) {
    super(scope, id, props);

    // ── S3 compliance reports bucket ───────────────────────────────
    // NOTE: removalPolicy is DESTROY for clean teardown. For production,
    // change to RETAIN and remove autoDeleteObjects to preserve audit data.
    const complianceReportsBucket = new s3.Bucket(this, 'ComplianceReportsBucket', {
      bucketName: `patch-compliance-reports-${this.account}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(365) }],
    });

    // Allow spoke accounts to read baseline override files for severity-scoped patching.
    // Scoped to baseline-overrides/* prefix only — spokes cannot read compliance reports.
    complianceReportsBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'SpokeBaselineOverrideAccess',
      effect: iam.Effect.ALLOW,
      principals: [new iam.AnyPrincipal()],
      actions: ['s3:GetObject'],
      resources: [`${complianceReportsBucket.bucketArn}/baseline-overrides/*`],
      conditions: process.env.AWS_ORG_ID ? {
        StringEquals: { 'aws:PrincipalOrgID': process.env.AWS_ORG_ID },
      } : {
        StringEquals: { 'aws:PrincipalOrgID': 'NONE' },  // No org ID — deny all cross-account access
      },
    }));

    new cdk.CfnOutput(this, 'ComplianceReportsBucketName', {
      value: complianceReportsBucket.bucketName,
      description: 'S3 bucket for patch compliance reports',
    });

    // ── AgentCore IAM policy ───────────────────────────────────────
    const agentCorePolicy = new iam.ManagedPolicy(this, 'AgentCorePolicy', {
      managedPolicyName: 'PatchyAgentCorePolicy',
      description: 'IAM permissions for Patchy AgentCore runtime',
      statements: [
        // ec2:Describe* do not support resource-level permissions (AWS limitation).
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:DescribeInstances', 'ec2:DescribeTags', 'ec2:DescribeInstanceStatus',
            'ec2:DescribeImages', 'ec2:DescribeSecurityGroups', 'ec2:DescribeVpcs',
            'ec2:DescribeSubnets',
          ],
          resources: ['*'],
        }),
        // SSM read APIs do not support resource-level permissions (AWS limitation).
        new iam.PolicyStatement({
          sid: 'SSMRead',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:DescribeInstanceInformation', 'ssm:ListComplianceItems',
            'ssm:GetCommandInvocation', 'ssm:ListCommandInvocations',
            'ssm:ListCommands',
            'ssm:DescribeInstancePatches', 'ssm:DescribeInstancePatchStates',
            // Maintenance Window read
            'ssm:DescribeMaintenanceWindows', 'ssm:GetMaintenanceWindow',
            'ssm:DescribeMaintenanceWindowTargets', 'ssm:DescribeMaintenanceWindowTasks',
            // Patch Baseline read
            'ssm:DescribePatchBaselines', 'ssm:GetPatchBaseline',
            // Other read APIs
            'ssm:ListInventoryEntries',
            'ssm:ListAssociations', 'ssm:DescribeAssociation',
            'ssm:GetOpsSummary',
          ],
          resources: ['*'],
        }),
        // SSM create — tag-on-create constraint guarantees every resource the
        // agent creates carries ManagedBy=IntelligentPatchAutomation. The
        // Modify statement below uses the same tag to scope update/delete.
        // Without this constraint, an untagged orphan resource could be
        // created and immediately become unmanageable by the agent.
        new iam.PolicyStatement({
          sid: 'SSMCreate',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:CreateMaintenanceWindow',
            'ssm:CreatePatchBaseline',
          ],
          resources: ['*'],
          conditions: {
            StringEquals: {
              'aws:RequestTag/ManagedBy': 'IntelligentPatchAutomation',
            },
            'ForAllValues:StringEquals': {
              // Restricts the *set* of tag keys allowed on creation. Customers
              // who need additional tags (e.g., cost-allocation) should add
              // them to this list before deploying.
              'aws:TagKeys': ['ManagedBy', 'Project', 'Environment', 'Owner', 'Team'],
            },
          },
        }),
        // SSM modify — restricted to resources the agent created (tagged
        // ManagedBy=IntelligentPatchAutomation). Customer-owned maintenance
        // windows and patch baselines (which lack this tag) are off-limits.
        new iam.PolicyStatement({
          sid: 'SSMModify',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:UpdateMaintenanceWindow', 'ssm:DeleteMaintenanceWindow',
            'ssm:RegisterTargetWithMaintenanceWindow', 'ssm:RegisterTaskWithMaintenanceWindow',
            'ssm:DeregisterTargetFromMaintenanceWindow', 'ssm:DeregisterTaskFromMaintenanceWindow',
            'ssm:UpdateMaintenanceWindowTarget', 'ssm:UpdateMaintenanceWindowTask',
            'ssm:UpdatePatchBaseline', 'ssm:DeletePatchBaseline',
          ],
          resources: [
            `arn:aws:ssm:${this.region}:${this.account}:maintenancewindow/*`,
            `arn:aws:ssm:${this.region}:${this.account}:patchbaseline/*`,
          ],
          conditions: {
            StringEquals: {
              'aws:ResourceTag/ManagedBy': 'IntelligentPatchAutomation',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:SendCommand'],
          resources: [
            `arn:aws:ssm:${this.region}::document/AWS-RunPatchBaseline`,
            `arn:aws:ssm:${this.region}::document/AWS-RunShellScript`,
          ],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:SendCommand'],
          resources: [`arn:aws:ec2:${this.region}:${this.account}:instance/*`],
          conditions: {
            StringEquals: {
              [`ssm:resourceTag/${process.env.SSM_SCOPE_TAG_KEY || 'PatchAutomation'}`]:
                process.env.SSM_SCOPE_TAG_VALUE || 'enabled',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['s3:PutObject', 's3:GetObject', 's3:ListBucket'],
          resources: [
            `arn:aws:s3:::patch-compliance-reports-${this.account}`,
            `arn:aws:s3:::patch-compliance-reports-${this.account}/*`,
          ],
        }),
        // Inspector2 APIs do not support resource-level permissions (AWS limitation).
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'inspector2:ListFindings', 'inspector2:BatchGetFreeTrialInfo',
            'inspector2:DescribeOrganizationConfiguration', 'inspector2:GetConfiguration',
          ],
          resources: ['*'],
        }),
        // CloudWatch/Logs read APIs do not support resource-level permissions (AWS limitation).
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cloudwatch:GetMetricStatistics', 'cloudwatch:ListMetrics',
            'cloudwatch:GetMetricData', 'cloudwatch:DescribeAlarms',
            'cloudwatch:DescribeAlarmHistory',
            'logs:DescribeLogGroups', 'logs:DescribeLogStreams', 'logs:GetLogEvents',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['iam:CreateServiceLinkedRole'],
          resources: ['arn:aws:iam::*:role/aws-service-role/ssm.amazonaws.com/AWSServiceRoleForAmazonSSM'],
          conditions: { StringLike: { 'iam:AWSServiceName': 'ssm.amazonaws.com' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['iam:PassRole'],
          resources: [`arn:aws:iam::${this.account}:role/*`],
          conditions: { StringEquals: { 'iam:PassedToService': 'ssm.amazonaws.com' } },
        }),
        // Read-only for use_aws fallback tool
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:Describe*', 'autoscaling:Describe*', 'tag:Get*'],
          resources: ['*'],
        }),
        // Cross-account: assume spoke roles. Only added when MULTI_ACCOUNT_ENABLED=true,
        // and AWS_ORG_ID is mandatory in that case so the assume-role permission is
        // always scoped by aws:PrincipalOrgID. Single-account deploys do not need this
        // statement at all (no spoke roles to assume).
        ...(process.env.MULTI_ACCOUNT_ENABLED === 'true' ? [
          new iam.PolicyStatement({
            sid: 'CrossAccountAssumeRole',
            effect: iam.Effect.ALLOW,
            actions: ['sts:AssumeRole'],
            resources: [`arn:aws:iam::*:role/${process.env.SPOKE_EXECUTION_ROLE || 'PatchySpokeRole'}`],
            conditions: {
              StringEquals: {
                'aws:PrincipalOrgID': (() => {
                  if (!process.env.AWS_ORG_ID) {
                    throw new Error(
                      'AWS_ORG_ID is required when MULTI_ACCOUNT_ENABLED=true. ' +
                      'Set it in .env so the cross-account assume-role policy is ' +
                      'scoped to your organization. Get it via: ' +
                      "aws organizations describe-organization --query 'Organization.Id' --output text"
                    );
                  }
                  return process.env.AWS_ORG_ID;
                })(),
              },
            },
          }),
        ] : []),
        // Organizations APIs require Resource: "*" (AWS service limitation).
        new iam.PolicyStatement({
          sid: 'OrganizationsDiscovery',
          effect: iam.Effect.ALLOW,
          actions: [
            'organizations:ListAccounts',
            'organizations:ListAccountsForParent',
            'organizations:ListTagsForResource',
          ],
          resources: ['*'],
        }),
        // SSM Automation APIs require Resource: "*" for cross-account execution
        // via TargetLocations (cannot scope to specific automation ARNs).
        new iam.PolicyStatement({
          sid: 'AutomationExecution',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:StartAutomationExecution',
            'ssm:GetAutomationExecution',
            'ssm:DescribeAutomationExecutions',
            'ssm:DescribeAutomationStepExecutions',
            'ssm:StopAutomationExecution',
          ],
          resources: ['*'],
        }),
      ],
    });

    // Grant AgentCore role access to compliance bucket + attach policy
    if (props?.agentCoreRoleArn) {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRole', props.agentCoreRoleArn);
      complianceReportsBucket.grantReadWrite(agentCoreRole);
      agentCorePolicy.attachToRole(agentCoreRole);
    }

    new cdk.CfnOutput(this, 'AgentCorePolicyArn', {
      value: agentCorePolicy.managedPolicyArn,
      description: 'ARN of the AgentCore patch automation policy',
    });

    // ── SSM Automation Documents ──────────────────────────────────────
    // Documents are deployed independently by Patchy-SsmDocs StackSet to every
    // (account, region) the agent fans out into — including the hub. Keeping
    // them out of CoreStack avoids drift between hub and spoke definitions and
    // lets operators update the doc YAML without redeploying CoreStack.
  }
}
