// ── Sample Environment ──────────────────────────────────────────────
// Self-contained sample stack: VPC, security group, instance role, 15 EC2
// instances across dev/staging/prod, maintenance windows, ALBs, patch
// baselines, and CloudWatch alarms.
//
// NOT required for the solution — most customers point the agent at
// their own existing fleet.
//
// Deploy:  ./deploy.sh --with-sample-env
// Destroy: ./deploy.sh destroy --sample-only

import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as targets from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

interface SampleEnvironmentStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
}

export class SampleEnvironmentStack extends cdk.Stack {
  public readonly instances: ec2.Instance[] = [];

  constructor(scope: Construct, id: string, props: SampleEnvironmentStackProps) {
    super(scope, id, props);

    // ── Instance Security Group (sample environment) ─────────────────
    const instanceSG = new ec2.SecurityGroup(this, 'SampleInstanceSG', {
      vpc: props.vpc,
      description: 'Security group for sample patch automation instances',
      allowAllOutbound: true,
    });
    instanceSG.addIngressRule(instanceSG, ec2.Port.tcp(80), 'HTTP from ALB');
    instanceSG.addIngressRule(instanceSG, ec2.Port.tcp(443), 'HTTPS from ALB');

    // ── Instance IAM Role (sample environment) ───────────────────────
    const instanceRole = new iam.Role(this, 'SampleInstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      description: 'IAM role for sample patch automation instances',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchAgentServerPolicy'),
      ],
    });

    instanceRole.addToPolicy(new iam.PolicyStatement({
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
        'ec2messages:SendReply',
      ],
      resources: ['*'],
    }));

    new iam.CfnInstanceProfile(this, 'SampleInstanceProfile', {
      roles: [instanceRole.roleName],
    });

    // ── User data ──────────────────────────────────────────────────
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      '#!/bin/bash',
      'set -e',
      'yum update -y',
      'yum install -y amazon-ssm-agent',
      'systemctl enable amazon-ssm-agent',
      'systemctl start amazon-ssm-agent',
      'yum install -y httpd',
      'systemctl enable httpd',
      'systemctl start httpd',
      'INSTANCE_ID=$(ec2-metadata --instance-id | cut -d " " -f 2)',
      'ENVIRONMENT=$(aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=Environment" --region us-east-1 --query "Tags[0].Value" --output text 2>/dev/null || echo "unknown")',
      'cat > /var/www/html/index.html << EOF',
      '<html><head><title>Patch Automation Server</title></head>',
      '<body><h1>Intelligent Patch Automation</h1>',
      '<p>Instance: $INSTANCE_ID | Environment: $ENVIRONMENT | Apache: $(httpd -v | head -1)</p>',
      '</body></html>',
      'EOF',
      'echo "Instance setup complete" > /tmp/setup-complete.txt',
    );

    // ── Environment configuration ──────────────────────────────────
    const environments = [
      {
        name: 'dev', count: 5, criticality: 'Low',
        teams: ['platform', 'api', 'frontend', 'data', 'platform'],
        products: ['api-gateway', 'user-service', 'web-app', 'analytics', 'monitoring'],
        complianceFrameworks: ['SOC2', 'SOC2', 'SOC2', 'SOC2', 'SOC2'],
        slaOverrides: [
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
        ],
      },
      {
        name: 'staging', count: 5, criticality: 'Medium',
        teams: ['platform', 'api', 'frontend', 'data', 'security'],
        products: ['api-gateway', 'user-service', 'web-app', 'analytics', 'auth-service'],
        complianceFrameworks: ['SOC2', 'PCI-DSS', 'SOC2', 'SOC2', 'SOC2'],
        slaOverrides: [
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '12', 'SLA-HIGH': '48', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // PCI-DSS — stricter
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
        ],
      },
      {
        name: 'prod', count: 5, criticality: 'High',
        teams: ['platform', 'api', 'frontend', 'data', 'security'],
        products: ['api-gateway', 'user-service', 'web-app', 'analytics', 'auth-service'],
        complianceFrameworks: ['SOC2,HIPAA', 'PCI-DSS', 'SOC2', 'HIPAA', 'PCI-DSS,SOC2'],
        slaOverrides: [
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // SOC2,HIPAA
          { 'SLA-CRITICAL': '6',  'SLA-HIGH': '24', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // PCI-DSS payment — strictest
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // SOC2
          { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // HIPAA
          { 'SLA-CRITICAL': '6',  'SLA-HIGH': '24', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },  // PCI-DSS,SOC2 — strictest
        ],
      },
    ];

    // ── Create instances ───────────────────────────────────────────
    environments.forEach(env => {
      for (let i = 1; i <= env.count; i++) {
        const instance = new ec2.Instance(this, `${env.name}-instance-${i}`, {
          vpc: props.vpc,
          vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
          machineImage: ec2.MachineImage.genericLinux({
            'us-east-1': 'ami-007f9744891c45503',
          }),
          securityGroup: instanceSG,
          role: instanceRole,
          userData,
          userDataCausesReplacement: false,
        });

        cdk.Tags.of(instance).add('Name', `${env.name}-webserver-${i}`);
        cdk.Tags.of(instance).add('Environment', env.name);
        cdk.Tags.of(instance).add('Application', 'WebServer');
        cdk.Tags.of(instance).add('Criticality', env.criticality);
        cdk.Tags.of(instance).add('PatchGroup', `${env.name}-patch-group`);
        cdk.Tags.of(instance).add('ManagedBy', 'IntelligentPatchAutomation');
        cdk.Tags.of(instance).add('Team', env.teams[i - 1]);
        cdk.Tags.of(instance).add('Product', env.products[i - 1]);
        cdk.Tags.of(instance).add('CostCenter', `${env.teams[i - 1]}-engineering`);
        cdk.Tags.of(instance).add('Owner', `${env.teams[i - 1]}-lead@example.com`);
        cdk.Tags.of(instance).add('ComplianceFrameworks', env.complianceFrameworks[i - 1]);
        // Per-instance SLA overrides (optional — demonstrates tag-based SLA)
        const slaOverride = env.slaOverrides?.[i - 1] ?? {};
        for (const [key, value] of Object.entries(slaOverride)) {
          cdk.Tags.of(instance).add(key, value as string);
        }

        this.instances.push(instance);

        new cdk.CfnOutput(this, `${env.name}-instance-${i}-id`, {
          value: instance.instanceId,
          description: `Instance ID for ${env.name} webserver ${i}`,
          exportName: `SampleEnv-${env.name}-instance-${i}-id`,
        });
      }
    });

    // ── Maintenance windows ────────────────────────────────────────
    const devWindow = new ssm.CfnMaintenanceWindow(this, 'DevMaintenanceWindow', {
      name: 'dev-daily-patching',
      description: 'Daily maintenance window for development environment',
      schedule: 'cron(0 1 ? * * *)',
      duration: 2, cutoff: 0, allowUnassociatedTargets: false,
    });
    const stagingWindow = new ssm.CfnMaintenanceWindow(this, 'StagingMaintenanceWindow', {
      name: 'staging-weekly-patching',
      description: 'Weekly maintenance window for staging environment',
      schedule: 'cron(0 2 ? * TUE *)',
      duration: 2, cutoff: 0, allowUnassociatedTargets: false,
    });
    const prodWindow = new ssm.CfnMaintenanceWindow(this, 'ProdMaintenanceWindow', {
      name: 'prod-monthly-patching',
      description: 'Monthly maintenance window for production environment',
      schedule: 'cron(0 2 1 * ? *)',
      duration: 4, cutoff: 1, allowUnassociatedTargets: false,
    });

    // Register targets
    new ssm.CfnMaintenanceWindowTarget(this, 'DevWindowTarget', {
      windowId: devWindow.ref, resourceType: 'INSTANCE',
      targets: [{ key: 'tag:Environment', values: ['dev'] }],
      name: 'dev-instances',
    });
    new ssm.CfnMaintenanceWindowTarget(this, 'StagingWindowTarget', {
      windowId: stagingWindow.ref, resourceType: 'INSTANCE',
      targets: [{ key: 'tag:Environment', values: ['staging'] }],
      name: 'staging-instances',
    });
    new ssm.CfnMaintenanceWindowTarget(this, 'ProdWindowTarget', {
      windowId: prodWindow.ref, resourceType: 'INSTANCE',
      targets: [{ key: 'tag:Environment', values: ['prod'] }],
      name: 'prod-instances',
    });

    // ── ALBs + target groups (for dependency analysis) ─────────────
    ['dev', 'staging', 'prod'].forEach(envName => {
      const alb = new elbv2.ApplicationLoadBalancer(this, `${envName}ALB`, {
        vpc: props.vpc, internetFacing: false,
        loadBalancerName: `${envName}-patch-automation-alb`,
        securityGroup: instanceSG,
      });
      const tg = new elbv2.ApplicationTargetGroup(this, `${envName}TG`, {
        vpc: props.vpc, port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        targetType: elbv2.TargetType.INSTANCE,
        targetGroupName: `${envName}-patch-automation-tg`,
        healthCheck: { path: '/', interval: cdk.Duration.seconds(30), timeout: cdk.Duration.seconds(5), healthyThresholdCount: 2, unhealthyThresholdCount: 3 },
      });
      alb.addListener(`${envName}Listener`, { port: 80, defaultTargetGroups: [tg] });
      this.instances.filter(i => i.node.id.toLowerCase().includes(envName))
        .forEach(inst => tg.addTarget(new targets.InstanceTarget(inst, 80)));
      new cdk.CfnOutput(this, `${envName}ALBDNS`, {
        value: alb.loadBalancerDnsName,
        description: `${envName} ALB DNS name`,
        exportName: `SampleEnv-${envName}-alb-dns`,
      });
    });

    // ── CloudWatch alarms (staging sample) ─────────────────────────
    this.instances.filter(i => i.node.id.toLowerCase().includes('staging')).slice(0, 3)
      .forEach((instance, idx) => {
        new cloudwatch.Alarm(this, `staging-httpd-alarm-${idx + 1}`, {
          alarmName: `staging-instance-${idx + 1}-httpd-health`,
          alarmDescription: `Apache httpd health for staging instance ${idx + 1}`,
          metric: new cloudwatch.Metric({
            namespace: 'CWAgent', metricName: 'procstat_running',
            dimensionsMap: { InstanceId: instance.instanceId, process_name: 'httpd' },
            statistic: 'Average', period: cdk.Duration.minutes(1),
          }),
          threshold: 1, evaluationPeriods: 2,
          comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
          treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        });
      });

    // ── Patch baselines ────────────────────────────────────────────
    new ssm.CfnPatchBaseline(this, 'DevPatchBaseline', {
      name: 'dev-baseline', description: 'Dev - immediate patching', operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: { patchRules: [{ patchFilterGroup: { patchFilters: [{ key: 'SEVERITY', values: ['Critical', 'Important', 'Medium', 'Low'] }] }, approveAfterDays: 0, enableNonSecurity: true, complianceLevel: 'CRITICAL' }] },
      tags: [{ key: 'ManagedBy', value: 'IntelligentPatchAutomation' }],
    });
    new ssm.CfnPatchBaseline(this, 'StagingPatchBaseline', {
      name: 'staging-baseline', description: 'Staging - critical/important only', operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: { patchRules: [{ patchFilterGroup: { patchFilters: [{ key: 'SEVERITY', values: ['Critical', 'Important'] }] }, approveAfterDays: 0, enableNonSecurity: false, complianceLevel: 'HIGH' }] },
      tags: [{ key: 'ManagedBy', value: 'IntelligentPatchAutomation' }],
    });
    new ssm.CfnPatchBaseline(this, 'ProdPatchBaseline', {
      name: 'prod-baseline', description: 'Prod - 7-day approval delay', operatingSystem: 'AMAZON_LINUX_2',
      approvalRules: { patchRules: [{ patchFilterGroup: { patchFilters: [{ key: 'SEVERITY', values: ['Critical', 'Important'] }] }, approveAfterDays: 7, enableNonSecurity: false, complianceLevel: 'CRITICAL' }] },
      tags: [{ key: 'ManagedBy', value: 'IntelligentPatchAutomation' }],
    });

    // ── Outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'TotalInstances', { value: this.instances.length.toString() });
    new cdk.CfnOutput(this, 'DevMaintenanceWindowId', { value: devWindow.ref, exportName: 'SampleEnv-DevMaintenanceWindowId' });
    new cdk.CfnOutput(this, 'StagingMaintenanceWindowId', { value: stagingWindow.ref, exportName: 'SampleEnv-StagingMaintenanceWindowId' });
    new cdk.CfnOutput(this, 'ProdMaintenanceWindowId', { value: prodWindow.ref, exportName: 'SampleEnv-ProdMaintenanceWindowId' });
  }
}
