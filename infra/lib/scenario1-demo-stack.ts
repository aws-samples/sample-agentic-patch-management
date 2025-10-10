import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

interface Scenario1DemoStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  securityGroup: ec2.SecurityGroup;
  instanceRole: iam.Role;
}

export class Scenario1DemoStack extends cdk.Stack {
  public readonly instances: ec2.Instance[] = [];

  constructor(scope: Construct, id: string, props: Scenario1DemoStackProps) {
    super(scope, id, props);

    // User data for Apache installation and SSM Agent
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      '#!/bin/bash',
      'set -e',
      
      // Update system
      'yum update -y',
      
      // Install SSM Agent (should be pre-installed on Amazon Linux 2)
      'yum install -y amazon-ssm-agent',
      'systemctl enable amazon-ssm-agent',
      'systemctl start amazon-ssm-agent',
      
      // Install Apache (intentionally older version for vulnerability demo)
      'yum install -y httpd',
      'systemctl enable httpd',
      'systemctl start httpd',
      
      // Create simple web page
      'INSTANCE_ID=$(ec2-metadata --instance-id | cut -d " " -f 2)',
      'ENVIRONMENT=$(aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=Environment" --region us-east-1 --query "Tags[0].Value" --output text 2>/dev/null || echo "unknown")',
      'cat > /var/www/html/index.html << EOF',
      '<html>',
      '<head><title>Patch Demo Server</title></head>',
      '<body>',
      '<h1>Patch Automation Demo</h1>',
      '<p>Instance ID: $INSTANCE_ID</p>',
      '<p>Environment: $ENVIRONMENT</p>',
      '<p>Apache Version: $(httpd -v | head -1)</p>',
      '</body>',
      '</html>',
      'EOF',
      
      // Signal completion
      'echo "Instance setup complete" > /tmp/setup-complete.txt'
    );

    // Configuration for each environment
    const environments = [
      { name: 'dev', count: 5, criticality: 'Low' },
      { name: 'staging', count: 5, criticality: 'Medium' },
      { name: 'prod', count: 5, criticality: 'High' }
    ];

    // Create instances for each environment
    environments.forEach(env => {
      for (let i = 1; i <= env.count; i++) {
        const instance = new ec2.Instance(this, `${env.name}-instance-${i}`, {
          vpc: props.vpc,
          vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
          machineImage: ec2.MachineImage.latestAmazonLinux2(),
          securityGroup: props.securityGroup,
          role: props.instanceRole,
          userData: userData,
          userDataCausesReplacement: false, // Make idempotent
        });

        // Apply tags
        cdk.Tags.of(instance).add('Name', `${env.name}-webserver-${i}`);
        cdk.Tags.of(instance).add('Environment', env.name);
        cdk.Tags.of(instance).add('Application', 'WebServer');
        cdk.Tags.of(instance).add('Criticality', env.criticality);
        cdk.Tags.of(instance).add('PatchGroup', `${env.name}-patch-group`);
        cdk.Tags.of(instance).add('Demo', 'Scenario1');

        this.instances.push(instance);

        // Output instance ID
        new cdk.CfnOutput(this, `${env.name}-instance-${i}-id`, {
          value: instance.instanceId,
          description: `Instance ID for ${env.name} webserver ${i}`,
          exportName: `Scenario1-${env.name}-instance-${i}-id`
        });
      }
    });

    // Create maintenance windows for staging and prod
    const stagingWindow = new ssm.CfnMaintenanceWindow(this, 'StagingMaintenanceWindow', {
      name: 'scenario1-staging-maintenance',
      description: 'Maintenance window for staging environment',
      schedule: 'cron(0 2 ? * THU *)', // Thursday 2 AM
      duration: 2,
      cutoff: 0,
      allowUnassociatedTargets: false,
      tags: [{
        key: 'Demo',
        value: 'Scenario1'
      }]
    });

    const prodWindow = new ssm.CfnMaintenanceWindow(this, 'ProdMaintenanceWindow', {
      name: 'scenario1-prod-maintenance',
      description: 'Maintenance window for production environment',
      schedule: 'cron(0 3 ? * SAT *)', // Saturday 3 AM
      duration: 4,
      cutoff: 1,
      allowUnassociatedTargets: false,
      tags: [{
        key: 'Demo',
        value: 'Scenario1'
      }]
    });

    // Register targets for maintenance windows
    new ssm.CfnMaintenanceWindowTarget(this, 'StagingWindowTarget', {
      windowId: stagingWindow.ref,
      resourceType: 'INSTANCE',
      targets: [{
        key: 'tag:Environment',
        values: ['staging']
      }]
    });

    new ssm.CfnMaintenanceWindowTarget(this, 'ProdWindowTarget', {
      windowId: prodWindow.ref,
      resourceType: 'INSTANCE',
      targets: [{
        key: 'tag:Environment',
        values: ['prod']
      }]
    });

    // Create patch baselines
    new ssm.CfnPatchBaseline(this, 'DevPatchBaseline', {
      name: 'scenario1-dev-baseline',
      description: 'Patch baseline for dev environment - immediate patching',
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
          enableNonSecurity: true,
          complianceLevel: 'CRITICAL'
        }]
      },
      tags: [{
        key: 'Demo',
        value: 'Scenario1'
      }]
    });

    new ssm.CfnPatchBaseline(this, 'StagingPatchBaseline', {
      name: 'scenario1-staging-baseline',
      description: 'Patch baseline for staging environment',
      operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: {
        patchRules: [{
          patchFilterGroup: {
            patchFilters: [{
              key: 'SEVERITY',
              values: ['Critical', 'Important']
            }]
          },
          approveAfterDays: 0,
          enableNonSecurity: false,
          complianceLevel: 'HIGH'
        }]
      },
      tags: [{
        key: 'Demo',
        value: 'Scenario1'
      }]
    });

    new ssm.CfnPatchBaseline(this, 'ProdPatchBaseline', {
      name: 'scenario1-prod-baseline',
      description: 'Patch baseline for production environment',
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
          enableNonSecurity: false,
          complianceLevel: 'CRITICAL'
        }]
      },
      tags: [{
        key: 'Demo',
        value: 'Scenario1'
      }]
    });

    // Outputs
    new cdk.CfnOutput(this, 'TotalInstances', {
      value: this.instances.length.toString(),
      description: 'Total number of demo instances created'
    });

    new cdk.CfnOutput(this, 'StagingMaintenanceWindowId', {
      value: stagingWindow.ref,
      description: 'Staging maintenance window ID',
      exportName: 'Scenario1-StagingMaintenanceWindowId'
    });

    new cdk.CfnOutput(this, 'ProdMaintenanceWindowId', {
      value: prodWindow.ref,
      description: 'Production maintenance window ID',
      exportName: 'Scenario1-ProdMaintenanceWindowId'
    });
  }
}
