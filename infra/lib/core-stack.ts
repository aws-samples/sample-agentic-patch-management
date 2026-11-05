import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { assumeSpokeRoleStatement } from './common';

/** Bucket prefixes whose objects expire after a year ('2' is the dated reports, YYYY/MM/DD/). */
export const EXPIRING_PREFIXES = ['plans/', 'plan-runs/', 'plan-decisions/', 'plan-executions/', 'run-executions/',
  'promotions/', 'session-owners/', 'pending-reports/', '2'];

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
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      // Run records and reports expire after a year. Lifecycle rules can't
      // exclude a prefix, so expiry is scoped to each record prefix and the
      // dated reports (2), which leaves baseline-overrides/ and
      // install-overrides/ in place: instances read them on every patch run
      // and only a full deploy writes the baseline overrides again. Old
      // versions of any object go after 30 days. A new record prefix needs
      // adding to EXPIRING_PREFIXES, or its objects are kept.
      lifecycleRules: [
        ...EXPIRING_PREFIXES.map((prefix) => ({
          id: prefix === '2' ? 'ExpireReports' : `Expire-${prefix.slice(0, -1)}`,
          prefix,
          expiration: cdk.Duration.days(365),
        })),
        {
          id: 'ExpireOldVersions',
          noncurrentVersionExpiration: cdk.Duration.days(30),
          expiredObjectDeleteMarker: true,
        },
      ],
    });

    // Let instances read their override files: baseline overrides for
    // severity-scoped patching and install lists for CVE-scoped patching.
    // Scoped to those prefixes only, so instances can't read compliance reports.
    // Without an organisation, the hub's own principals: within one account this
    // statement is enough on its own, so instance roles need no S3 grant. Spoke
    // instances still need the grant in their own role (a cross-account read
    // needs both sides).
    complianceReportsBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'SpokeBaselineOverrideAccess',
      effect: iam.Effect.ALLOW,
      principals: [new iam.AnyPrincipal()],
      actions: ['s3:GetObject'],
      resources: [`${complianceReportsBucket.bucketArn}/baseline-overrides/*`,
                  `${complianceReportsBucket.bucketArn}/install-overrides/*`],
      conditions: process.env.AWS_ORG_ID ? {
        StringEquals: { 'aws:PrincipalOrgID': process.env.AWS_ORG_ID },
      } : {
        StringEquals: { 'aws:PrincipalAccount': this.account },
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
            // A window task's Operation (Install or Scan) is only in its invocation parameters.
            'ssm:GetMaintenanceWindowTask',
            // Patch Baseline read
            'ssm:DescribePatchBaselines', 'ssm:GetPatchBaseline',
            // Other read APIs
            'ssm:ListInventoryEntries',
            'ssm:ListAssociations', 'ssm:DescribeAssociation',
            'ssm:GetOpsSummary',
            // Change Calendar check before planning and before a run (CHANGE_CALENDAR)
            'ssm:GetCalendarState',
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
          // AddTagsToResource is what tag-on-create calls; no code tags anything
          // else, and the model has no general AWS tool.
          actions: ['ssm:CreateMaintenanceWindow', 'ssm:AddTagsToResource'],
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
        // SSM modify — only what create_maintenance_window does to the window it
        // just created (tagged ManagedBy=IntelligentPatchAutomation): register its
        // target and patch task, or delete it if the task can't be registered.
        // Customer-owned windows (which lack the tag) are off-limits.
        new iam.PolicyStatement({
          sid: 'SSMModify',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:RegisterTargetWithMaintenanceWindow', 'ssm:RegisterTaskWithMaintenanceWindow',
            'ssm:DeleteMaintenanceWindow',
          ],
          resources: [`arn:aws:ssm:${this.region}:${this.account}:maintenancewindow/*`],
          conditions: {
            StringEquals: {
              'aws:ResourceTag/ManagedBy': 'IntelligentPatchAutomation',
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
        // Decisions (approve or cancel) are written only by the UI's approval
        // API. The deny overrides the bucket grant, so the agent can read an
        // approval but never create one for its own plan.
        new iam.PolicyStatement({
          sid: 'NoPlanApprovals',
          effect: iam.Effect.DENY,
          actions: ['s3:PutObject', 's3:DeleteObject'],
          resources: ['plan-decisions']
            .map((prefix) => `arn:aws:s3:::patch-compliance-reports-${this.account}/${prefix}/*`),
        }),
        // Plans, run claims, run records, session owners, pending report
        // contexts and CVE install lists are created once and never
        // rewritten: a write without If-None-Match is refused. promotions/ is
        // left out: the runtime both writes and reads it, so a create-only
        // rule there would stop nothing a compromised runtime couldn't do.
        new iam.PolicyStatement({
          sid: 'CreateOnlyRecords',
          effect: iam.Effect.DENY,
          actions: ['s3:PutObject'],
          resources: ['plans', 'plan-runs', 'run-executions', 'plan-executions', 'session-owners', 'pending-reports', 'install-overrides']
            .map((prefix) => `arn:aws:s3:::patch-compliance-reports-${this.account}/${prefix}/*`),
          conditions: { Null: { 's3:if-none-match': 'true' } },
        }),
        // Baseline overrides can set yum repositories (Sources) on every
        // patched instance, so only the deploy machine writes them
        // (setup_baseline_overrides.py).
        // Final reports (YYYY/MM/DD/...) are written only by the reconciler
        // in the UI and the report Lambda.
        new iam.PolicyStatement({
          sid: 'NoReportWrites',
          effect: iam.Effect.DENY,
          actions: ['s3:PutObject'],
          resources: [`arn:aws:s3:::patch-compliance-reports-${this.account}/2*`],
        }),
        new iam.PolicyStatement({
          sid: 'NoBaselineOverrideWrites',
          effect: iam.Effect.DENY,
          actions: ['s3:PutObject'],
          resources: [`arn:aws:s3:::patch-compliance-reports-${this.account}/baseline-overrides/*`],
        }),
        // The runtime never deletes. With versioning on, DeleteObjectVersion
        // would otherwise let it erase a cancellation or an approval.
        new iam.PolicyStatement({
          sid: 'NoDeletes',
          effect: iam.Effect.DENY,
          actions: ['s3:DeleteObject*'],
          resources: [`arn:aws:s3:::patch-compliance-reports-${this.account}/*`],
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
        // PassRole limited to the spoke execution role (also present in the hub),
        // the only role Patchy's Automation runs pass.
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['iam:PassRole'],
          resources: [
            `arn:aws:iam::${this.account}:role/${process.env.SPOKE_EXECUTION_ROLE || 'PatchySpokeRole'}`,
          ],
          conditions: { StringEquals: { 'iam:PassedToService': 'ssm.amazonaws.com' } },
        }),
        // Cross-account: assume spoke roles in this organisation. Only added
        // when MULTI_ACCOUNT_ENABLED=true; single-account deploys have no spoke
        // roles to assume.
        ...(process.env.MULTI_ACCOUNT_ENABLED === 'true' ? [
          (() => {
            const st = assumeSpokeRoleStatement(process.env.SPOKE_EXECUTION_ROLE || 'PatchySpokeRole',
                                                'the cross-account assume-role policy');
            st.sid = 'CrossAccountAssumeRole';
            return st;
          })(),
        ] : []),
        // Organizations APIs require Resource: "*" (AWS service limitation).
        new iam.PolicyStatement({
          sid: 'OrganizationsDiscovery',
          effect: iam.Effect.ALLOW,
          actions: [
            'organizations:ListAccounts',
            'organizations:ListAccountsForParent',
            // Nested OUs: an OU's accounts include those in its child OUs.
            'organizations:ListOrganizationalUnitsForParent',
            'organizations:ListTagsForResource',
            // The OUs above an account, to match organization patch policies' targets.
            'organizations:ListParents',
          ],
          resources: ['*'],
        }),
        // Quick Setup patch policies (the hub's own, and organization policies the
        // delegated administrator can read) count in the SLA check. Read-only.
        new iam.PolicyStatement({
          sid: 'QuickSetupPatchPolicies',
          effect: iam.Effect.ALLOW,
          actions: ['ssm-quicksetup:ListConfigurationManagers', 'ssm-quicksetup:GetConfigurationManager'],
          resources: ['*'],
        }),
        // With RUNTIME_AUTH=jwt the runtime reads the caller's email from their user
        // pool, since Cognito access tokens carry only the username. Read-only.
        new iam.PolicyStatement({
          sid: 'CognitoCallerEmail',
          effect: iam.Effect.ALLOW,
          actions: ['cognito-idp:AdminGetUser'],
          resources: [`arn:aws:cognito-idp:${this.region}:${this.account}:userpool/*`],
        }),
        // Starting an Automation is limited to Patchy's own runbooks, so the
        // runtime can't start AWS-owned or other documents with another role.
        new iam.PolicyStatement({
          sid: 'AutomationStart',
          effect: iam.Effect.ALLOW,
          actions: ['ssm:StartAutomationExecution'],
          resources: [`arn:aws:ssm:*:${this.account}:automation-definition/Patchy-*:*`],
        }),
        // Execution read and stop APIs act on execution IDs, which have no
        // document scope, so they stay on "*".
        new iam.PolicyStatement({
          sid: 'AutomationExecution',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:GetAutomationExecution',
            'ssm:DescribeAutomationExecutions',
            'ssm:DescribeAutomationStepExecutions',
            'ssm:StopAutomationExecution',
          ],
          resources: ['*'],
        }),
      ],
    });

    // Attach to the AgentCore role. Bucket access comes from the policy's
    // explicit Get/Put/List statement, not grantReadWrite, which would also
    // grant deletes.
    if (props?.agentCoreRoleArn) {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRole', props.agentCoreRoleArn);
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
