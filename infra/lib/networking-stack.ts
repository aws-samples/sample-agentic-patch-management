import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export class NetworkingStack extends cdk.Stack {
  public readonly vpc: ec2.IVpc;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Create VPC with proper enterprise setup
    this.vpc = new ec2.Vpc(this, 'VPC', {
      ipAddresses: ec2.IpAddresses.cidr('10.0.0.0/16'),
      maxAzs: 3,
      
      // Public subnets for internet-facing resources
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
        {
          cidrMask: 28,
          name: 'Isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
        }
      ],

      // Enable DNS
      enableDnsHostnames: true,
      enableDnsSupport: true,

      // NAT Gateways for private subnet internet access
      natGateways: 2,
    });

    // Add VPC Flow Logs for security monitoring
    const flowLog = new ec2.FlowLog(this, 'VPCFlowLog', {
      resourceType: ec2.FlowLogResourceType.fromVpc(this.vpc),
      destination: ec2.FlowLogDestination.toCloudWatchLogs(),
    });

    // A month of flow logs, removed with the stack. The group the flow log
    // creates is changed in place, so its logical ID (and the flow log) stay.
    const flowLogGroup = flowLog.node.findChild('LogGroup').node.defaultChild as logs.CfnLogGroup;
    flowLogGroup.retentionInDays = logs.RetentionDays.ONE_MONTH;
    flowLogGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Tag all VPC resources
    cdk.Tags.of(this.vpc).add('Project', 'Patchy');

    // Output VPC details
    new cdk.CfnOutput(this, 'VPCId', {
      value: this.vpc.vpcId,
      description: 'VPC ID for Patch Automation'
    });

    new cdk.CfnOutput(this, 'PublicSubnets', {
      value: this.vpc.publicSubnets.map(subnet => subnet.subnetId).join(','),
      description: 'Public subnet IDs'
    });

    new cdk.CfnOutput(this, 'PrivateSubnets', {
      value: this.vpc.privateSubnets.map(subnet => subnet.subnetId).join(','),
      description: 'Private subnet IDs'
    });
  }
}
