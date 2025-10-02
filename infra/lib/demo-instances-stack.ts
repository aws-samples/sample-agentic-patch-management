import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

interface DemoInstancesStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  securityGroup: ec2.SecurityGroup;
  instanceRole: iam.Role;
}

export class DemoInstancesStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DemoInstancesStackProps) {
    super(scope, id, props);

    // Instance Profile
    const instanceProfile = new iam.CfnInstanceProfile(this, 'InstanceProfile', {
      roles: [props.instanceRole.roleName]
    });

    // User data script for Systems Manager and demo setup
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      '#!/bin/bash',
      'yum update -y',
      
      // Install Systems Manager Agent
      'yum install -y amazon-ssm-agent',
      'systemctl enable amazon-ssm-agent',
      'systemctl start amazon-ssm-agent',
      
      // Install Apache for vulnerability demo
      'yum install -y httpd',
      'systemctl enable httpd',
      'systemctl start httpd',
      
      // Install older version of packages for vulnerability simulation
      'yum install -y nginx-1.20.1',
      
      // Create demo web content
      'echo "<h1>Patch Automation Demo Server</h1>" > /var/www/html/index.html',
      'echo "<p>Environment: $(curl -s http://169.254.169.254/latest/meta-data/tags/instance/Environment)</p>" >> /var/www/html/index.html',
      
      // Install CloudWatch agent
      'wget https://s3.amazonaws.com/amazoncloudwatch-agent/amazon_linux/amd64/latest/amazon-cloudwatch-agent.rpm',
      'rpm -U ./amazon-cloudwatch-agent.rpm'
    );

    // Production instances (2 instances)
    for (let i = 1; i <= 2; i++) {
      const prodInstance = new ec2.Instance(this, `ProductionInstance${i}`, {
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
        machineImage: ec2.MachineImage.latestAmazonLinux2(),
        securityGroup: props.securityGroup,
        role: props.instanceRole,
        userData: userData,
        keyName: 'patch-automation-demo' // You'll need to create this key pair
      });

      // Production tags
      cdk.Tags.of(prodInstance).add('Name', `Production-WebServer-${i}`);
      cdk.Tags.of(prodInstance).add('Environment', 'prod');
      cdk.Tags.of(prodInstance).add('Application', 'WebServer');
      cdk.Tags.of(prodInstance).add('Criticality', 'High');
      cdk.Tags.of(prodInstance).add('PatchGroup', 'Production');
    }

    // Staging instances (1 instance)
    const stagingInstance = new ec2.Instance(this, 'StagingInstance1', {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      machineImage: ec2.MachineImage.latestAmazonLinux2(),
      securityGroup: props.securityGroup,
      role: props.instanceRole,
      userData: userData,
      keyName: 'patch-automation-demo'
    });

    // Staging tags
    cdk.Tags.of(stagingInstance).add('Name', 'Staging-WebServer-1');
    cdk.Tags.of(stagingInstance).add('Environment', 'staging');
    cdk.Tags.of(stagingInstance).add('Application', 'WebServer');
    cdk.Tags.of(stagingInstance).add('Criticality', 'Medium');
    cdk.Tags.of(stagingInstance).add('PatchGroup', 'Staging');

    // Development instances (2 instances)
    for (let i = 1; i <= 2; i++) {
      const devInstance = new ec2.Instance(this, `DevelopmentInstance${i}`, {
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
        machineImage: ec2.MachineImage.latestAmazonLinux2(),
        securityGroup: props.securityGroup,
        role: props.instanceRole,
        userData: userData,
        keyName: 'patch-automation-demo'
      });

      // Development tags
      cdk.Tags.of(devInstance).add('Name', `Development-WebServer-${i}`);
      cdk.Tags.of(devInstance).add('Environment', 'dev');
      cdk.Tags.of(devInstance).add('Application', 'WebServer');
      cdk.Tags.of(devInstance).add('Criticality', 'Low');
      cdk.Tags.of(devInstance).add('PatchGroup', 'Development');
    }

    // Create patch baselines for different environments
    new ssm.CfnPatchBaseline(this, 'ProductionPatchBaseline', {
      name: 'Production-Baseline',
      description: 'Patch baseline for production systems',
      operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: {
        patchRules: [{
          patchFilterGroup: {
            patchFilters: [{
              key: 'SEVERITY',
              values: ['Critical', 'Important']
            }]
          },
          approveAfterDays: 7,
          enableNonSecurity: false
        }]
      },
      approvedPatches: [],
      rejectedPatches: []
    });

    new ssm.CfnPatchBaseline(this, 'DevelopmentPatchBaseline', {
      name: 'Development-Baseline',
      description: 'Patch baseline for development systems',
      operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: {
        patchRules: [{
          patchFilterGroup: {
            patchFilters: [{
              key: 'SEVERITY',
              values: ['Critical', 'Important', 'Medium', 'Low']
            }]
          },
          approveAfterDays: 0,
          enableNonSecurity: true
        }]
      }
    });

    // Create maintenance windows
    new ssm.CfnMaintenanceWindow(this, 'ProductionMaintenanceWindow', {
      name: 'Production-Weekly-Maintenance',
      description: 'Weekly maintenance window for production systems',
      schedule: 'cron(0 2 ? * SUN *)', // Sunday 2 AM
      duration: 4,
      cutoff: 1,
      allowUnassociatedTargets: false
    });

    new ssm.CfnMaintenanceWindow(this, 'DevelopmentMaintenanceWindow', {
      name: 'Development-Daily-Maintenance',
      description: 'Daily maintenance window for development systems',
      schedule: 'cron(0 1 ? * * *)', // Daily 1 AM
      duration: 2,
      cutoff: 0,
      allowUnassociatedTargets: false
    });
  }
}
