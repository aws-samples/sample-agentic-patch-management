import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as config from 'aws-cdk-lib/aws-config';
import { Construct } from 'constructs';

export interface ComplianceStackProps extends cdk.StackProps {
  agentCoreRoleArn?: string;
}

/**
 * Solution infrastructure: S3 compliance reports bucket and AWS Config recorder.
 * Always deployed regardless of whether the customer brings their own VPC/instances.
 */
export class ComplianceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: ComplianceStackProps) {
    super(scope, id, props);

    // ── S3 bucket for compliance reports ────────────────────────────
    // NOTE: removalPolicy is DESTROY for clean teardown. For production,
    // change to RETAIN and remove autoDeleteObjects to preserve audit data.
    const complianceReportsBucket = new s3.Bucket(this, 'ComplianceReportsBucket', {
      bucketName: `patch-compliance-reports-${cdk.Stack.of(this).account}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(365) }],
    });

    // Grant AgentCore read/write access
    if (props?.agentCoreRoleArn && props.agentCoreRoleArn !== 'placeholder') {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRuntimeRole', props.agentCoreRoleArn);
      complianceReportsBucket.grantReadWrite(agentCoreRole);
    }

    new cdk.CfnOutput(this, 'ComplianceReportsBucketName', {
      value: complianceReportsBucket.bucketName,
      description: 'S3 bucket for patch compliance reports',
    });

    // ── AWS Config recorder ────────────────────────────────────────
    // NOTE: Many accounts already have a Config recorder. If deployment fails
    // with "ConfigurationRecorder already exists", set CDK context flag:
    //   cdk deploy -c skipConfigRecorder=true
    const skipConfig = this.node.tryGetContext('skipConfigRecorder') === 'true' ||
                       this.node.tryGetContext('skipConfigRecorder') === true;

    if (!skipConfig) {
      const configBucket = new s3.Bucket(this, 'ConfigBucket', {
        bucketName: `patch-automation-config-${cdk.Stack.of(this).account}`,
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
            'AWS::EC2::Instance',
            'AWS::EC2::SecurityGroup',
            'AWS::EC2::VPC',
            'AWS::EC2::Subnet',
            'AWS::IAM::Role',
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
  }
}
