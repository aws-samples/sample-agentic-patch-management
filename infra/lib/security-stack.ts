import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
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
      description: 'Security group for patch automation demo instances',
      allowAllOutbound: true
    });

    // Allow SSH access from anywhere (demo purposes only)
    this.instanceSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(22),
      'SSH access for demo'
    );

    // Allow HTTP/HTTPS for web servers
    this.instanceSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'HTTP access'
    );

    this.instanceSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'HTTPS access'
    );

    // IAM Role for EC2 instances with Systems Manager permissions
    this.instanceRole = new iam.Role(this, 'InstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      description: 'IAM role for patch automation demo instances',
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

    // Instance Profile
    const instanceProfile = new iam.CfnInstanceProfile(this, 'InstanceProfile', {
      roles: [this.instanceRole.roleName],
      instanceProfileName: 'PatchAutomationInstanceProfile'
    });

    // IAM Role for Patch Automation Agents
    const agentRole = new iam.Role(this, 'AgentRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com'),
      description: 'IAM role for Intelligent Patch Automation agents',
    });

    // Agent permissions for AWS services
    agentRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        // Inspector permissions
        'inspector2:ListFindings',
        'inspector2:GetFindingDetails',
        'inspector2:BatchGetAccountStatus',
        
        // Systems Manager permissions
        'ssm:DescribeInstanceInformation',
        'ssm:DescribeInstanceAssociations',
        'ssm:ListComplianceItems',
        'ssm:ListComplianceSummary',
        'ssm:DescribeMaintenanceWindows',
        'ssm:DescribeMaintenanceWindowTargets',
        'ssm:CreatePatchBaseline',
        'ssm:SendCommand',
        'ssm:GetCommandInvocation',
        'ssm:ListCommandInvocations',
        'ssm:CreateMaintenanceWindow',
        'ssm:RegisterTargetWithMaintenanceWindow',
        
        // EC2 permissions
        'ec2:DescribeInstances',
        'ec2:DescribeTags',
        'ec2:DescribeImages',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeSubnets',
        'ec2:DescribeVpcs'
      ],
      resources: ['*']
    }));

    // IAM Role for AgentCore Execution
    const agentCoreExecutionRole = new iam.Role(this, 'AgentCoreExecutionRole', {
      roleName: 'IntelligentPatchAutomation',
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('bedrock.amazonaws.com'),
        new iam.ServicePrincipal('lambda.amazonaws.com'),
        new iam.ServicePrincipal('ecs-tasks.amazonaws.com')
      ),
      description: 'Execution role for AgentCore intelligent patch automation',
    });

    // AgentCore execution permissions
    agentCoreExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        // Inspector permissions
        'inspector2:ListFindings',
        'inspector2:GetFindingDetails',
        'inspector2:BatchGetAccountStatus',
        
        // Systems Manager permissions
        'ssm:DescribeInstanceInformation',
        'ssm:DescribeInstanceAssociations',
        'ssm:ListComplianceItems',
        'ssm:ListComplianceSummary',
        'ssm:DescribeMaintenanceWindows',
        'ssm:DescribeMaintenanceWindowTargets',
        'ssm:CreatePatchBaseline',
        'ssm:SendCommand',
        'ssm:GetCommandInvocation',
        'ssm:ListCommandInvocations',
        'ssm:CreateMaintenanceWindow',
        'ssm:RegisterTargetWithMaintenanceWindow',
        
        // EC2 permissions
        'ec2:DescribeInstances',
        'ec2:DescribeTags',
        'ec2:DescribeImages',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeSubnets',
        'ec2:DescribeVpcs',
        
        // Bedrock permissions
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream'
      ],
      resources: ['*']
    }));

    // Outputs
    new cdk.CfnOutput(this, 'InstanceSecurityGroupId', {
      value: this.instanceSecurityGroup.securityGroupId,
      description: 'Security Group ID for instances'
    });

    new cdk.CfnOutput(this, 'InstanceRoleArn', {
      value: this.instanceRole.roleArn,
      description: 'IAM Role ARN for instances'
    });

    new cdk.CfnOutput(this, 'AgentRoleArn', {
      value: agentRole.roleArn,
      description: 'IAM Role ARN for patch automation agents'
    });

    new cdk.CfnOutput(this, 'AgentCoreExecutionRoleArn', {
      value: agentCoreExecutionRole.roleArn,
      description: 'IAM Role ARN for AgentCore execution'
    });

    // AWS Config for resource dependency tracking
    const configBucket = new s3.Bucket(this, 'ConfigBucket', {
      bucketName: `aws-config-cop407-${cdk.Stack.of(this).account}`,
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
      roleName: 'AWSConfigRole-cop407',
      assumedBy: new iam.ServicePrincipal('config.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWS_ConfigRole')
      ]
    });

    configBucket.grantWrite(configRole);

    const deliveryChannel = new config.CfnDeliveryChannel(this, 'ConfigDeliveryChannel', {
      s3BucketName: configBucket.bucketName
    });

    const configRecorder = new config.CfnConfigurationRecorder(this, 'ConfigRecorder', {
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
    // Note: Update this ARN to match your AgentCore runtime role after deployment
    // You can find it via: aws iam list-roles --query "Roles[?contains(RoleName, 'AgentCore')]"
    const agentCoreRoleArn = this.node.tryGetContext('agentCoreRoleArn') 
      || `arn:aws:iam::${cdk.Stack.of(this).account}:role/AmazonBedrockAgentCoreSDKRuntime-${cdk.Stack.of(this).region}-*`;
    
    complianceReportsBucket.addToResourcePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      principals: [new iam.ArnPrincipal(agentCoreRoleArn)],
      actions: ['s3:PutObject', 's3:GetObject', 's3:ListBucket'],
      resources: [
        complianceReportsBucket.bucketArn,
        `${complianceReportsBucket.bucketArn}/*`
      ]
    }));

    new cdk.CfnOutput(this, 'ComplianceReportsBucketName', {
      value: complianceReportsBucket.bucketName,
      description: 'S3 bucket for patch compliance reports'
    });
  }
}
