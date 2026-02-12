import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as config from 'aws-cdk-lib/aws-config';
import { Construct } from 'constructs';

interface SecurityStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
}

export class SecurityStack extends cdk.Stack {
  public readonly instanceSecurityGroup: ec2.SecurityGroup;
  public readonly instanceRole: iam.Role;

  constructor(scope: Construct, id: string, props: SecurityStackProps) {
    super(scope, id, props);

    // Security Group for EC2 instances
    this.instanceSecurityGroup = new ec2.SecurityGroup(this, 'InstanceSecurityGroup', {
      vpc: props.vpc,
      description: 'Security group for patch automation managed instances',
      allowAllOutbound: true
    });

    // HTTP/HTTPS restricted to security group (ALB → instance traffic only)
    this.instanceSecurityGroup.addIngressRule(
      this.instanceSecurityGroup,
      ec2.Port.tcp(80),
      'HTTP from ALB only'
    );

    this.instanceSecurityGroup.addIngressRule(
      this.instanceSecurityGroup,
      ec2.Port.tcp(443),
      'HTTPS from ALB only'
    );

    // IAM Role for EC2 instances with Systems Manager permissions
    this.instanceRole = new iam.Role(this, 'InstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      description: 'IAM role for patch automation managed instances',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchAgentServerPolicy'),
      ]
    });

    // Additional permissions for patch management
    this.instanceRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ssm:UpdateInstanceInformation',
        'ssm:SendCommand',
        'ssm:ListCommands',
        'ssm:ListCommandInvocations',
        'ssm:DescribeInstanceInformation',
        'ssm:GetDeployablePatchSnapshotForInstance',
        'ssm:GetDefaultPatchBaseline',
        'ssm:GetManifest',
        'ssm:GetParameter',
        'ssm:GetParameters',
        'ssm:ListAssociations',
        'ssm:ListInstanceAssociations',
        'ssm:PutInventory',
        'ssm:PutComplianceItems',
        'ssm:PutConfigurePackageResult',
        'ssm:UpdateAssociationStatus',
        'ssm:UpdateInstanceAssociationStatus',
        'ec2messages:AcknowledgeMessage',
        'ec2messages:DeleteMessage',
        'ec2messages:FailMessage',
        'ec2messages:GetEndpoint',
        'ec2messages:GetMessages',
        'ec2messages:SendReply'
      ],
      resources: ['*']
    }));

    // Instance Profile (no hardcoded name — avoids conflicts on redeploy)
    const instanceProfile = new iam.CfnInstanceProfile(this, 'InstanceProfile', {
      roles: [this.instanceRole.roleName],
    });

    // Agent permissions are defined in AgentCorePermissionsStack — no duplicates here.

    // Outputs
    new cdk.CfnOutput(this, 'InstanceSecurityGroupId', {
      value: this.instanceSecurityGroup.securityGroupId,
      description: 'Security Group ID for instances'
    });

    new cdk.CfnOutput(this, 'InstanceRoleArn', {
      value: this.instanceRole.roleArn,
      description: 'IAM Role ARN for instances'
    });

    // AWS Config for resource dependency tracking
    // NOTE: Many accounts already have a Config recorder. If deployment fails with
    // "ConfigurationRecorder already exists", set CDK context flag to skip:
    //   cdk deploy -c skipConfigRecorder=true
    const skipConfig = this.node.tryGetContext('skipConfigRecorder') === 'true' ||
                       this.node.tryGetContext('skipConfigRecorder') === true;

    if (!skipConfig) {
      const configBucket = new s3.Bucket(this, 'ConfigBucket', {
        bucketName: `patch-automation-config-${cdk.Stack.of(this).account}`,
        versioned: true,
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
        lifecycleRules: [{
          expiration: cdk.Duration.days(90),
          noncurrentVersionExpiration: cdk.Duration.days(30)
        }]
      });

      const configRole = new iam.Role(this, 'ConfigRole', {
        assumedBy: new iam.ServicePrincipal('config.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWS_ConfigRole')
        ]
      });

      configBucket.grantWrite(configRole);

      new config.CfnDeliveryChannel(this, 'ConfigDeliveryChannel', {
        s3BucketName: configBucket.bucketName
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
            'AWS::ElasticLoadBalancingV2::TargetGroup'
          ]
        }
      });

      new cdk.CfnOutput(this, 'ConfigBucketName', {
        value: configBucket.bucketName,
        description: 'S3 bucket for AWS Config snapshots'
      });

      new cdk.CfnOutput(this, 'ConfigRoleArn', {
        value: configRole.roleArn,
        description: 'IAM Role ARN for AWS Config'
      });
    }

    // S3 bucket for compliance reports
    const complianceReportsBucket = new s3.Bucket(this, 'ComplianceReportsBucket', {
      bucketName: `patch-compliance-reports-${cdk.Stack.of(this).account}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [{
        expiration: cdk.Duration.days(365)
      }]
    });

    // Grant AgentCore write access to compliance reports bucket
    // Uses IAM role grant instead of bucket policy to avoid "policy already exists" errors on redeploy
    const agentCoreRoleArn = this.node.tryGetContext('agentCoreRoleArn');
    if (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRuntimeRole', agentCoreRoleArn);
      complianceReportsBucket.grantReadWrite(agentCoreRole);
    }

    new cdk.CfnOutput(this, 'ComplianceReportsBucketName', {
      value: complianceReportsBucket.bucketName,
      description: 'S3 bucket for patch compliance reports'
    });

    // SQS FIFO queue for real-time workflow event transport
    // Agent (AgentCore container) sends events -> FastAPI SSE endpoint receives them
    const workflowEventsQueue = new sqs.Queue(this, 'WorkflowEventsQueue', {
      queueName: 'patch-automation-workflow-events.fifo',
      fifo: true,
      contentBasedDeduplication: false,  // We provide explicit dedup IDs
      retentionPeriod: cdk.Duration.hours(1),  // Events are ephemeral
      visibilityTimeout: cdk.Duration.seconds(30),
      receiveMessageWaitTime: cdk.Duration.seconds(1),  // Enable long polling
    });

    // Grant AgentCore role access to send/receive workflow events
    if (agentCoreRoleArn && agentCoreRoleArn !== 'placeholder') {
      const agentCoreRole = iam.Role.fromRoleArn(this, 'AgentCoreRoleForSqs', agentCoreRoleArn);
      workflowEventsQueue.grantSendMessages(agentCoreRole);
      workflowEventsQueue.grantConsumeMessages(agentCoreRole);
    }

    new cdk.CfnOutput(this, 'WorkflowEventsQueueUrl', {
      value: workflowEventsQueue.queueUrl,
      description: 'SQS FIFO queue URL for workflow event transport',
      exportName: 'WorkflowEventsQueueUrl',
    });

    new cdk.CfnOutput(this, 'WorkflowEventsQueueArn', {
      value: workflowEventsQueue.queueArn,
      description: 'SQS FIFO queue ARN for workflow event transport',
    });
  }
}
