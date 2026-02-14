import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as config from 'aws-cdk-lib/aws-config';
import { Construct } from 'constructs';

export interface CoreStackProps extends cdk.StackProps {
  agentCoreRoleArn?: string;
}

/**
 * Patchy-Core: S3 compliance bucket, AWS Config recorder, and AgentCore IAM policy.
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

    new cdk.CfnOutput(this, 'ComplianceReportsBucketName', {
      value: complianceReportsBucket.bucketName,
      description: 'S3 bucket for patch compliance reports',
    });

    // ── AWS Config recorder ────────────────────────────────────────
    // Many accounts already have one. Skip with: -c skipConfigRecorder=true
    const skipConfig = this.node.tryGetContext('skipConfigRecorder') === 'true' ||
                       this.node.tryGetContext('skipConfigRecorder') === true;

    if (!skipConfig) {
      const configBucket = new s3.Bucket(this, 'ConfigBucket', {
        bucketName: `patch-automation-config-${this.account}`,
        versioned: true,
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
        lifecycleRules: [{
          expiration: cdk.Duration.days(90),
          noncurrentVersionExpiration: cdk.Duration.days(30),
        }],
      });

      const configRole = new iam.Role(this, 'ConfigRole', {
        assumedBy: new iam.ServicePrincipal('config.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWS_ConfigRole'),
        ],
      });
      configBucket.grantWrite(configRole);

      new config.CfnDeliveryChannel(this, 'ConfigDeliveryChannel', {
        s3BucketName: configBucket.bucketName,
      });
      new config.CfnConfigurationRecorder(this, 'ConfigRecorder', {
        roleArn: configRole.roleArn,
        recordingGroup: {
          allSupported: false,
          includeGlobalResourceTypes: false,
          resourceTypes: [
            'AWS::EC2::Instance', 'AWS::EC2::SecurityGroup', 'AWS::EC2::VPC',
            'AWS::EC2::Subnet', 'AWS::IAM::Role',
            'AWS::ElasticLoadBalancingV2::LoadBalancer',
            'AWS::ElasticLoadBalancingV2::TargetGroup',
          ],
        },
      });

      new cdk.CfnOutput(this, 'ConfigBucketName', {
        value: configBucket.bucketName,
        description: 'S3 bucket for AWS Config snapshots',
      });
    }

    // ── AgentCore IAM policy ───────────────────────────────────────
    const agentCorePolicy = new iam.ManagedPolicy(this, 'AgentCorePolicy', {
      managedPolicyName: 'PatchyAgentCorePolicy',
      description: 'IAM permissions for Patchy AgentCore runtime',
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:DescribeInstances', 'ec2:DescribeTags', 'ec2:DescribeInstanceStatus',
            'ec2:DescribeImages', 'ec2:DescribeSecurityGroups', 'ec2:DescribeVpcs',
            'ec2:DescribeSubnets',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ssm:DescribeInstanceInformation', 'ssm:ListComplianceItems',
            'ssm:SendCommand', 'ssm:GetCommandInvocation', 'ssm:ListCommandInvocations',
            'ssm:DescribeInstancePatches', 'ssm:DescribeInstancePatchStates',
            'ssm:DescribeMaintenanceWindows', 'ssm:GetMaintenanceWindow',
            'ssm:DescribeMaintenanceWindowTargets', 'ssm:RegisterTaskWithMaintenanceWindow',
            'ssm:DescribeMaintenanceWindowTasks', 'ssm:DescribePatchBaselines',
            'ssm:GetPatchBaseline', 'ssm:CreatePatchBaseline', 'ssm:UpdatePatchBaseline',
            'ssm:DeletePatchBaseline', 'ssm:ListInventoryEntries',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['s3:PutObject', 's3:GetObject', 's3:ListBucket'],
          resources: [
            `arn:aws:s3:::patch-compliance-reports-${this.account}`,
            `arn:aws:s3:::patch-compliance-reports-${this.account}/*`,
          ],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'inspector2:ListFindings', 'inspector2:BatchGetFreeTrialInfo',
            'inspector2:DescribeOrganizationConfiguration', 'inspector2:GetConfiguration',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'config:GetComplianceDetailsByConfigRule', 'config:GetComplianceDetailsByResource',
            'config:DescribeConfigRules', 'config:DescribeComplianceByConfigRule',
            'config:DescribeComplianceByResource',
          ],
          resources: ['*'],
        }),
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
  }
}
