"use strict";
// ── Sample Environment ──────────────────────────────────────────────
// Self-contained sample stack: VPC, security group, instance role, 5 EC2
// instances across dev/staging/prod, maintenance windows, ALBs, patch
// baselines, and CloudWatch alarms.
//
// NOT required for the solution — most customers point the agent at
// their own existing fleet.
//
// Deploy:  ./deploy.sh --with-sample-env
// Destroy: ./deploy.sh destroy --sample-only
Object.defineProperty(exports, "__esModule", { value: true });
exports.SampleEnvironmentStack = void 0;
const cdk = require("aws-cdk-lib");
const ec2 = require("aws-cdk-lib/aws-ec2");
const iam = require("aws-cdk-lib/aws-iam");
const s3 = require("aws-cdk-lib/aws-s3");
const ssm = require("aws-cdk-lib/aws-ssm");
const elbv2 = require("aws-cdk-lib/aws-elasticloadbalancingv2");
const targets = require("aws-cdk-lib/aws-elasticloadbalancingv2-targets");
const cloudwatch = require("aws-cdk-lib/aws-cloudwatch");
class SampleEnvironmentStack extends cdk.Stack {
    constructor(scope, id, props) {
        super(scope, id, props);
        this.instances = [];
        // Use provided VPC or create a self-contained one (spoke account deployment)
        const vpc = props.vpc ?? new ec2.Vpc(this, 'SampleVpc', {
            maxAzs: 2,
            natGateways: 1,
            subnetConfiguration: [
                { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
                { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
            ],
        });
        // ── Instance Security Group (sample environment) ─────────────────
        const instanceSG = new ec2.SecurityGroup(this, 'SampleInstanceSG', {
            vpc,
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
        // S3 read access for BaselineOverride files (severity-scoped patching).
        // The compliance reports bucket lives in the HUB account (Patchy-Core stack).
        // When this stack deploys to a spoke account, this.account is the spoke — but
        // the bucket is in the hub. Use hubAccountId context to reference the correct bucket.
        // The hub bucket's resource policy allows org-wide GetObject on baseline-overrides/*.
        const hubAccountId = this.node.tryGetContext('hubAccountId') || process.env.HUB_ACCOUNT_ID || this.account;
        const complianceBucket = s3.Bucket.fromBucketName(this, 'ComplianceBucketRef', `patch-compliance-reports-${hubAccountId}`);
        complianceBucket.grantRead(instanceRole, 'baseline-overrides/*');
        new iam.CfnInstanceProfile(this, 'SampleInstanceProfile', {
            roles: [instanceRole.roleName],
        });
        // ── User data ──────────────────────────────────────────────────
        const userData = ec2.UserData.forLinux();
        userData.addCommands('#!/bin/bash', 'set -e', 'yum install -y amazon-ssm-agent', 'systemctl enable amazon-ssm-agent', 'systemctl start amazon-ssm-agent', 'yum install -y httpd', 'systemctl enable httpd', 'systemctl start httpd', 'INSTANCE_ID=$(ec2-metadata --instance-id | cut -d " " -f 2)', 'TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")', 'DEPLOY_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)', 'ENVIRONMENT=$(aws ec2 describe-tags --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=Environment" --region $DEPLOY_REGION --query "Tags[0].Value" --output text 2>/dev/null || echo "unknown")', 'cat > /var/www/html/index.html << EOF', '<html><head><title>Patch Automation Server</title></head>', '<body><h1>Intelligent Patch Automation</h1>', '<p>Instance: $INSTANCE_ID | Environment: $ENVIRONMENT | Apache: $(httpd -v | head -1)</p>', '</body></html>', 'EOF', 'echo "Instance setup complete" > /tmp/setup-complete.txt');
        // ── Environment configuration ──────────────────────────────────
        const environments = [
            {
                name: 'dev', count: 2, criticality: 'Low',
                teams: ['platform', 'api'],
                products: ['api-gateway', 'user-service'],
                complianceFrameworks: ['SOC2', 'SOC2'],
                slaOverrides: [
                    { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
                    { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
                ],
            },
            {
                name: 'staging', count: 1, criticality: 'Medium',
                teams: ['platform'],
                products: ['api-gateway'],
                complianceFrameworks: ['PCI-DSS'],
                slaOverrides: [
                    { 'SLA-CRITICAL': '12', 'SLA-HIGH': '48', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
                ],
            },
            {
                name: 'prod', count: 2, criticality: 'High',
                teams: ['platform', 'security'],
                products: ['api-gateway', 'auth-service'],
                complianceFrameworks: ['SOC2,HIPAA', 'PCI-DSS,SOC2'],
                slaOverrides: [
                    { 'SLA-CRITICAL': '24', 'SLA-HIGH': '72', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
                    { 'SLA-CRITICAL': '6', 'SLA-HIGH': '24', 'SLA-MEDIUM': '168', 'SLA-LOW': '720' },
                ],
            },
        ];
        // ── Create instances ───────────────────────────────────────────
        environments.forEach(env => {
            for (let i = 1; i <= env.count; i++) {
                const instance = new ec2.Instance(this, `${env.name}-instance-${i}`, {
                    vpc: vpc,
                    vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
                    instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
                    // Intentionally old AMI — ensures vulnerabilities exist for demo/testing
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
                cdk.Tags.of(instance).add(process.env.SSM_SCOPE_TAG_KEY || 'PatchAutomation', process.env.SSM_SCOPE_TAG_VALUE || 'enabled');
                cdk.Tags.of(instance).add('Team', env.teams[i - 1]);
                cdk.Tags.of(instance).add('Product', env.products[i - 1]);
                cdk.Tags.of(instance).add('CostCenter', `${env.teams[i - 1]}-engineering`);
                cdk.Tags.of(instance).add('Owner', `${env.teams[i - 1]}-lead@example.com`);
                cdk.Tags.of(instance).add('ComplianceFrameworks', env.complianceFrameworks[i - 1]);
                // Per-instance SLA overrides (optional — demonstrates tag-based SLA)
                const slaOverride = env.slaOverrides?.[i - 1] ?? {};
                for (const [key, value] of Object.entries(slaOverride)) {
                    cdk.Tags.of(instance).add(key, value);
                }
                this.instances.push(instance);
                new cdk.CfnOutput(this, `${env.name}-instance-${i}-id`, {
                    value: instance.instanceId,
                    description: `Instance ID for ${env.name} webserver ${i}`,
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
        const scopeTagKey = process.env.SSM_SCOPE_TAG_KEY || 'PatchAutomation';
        const scopeTagValue = process.env.SSM_SCOPE_TAG_VALUE || 'enabled';
        new ssm.CfnMaintenanceWindowTarget(this, 'DevWindowTarget', {
            windowId: devWindow.ref, resourceType: 'INSTANCE',
            targets: [
                { key: 'tag:Environment', values: ['dev'] },
                { key: `tag:${scopeTagKey}`, values: [scopeTagValue] },
            ],
            name: 'dev-instances',
        });
        new ssm.CfnMaintenanceWindowTarget(this, 'StagingWindowTarget', {
            windowId: stagingWindow.ref, resourceType: 'INSTANCE',
            targets: [
                { key: 'tag:Environment', values: ['staging'] },
                { key: `tag:${scopeTagKey}`, values: [scopeTagValue] },
            ],
            name: 'staging-instances',
        });
        new ssm.CfnMaintenanceWindowTarget(this, 'ProdWindowTarget', {
            windowId: prodWindow.ref, resourceType: 'INSTANCE',
            targets: [
                { key: 'tag:Environment', values: ['prod'] },
                { key: `tag:${scopeTagKey}`, values: [scopeTagValue] },
            ],
            name: 'prod-instances',
        });
        // ── SSM Associations (inventory + patch scan) ──────────────────
        // Inventory collection — ensures instances appear in SSM Explorer
        // regardless of whether Quick Setup is configured in this account.
        new ssm.CfnAssociation(this, 'InventoryAssociation', {
            name: 'AWS-GatherSoftwareInventory',
            targets: [{ key: `tag:${scopeTagKey}`, values: [scopeTagValue] }],
            scheduleExpression: 'rate(30 minutes)',
            associationName: 'Patchy-SampleEnv-Inventory',
        });
        // Patch scan — populates patch compliance data (MissingCount, InstalledCount)
        // so the dashboard and agent have data immediately after deploy.
        // Scan only — does NOT install patches.
        new ssm.CfnAssociation(this, 'PatchScanAssociation', {
            name: 'AWS-RunPatchBaseline',
            targets: [{ key: `tag:${scopeTagKey}`, values: [scopeTagValue] }],
            scheduleExpression: 'rate(12 hours)',
            associationName: 'Patchy-SampleEnv-PatchScan',
            parameters: {
                Operation: ['Scan'],
            },
        });
        // ── ALBs + target groups (for dependency analysis) ─────────────
        ['dev', 'staging', 'prod'].forEach(envName => {
            const alb = new elbv2.ApplicationLoadBalancer(this, `${envName}ALB`, {
                vpc: vpc, internetFacing: false,
                loadBalancerName: `${envName}-patch-automation-alb`,
                securityGroup: instanceSG,
            });
            const tg = new elbv2.ApplicationTargetGroup(this, `${envName}TG`, {
                vpc: vpc, port: 80,
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
        new cdk.CfnOutput(this, 'DevMaintenanceWindowId', { value: devWindow.ref });
        new cdk.CfnOutput(this, 'StagingMaintenanceWindowId', { value: stagingWindow.ref });
        new cdk.CfnOutput(this, 'ProdMaintenanceWindowId', { value: prodWindow.ref });
    }
}
exports.SampleEnvironmentStack = SampleEnvironmentStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoic2FtcGxlLWVudmlyb25tZW50LXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsic2FtcGxlLWVudmlyb25tZW50LXN0YWNrLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7QUFBQSx1RUFBdUU7QUFDdkUseUVBQXlFO0FBQ3pFLHNFQUFzRTtBQUN0RSxvQ0FBb0M7QUFDcEMsRUFBRTtBQUNGLG9FQUFvRTtBQUNwRSw0QkFBNEI7QUFDNUIsRUFBRTtBQUNGLHlDQUF5QztBQUN6Qyw2Q0FBNkM7OztBQUU3QyxtQ0FBbUM7QUFDbkMsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQyx5Q0FBeUM7QUFDekMsMkNBQTJDO0FBQzNDLGdFQUFnRTtBQUNoRSwwRUFBMEU7QUFDMUUseURBQXlEO0FBU3pELE1BQWEsc0JBQXVCLFNBQVEsR0FBRyxDQUFDLEtBQUs7SUFHbkQsWUFBWSxLQUFnQixFQUFFLEVBQVUsRUFBRSxLQUFrQztRQUMxRSxLQUFLLENBQUMsS0FBSyxFQUFFLEVBQUUsRUFBRSxLQUFLLENBQUMsQ0FBQztRQUhWLGNBQVMsR0FBbUIsRUFBRSxDQUFDO1FBSzdDLDZFQUE2RTtRQUM3RSxNQUFNLEdBQUcsR0FBRyxLQUFLLENBQUMsR0FBRyxJQUFJLElBQUksR0FBRyxDQUFDLEdBQUcsQ0FBQyxJQUFJLEVBQUUsV0FBVyxFQUFFO1lBQ3RELE1BQU0sRUFBRSxDQUFDO1lBQ1QsV0FBVyxFQUFFLENBQUM7WUFDZCxtQkFBbUIsRUFBRTtnQkFDbkIsRUFBRSxJQUFJLEVBQUUsUUFBUSxFQUFFLFVBQVUsRUFBRSxHQUFHLENBQUMsVUFBVSxDQUFDLE1BQU0sRUFBRSxRQUFRLEVBQUUsRUFBRSxFQUFFO2dCQUNuRSxFQUFFLElBQUksRUFBRSxTQUFTLEVBQUUsVUFBVSxFQUFFLEdBQUcsQ0FBQyxVQUFVLENBQUMsbUJBQW1CLEVBQUUsUUFBUSxFQUFFLEVBQUUsRUFBRTthQUNsRjtTQUNGLENBQUMsQ0FBQztRQUVILG9FQUFvRTtRQUNwRSxNQUFN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RixDQUFDO0NBQ0Y7QUFoVUQsd0RBZ1VDIiwic291cmNlc0NvbnRlbnQiOlsiLy8g4pSA4pSAIFNhbXBsZSBFbnZpcm9ubWVudCDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbi8vIFNlbGYtY29udGFpbmVkIHNhbXBsZSBzdGFjazogVlBDLCBzZWN1cml0eSBncm91cCwgaW5zdGFuY2Ugcm9sZSwgNSBFQzJcbi8vIGluc3RhbmNlcyBhY3Jvc3MgZGV2L3N0YWdpbmcvcHJvZCwgbWFpbnRlbmFuY2Ugd2luZG93cywgQUxCcywgcGF0Y2hcbi8vIGJhc2VsaW5lcywgYW5kIENsb3VkV2F0Y2ggYWxhcm1zLlxuLy9cbi8vIE5PVCByZXF1aXJlZCBmb3IgdGhlIHNvbHV0aW9uIOKAlCBtb3N0IGN1c3RvbWVycyBwb2ludCB0aGUgYWdlbnQgYXRcbi8vIHRoZWlyIG93biBleGlzdGluZyBmbGVldC5cbi8vXG4vLyBEZXBsb3k6ICAuL2RlcGxveS5zaCAtLXdpdGgtc2FtcGxlLWVudlxuLy8gRGVzdHJveTogLi9kZXBsb3kuc2ggZGVzdHJveSAtLXNhbXBsZS1vbmx5XG5cbmltcG9ydCAqIGFzIGNkayBmcm9tICdhd3MtY2RrLWxpYic7XG5pbXBvcnQgKiBhcyBlYzIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWVjMic7XG5pbXBvcnQgKiBhcyBpYW0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWlhbSc7XG5pbXBvcnQgKiBhcyBzMyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtczMnO1xuaW1wb3J0ICogYXMgc3NtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zc20nO1xuaW1wb3J0ICogYXMgZWxidjIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWVsYXN0aWNsb2FkYmFsYW5jaW5ndjInO1xuaW1wb3J0ICogYXMgdGFyZ2V0cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWxhc3RpY2xvYWRiYWxhbmNpbmd2Mi10YXJnZXRzJztcbmltcG9ydCAqIGFzIGNsb3Vkd2F0Y2ggZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gnO1xuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XG5cbmludGVyZmFjZSBTYW1wbGVFbnZpcm9ubWVudFN0YWNrUHJvcHMgZXh0ZW5kcyBjZGsuU3RhY2tQcm9wcyB7XG4gIC8qKiBWUEMgdG8gZGVwbG95IGluc3RhbmNlcyBpbnRvLiBJZiBub3QgcHJvdmlkZWQsIGEgbWluaW1hbCBWUEMgaXMgY3JlYXRlZFxuICAgKiAgKHVzZWZ1bCBmb3Igc3Bva2UtYWNjb3VudCBkZXBsb3ltZW50cyB3aGVyZSBQYXRjaHktTmV0d29yayBkb2Vzbid0IGV4aXN0KS4gKi9cbiAgdnBjPzogZWMyLklWcGM7XG59XG5cbmV4cG9ydCBjbGFzcyBTYW1wbGVFbnZpcm9ubWVudFN0YWNrIGV4dGVuZHMgY2RrLlN0YWNrIHtcbiAgcHVibGljIHJlYWRvbmx5IGluc3RhbmNlczogZWMyLkluc3RhbmNlW10gPSBbXTtcblxuICBjb25zdHJ1Y3RvcihzY29wZTogQ29uc3RydWN0LCBpZDogc3RyaW5nLCBwcm9wczogU2FtcGxlRW52aXJvbm1lbnRTdGFja1Byb3BzKSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICAvLyBVc2UgcHJvdmlkZWQgVlBDIG9yIGNyZWF0ZSBhIHNlbGYtY29udGFpbmVkIG9uZSAoc3Bva2UgYWNjb3VudCBkZXBsb3ltZW50KVxuICAgIGNvbnN0IHZwYyA9IHByb3BzLnZwYyA/PyBuZXcgZWMyLlZwYyh0aGlzLCAnU2FtcGxlVnBjJywge1xuICAgICAgbWF4QXpzOiAyLFxuICAgICAgbmF0R2F0ZXdheXM6IDEsXG4gICAgICBzdWJuZXRDb25maWd1cmF0aW9uOiBbXG4gICAgICAgIHsgbmFtZTogJ3B1YmxpYycsIHN1Ym5ldFR5cGU6IGVjMi5TdWJuZXRUeXBlLlBVQkxJQywgY2lkck1hc2s6IDI0IH0sXG4gICAgICAgIHsgbmFtZTogJ3ByaXZhdGUnLCBzdWJuZXRUeXBlOiBlYzIuU3VibmV0VHlwZS5QUklWQVRFX1dJVEhfRUdSRVNTLCBjaWRyTWFzazogMjQgfSxcbiAgICAgIF0sXG4gICAgfSk7XG5cbiAgICAvLyDilIDilIAgSW5zdGFuY2UgU2VjdXJpdHkgR3JvdXAgKHNhbXBsZSBlbnZpcm9ubWVudCkg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSAXG4gICAgY29uc3QgaW5zdGFuY2VTRyA9IG5ldyBlYzIuU2VjdXJpdHlHcm91cCh0aGlzLCAnU2FtcGxlSW5zdGFuY2VTRycsIHtcbiAgICAgIHZwYyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2VjdXJpdHkgZ3JvdXAgZm9yIHNhbXBsZSBwYXRjaCBhdXRvbWF0aW9uIGluc3RhbmNlcycsXG4gICAgICBhbGxvd0FsbE91dGJvdW5kOiB0cnVlLFxuICAgIH0pO1xuICAgIGluc3RhbmNlU0cuYWRkSW5ncmVzc1J1bGUoaW5zdGFuY2VTRywgZWMyLlBvcnQudGNwKDgwKSwgJ0hUVFAgZnJvbSBBTEInKTtcbiAgICBpbnN0YW5jZVNHLmFkZEluZ3Jlc3NSdWxlKGluc3RhbmNlU0csIGVjMi5Qb3J0LnRjcCg0NDMpLCAnSFRUUFMgZnJvbSBBTEInKTtcblxuICAgIC8vIOKUgOKUgCBJbnN0YW5jZSBJQU0gUm9sZSAoc2FtcGxlIGVudmlyb25tZW50KSDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICBjb25zdCBpbnN0YW5jZVJvbGUgPSBuZXcgaWFtLlJvbGUodGhpcywgJ1NhbXBsZUluc3RhbmNlUm9sZScsIHtcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlYzIuYW1hem9uYXdzLmNvbScpLFxuICAgICAgZGVzY3JpcHRpb246ICdJQU0gcm9sZSBmb3Igc2FtcGxlIHBhdGNoIGF1dG9tYXRpb24gaW5zdGFuY2VzJyxcbiAgICAgIG1hbmFnZWRQb2xpY2llczogW1xuICAgICAgICBpYW0uTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ0FtYXpvblNTTU1hbmFnZWRJbnN0YW5jZUNvcmUnKSxcbiAgICAgICAgaWFtLk1hbmFnZWRQb2xpY3kuZnJvbUF3c01hbmFnZWRQb2xpY3lOYW1lKCdDbG91ZFdhdGNoQWdlbnRTZXJ2ZXJQb2xpY3knKSxcbiAgICAgIF0sXG4gICAgfSk7XG5cbiAgICBpbnN0YW5jZVJvbGUuYWRkVG9Qb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgZWZmZWN0OiBpYW0uRWZmZWN0LkFMTE9XLFxuICAgICAgYWN0aW9uczogW1xuICAgICAgICAnc3NtOlVwZGF0ZUluc3RhbmNlSW5mb3JtYXRpb24nLFxuICAgICAgICAnc3NtOlNlbmRDb21tYW5kJyxcbiAgICAgICAgJ3NzbTpMaXN0Q29tbWFuZHMnLFxuICAgICAgICAnc3NtOkxpc3RDb21tYW5kSW52b2NhdGlvbnMnLFxuICAgICAgICAnc3NtOkRlc2NyaWJlSW5zdGFuY2VJbmZvcm1hdGlvbicsXG4gICAgICAgICdzc206R2V0RGVwbG95YWJsZVBhdGNoU25hcHNob3RGb3JJbnN0YW5jZScsXG4gICAgICAgICdzc206R2V0RGVmYXVsdFBhdGNoQmFzZWxpbmUnLFxuICAgICAgICAnc3NtOkdldE1hbmlmZXN0JyxcbiAgICAgICAgJ3NzbTpHZXRQYXJhbWV0ZXInLFxuICAgICAgICAnc3NtOkdldFBhcmFtZXRlcnMnLFxuICAgICAgICAnc3NtOkxpc3RBc3NvY2lhdGlvbnMnLFxuICAgICAgICAnc3NtOkxpc3RJbnN0YW5jZUFzc29jaWF0aW9ucycsXG4gICAgICAgICdzc206UHV0SW52ZW50b3J5JyxcbiAgICAgICAgJ3NzbTpQdXRDb21wbGlhbmNlSXRlbXMnLFxuICAgICAgICAnc3NtOlB1dENvbmZpZ3VyZVBhY2thZ2VSZXN1bHQnLFxuICAgICAgICAnc3NtOlVwZGF0ZUFzc29jaWF0aW9uU3RhdHVzJyxcbiAgICAgICAgJ3NzbTpVcGRhdGVJbnN0YW5jZUFzc29jaWF0aW9uU3RhdHVzJyxcbiAgICAgICAgJ2VjMm1lc3NhZ2VzOkFja25vd2xlZGdlTWVzc2FnZScsXG4gICAgICAgICdlYzJtZXNzYWdlczpEZWxldGVNZXNzYWdlJyxcbiAgICAgICAgJ2VjMm1lc3NhZ2VzOkZhaWxNZXNzYWdlJyxcbiAgICAgICAgJ2VjMm1lc3NhZ2VzOkdldEVuZHBvaW50JyxcbiAgICAgICAgJ2VjMm1lc3NhZ2VzOkdldE1lc3NhZ2VzJyxcbiAgICAgICAgJ2VjMm1lc3NhZ2VzOlNlbmRSZXBseScsXG4gICAgICBdLFxuICAgICAgcmVzb3VyY2VzOiBbJyonXSxcbiAgICB9KSk7XG5cbiAgICAvLyBTMyByZWFkIGFjY2VzcyBmb3IgQmFzZWxpbmVPdmVycmlkZSBmaWxlcyAoc2V2ZXJpdHktc2NvcGVkIHBhdGNoaW5nKS5cbiAgICAvLyBUaGUgY29tcGxpYW5jZSByZXBvcnRzIGJ1Y2tldCBsaXZlcyBpbiB0aGUgSFVCIGFjY291bnQgKFBhdGNoeS1Db3JlIHN0YWNrKS5cbiAgICAvLyBXaGVuIHRoaXMgc3RhY2sgZGVwbG95cyB0byBhIHNwb2tlIGFjY291bnQsIHRoaXMuYWNjb3VudCBpcyB0aGUgc3Bva2Ug4oCUIGJ1dFxuICAgIC8vIHRoZSBidWNrZXQgaXMgaW4gdGhlIGh1Yi4gVXNlIGh1YkFjY291bnRJZCBjb250ZXh0IHRvIHJlZmVyZW5jZSB0aGUgY29ycmVjdCBidWNrZXQuXG4gICAgLy8gVGhlIGh1YiBidWNrZXQncyByZXNvdXJjZSBwb2xpY3kgYWxsb3dzIG9yZy13aWRlIEdldE9iamVjdCBvbiBiYXNlbGluZS1vdmVycmlkZXMvKi5cbiAgICBjb25zdCBodWJBY2NvdW50SWQgPSB0aGlzLm5vZGUudHJ5R2V0Q29udGV4dCgnaHViQWNjb3VudElkJykgfHwgcHJvY2Vzcy5lbnYuSFVCX0FDQ09VTlRfSUQgfHwgdGhpcy5hY2NvdW50O1xuICAgIGNvbnN0IGNvbXBsaWFuY2VCdWNrZXQgPSBzMy5CdWNrZXQuZnJvbUJ1Y2tldE5hbWUoXG4gICAgICB0aGlzLCAnQ29tcGxpYW5jZUJ1Y2tldFJlZicsXG4gICAgICBgcGF0Y2gtY29tcGxpYW5jZS1yZXBvcnRzLSR7aHViQWNjb3VudElkfWBcbiAgICApO1xuICAgIGNvbXBsaWFuY2VCdWNrZXQuZ3JhbnRSZWFkKGluc3RhbmNlUm9sZSwgJ2Jhc2VsaW5lLW92ZXJyaWRlcy8qJyk7XG5cbiAgICBuZXcgaWFtLkNmbkluc3RhbmNlUHJvZmlsZSh0aGlzLCAnU2FtcGxlSW5zdGFuY2VQcm9maWxlJywge1xuICAgICAgcm9sZXM6IFtpbnN0YW5jZVJvbGUucm9sZU5hbWVdLFxuICAgIH0pO1xuXG4gICAgLy8g4pSA4pSAIFVzZXIgZGF0YSDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICBjb25zdCB1c2VyRGF0YSA9IGVjMi5Vc2VyRGF0YS5mb3JMaW51eCgpO1xuICAgIHVzZXJEYXRhLmFkZENvbW1hbmRzKFxuICAgICAgJyMhL2Jpbi9iYXNoJyxcbiAgICAgICdzZXQgLWUnLFxuICAgICAgJ3l1bSBpbnN0YWxsIC15IGFtYXpvbi1zc20tYWdlbnQnLFxuICAgICAgJ3N5c3RlbWN0bCBlbmFibGUgYW1hem9uLXNzbS1hZ2VudCcsXG4gICAgICAnc3lzdGVtY3RsIHN0YXJ0IGFtYXpvbi1zc20tYWdlbnQnLFxuICAgICAgJ3l1bSBpbnN0YWxsIC15IGh0dHBkJyxcbiAgICAgICdzeXN0ZW1jdGwgZW5hYmxlIGh0dHBkJyxcbiAgICAgICdzeXN0ZW1jdGwgc3RhcnQgaHR0cGQnLFxuICAgICAgJ0lOU1RBTkNFX0lEPSQoZWMyLW1ldGFkYXRhIC0taW5zdGFuY2UtaWQgfCBjdXQgLWQgXCIgXCIgLWYgMiknLFxuICAgICAgJ1RPS0VOPSQoY3VybCAtcyAtWCBQVVQgaHR0cDovLzE2OS4yNTQuMTY5LjI1NC9sYXRlc3QvYXBpL3Rva2VuIC1IIFwiWC1hd3MtZWMyLW1ldGFkYXRhLXRva2VuLXR0bC1zZWNvbmRzOiAyMTYwMFwiKScsXG4gICAgICAnREVQTE9ZX1JFR0lPTj0kKGN1cmwgLXMgLUggXCJYLWF3cy1lYzItbWV0YWRhdGEtdG9rZW46ICRUT0tFTlwiIGh0dHA6Ly8xNjkuMjU0LjE2OS4yNTQvbGF0ZXN0L21ldGEtZGF0YS9wbGFjZW1lbnQvcmVnaW9uKScsXG4gICAgICAnRU5WSVJPTk1FTlQ9JChhd3MgZWMyIGRlc2NyaWJlLXRhZ3MgLS1maWx0ZXJzIFwiTmFtZT1yZXNvdXJjZS1pZCxWYWx1ZXM9JElOU1RBTkNFX0lEXCIgXCJOYW1lPWtleSxWYWx1ZXM9RW52aXJvbm1lbnRcIiAtLXJlZ2lvbiAkREVQTE9ZX1JFR0lPTiAtLXF1ZXJ5IFwiVGFnc1swXS5WYWx1ZVwiIC0tb3V0cHV0IHRleHQgMj4vZGV2L251bGwgfHwgZWNobyBcInVua25vd25cIiknLFxuICAgICAgJ2NhdCA+IC92YXIvd3d3L2h0bWwvaW5kZXguaHRtbCA8PCBFT0YnLFxuICAgICAgJzxodG1sPjxoZWFkPjx0aXRsZT5QYXRjaCBBdXRvbWF0aW9uIFNlcnZlcjwvdGl0bGU+PC9oZWFkPicsXG4gICAgICAnPGJvZHk+PGgxPkludGVsbGlnZW50IFBhdGNoIEF1dG9tYXRpb248L2gxPicsXG4gICAgICAnPHA+SW5zdGFuY2U6ICRJTlNUQU5DRV9JRCB8IEVudmlyb25tZW50OiAkRU5WSVJPTk1FTlQgfCBBcGFjaGU6ICQoaHR0cGQgLXYgfCBoZWFkIC0xKTwvcD4nLFxuICAgICAgJzwvYm9keT48L2h0bWw+JyxcbiAgICAgICdFT0YnLFxuICAgICAgJ2VjaG8gXCJJbnN0YW5jZSBzZXR1cCBjb21wbGV0ZVwiID4gL3RtcC9zZXR1cC1jb21wbGV0ZS50eHQnLFxuICAgICk7XG5cbiAgICAvLyDilIDilIAgRW52aXJvbm1lbnQgY29uZmlndXJhdGlvbiDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICBjb25zdCBlbnZpcm9ubWVudHMgPSBbXG4gICAgICB7XG4gICAgICAgIG5hbWU6ICdkZXYnLCBjb3VudDogMiwgY3JpdGljYWxpdHk6ICdMb3cnLFxuICAgICAgICB0ZWFtczogWydwbGF0Zm9ybScsICdhcGknXSxcbiAgICAgICAgcHJvZHVjdHM6IFsnYXBpLWdhdGV3YXknLCAndXNlci1zZXJ2aWNlJ10sXG4gICAgICAgIGNvbXBsaWFuY2VGcmFtZXdvcmtzOiBbJ1NPQzInLCAnU09DMiddLFxuICAgICAgICBzbGFPdmVycmlkZXM6IFtcbiAgICAgICAgICB7ICdTTEEtQ1JJVElDQUwnOiAnMjQnLCAnU0xBLUhJR0gnOiAnNzInLCAnU0xBLU1FRElVTSc6ICcxNjgnLCAnU0xBLUxPVyc6ICc3MjAnIH0sXG4gICAgICAgICAgeyAnU0xBLUNSSVRJQ0FMJzogJzI0JywgJ1NMQS1ISUdIJzogJzcyJywgJ1NMQS1NRURJVU0nOiAnMTY4JywgJ1NMQS1MT1cnOiAnNzIwJyB9LFxuICAgICAgICBdLFxuICAgICAgfSxcbiAgICAgIHtcbiAgICAgICAgbmFtZTogJ3N0YWdpbmcnLCBjb3VudDogMSwgY3JpdGljYWxpdHk6ICdNZWRpdW0nLFxuICAgICAgICB0ZWFtczogWydwbGF0Zm9ybSddLFxuICAgICAgICBwcm9kdWN0czogWydhcGktZ2F0ZXdheSddLFxuICAgICAgICBjb21wbGlhbmNlRnJhbWV3b3JrczogWydQQ0ktRFNTJ10sXG4gICAgICAgIHNsYU92ZXJyaWRlczogW1xuICAgICAgICAgIHsgJ1NMQS1DUklUSUNBTCc6ICcxMicsICdTTEEtSElHSCc6ICc0OCcsICdTTEEtTUVESVVNJzogJzE2OCcsICdTTEEtTE9XJzogJzcyMCcgfSxcbiAgICAgICAgXSxcbiAgICAgIH0sXG4gICAgICB7XG4gICAgICAgIG5hbWU6ICdwcm9kJywgY291bnQ6IDIsIGNyaXRpY2FsaXR5OiAnSGlnaCcsXG4gICAgICAgIHRlYW1zOiBbJ3BsYXRmb3JtJywgJ3NlY3VyaXR5J10sXG4gICAgICAgIHByb2R1Y3RzOiBbJ2FwaS1nYXRld2F5JywgJ2F1dGgtc2VydmljZSddLFxuICAgICAgICBjb21wbGlhbmNlRnJhbWV3b3JrczogWydTT0MyLEhJUEFBJywgJ1BDSS1EU1MsU09DMiddLFxuICAgICAgICBzbGFPdmVycmlkZXM6IFtcbiAgICAgICAgICB7ICdTTEEtQ1JJVElDQUwnOiAnMjQnLCAnU0xBLUhJR0gnOiAnNzInLCAnU0xBLU1FRElVTSc6ICcxNjgnLCAnU0xBLUxPVyc6ICc3MjAnIH0sXG4gICAgICAgICAgeyAnU0xBLUNSSVRJQ0FMJzogJzYnLCAgJ1NMQS1ISUdIJzogJzI0JywgJ1NMQS1NRURJVU0nOiAnMTY4JywgJ1NMQS1MT1cnOiAnNzIwJyB9LFxuICAgICAgICBdLFxuICAgICAgfSxcbiAgICBdO1xuXG4gICAgLy8g4pSA4pSAIENyZWF0ZSBpbnN0YW5jZXMg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSAXG4gICAgZW52aXJvbm1lbnRzLmZvckVhY2goZW52ID0+IHtcbiAgICAgIGZvciAobGV0IGkgPSAxOyBpIDw9IGVudi5jb3VudDsgaSsrKSB7XG4gICAgICAgIGNvbnN0IGluc3RhbmNlID0gbmV3IGVjMi5JbnN0YW5jZSh0aGlzLCBgJHtlbnYubmFtZX0taW5zdGFuY2UtJHtpfWAsIHtcbiAgICAgICAgICB2cGM6IHZwYyxcbiAgICAgICAgICB2cGNTdWJuZXRzOiB7IHN1Ym5ldFR5cGU6IGVjMi5TdWJuZXRUeXBlLlBSSVZBVEVfV0lUSF9FR1JFU1MgfSxcbiAgICAgICAgICBpbnN0YW5jZVR5cGU6IGVjMi5JbnN0YW5jZVR5cGUub2YoZWMyLkluc3RhbmNlQ2xhc3MuVDMsIGVjMi5JbnN0YW5jZVNpemUuTUlDUk8pLFxuICAgICAgICAgIC8vIEludGVudGlvbmFsbHkgb2xkIEFNSSDigJQgZW5zdXJlcyB2dWxuZXJhYmlsaXRpZXMgZXhpc3QgZm9yIGRlbW8vdGVzdGluZ1xuICAgICAgICAgIG1hY2hpbmVJbWFnZTogZWMyLk1hY2hpbmVJbWFnZS5nZW5lcmljTGludXgoe1xuICAgICAgICAgICAgJ3VzLWVhc3QtMSc6ICdhbWktMDA3Zjk3NDQ4OTFjNDU1MDMnLFxuICAgICAgICAgIH0pLFxuICAgICAgICAgIHNlY3VyaXR5R3JvdXA6IGluc3RhbmNlU0csXG4gICAgICAgICAgcm9sZTogaW5zdGFuY2VSb2xlLFxuICAgICAgICAgIHVzZXJEYXRhLFxuICAgICAgICAgIHVzZXJEYXRhQ2F1c2VzUmVwbGFjZW1lbnQ6IGZhbHNlLFxuICAgICAgICB9KTtcblxuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKCdOYW1lJywgYCR7ZW52Lm5hbWV9LXdlYnNlcnZlci0ke2l9YCk7XG4gICAgICAgIGNkay5UYWdzLm9mKGluc3RhbmNlKS5hZGQoJ0Vudmlyb25tZW50JywgZW52Lm5hbWUpO1xuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKCdBcHBsaWNhdGlvbicsICdXZWJTZXJ2ZXInKTtcbiAgICAgICAgY2RrLlRhZ3Mub2YoaW5zdGFuY2UpLmFkZCgnQ3JpdGljYWxpdHknLCBlbnYuY3JpdGljYWxpdHkpO1xuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKCdQYXRjaEdyb3VwJywgYCR7ZW52Lm5hbWV9LXBhdGNoLWdyb3VwYCk7XG4gICAgICAgIGNkay5UYWdzLm9mKGluc3RhbmNlKS5hZGQoJ01hbmFnZWRCeScsICdJbnRlbGxpZ2VudFBhdGNoQXV0b21hdGlvbicpO1xuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKFxuICAgICAgICAgIHByb2Nlc3MuZW52LlNTTV9TQ09QRV9UQUdfS0VZIHx8ICdQYXRjaEF1dG9tYXRpb24nLFxuICAgICAgICAgIHByb2Nlc3MuZW52LlNTTV9TQ09QRV9UQUdfVkFMVUUgfHwgJ2VuYWJsZWQnLFxuICAgICAgICApO1xuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKCdUZWFtJywgZW52LnRlYW1zW2kgLSAxXSk7XG4gICAgICAgIGNkay5UYWdzLm9mKGluc3RhbmNlKS5hZGQoJ1Byb2R1Y3QnLCBlbnYucHJvZHVjdHNbaSAtIDFdKTtcbiAgICAgICAgY2RrLlRhZ3Mub2YoaW5zdGFuY2UpLmFkZCgnQ29zdENlbnRlcicsIGAke2Vudi50ZWFtc1tpIC0gMV19LWVuZ2luZWVyaW5nYCk7XG4gICAgICAgIGNkay5UYWdzLm9mKGluc3RhbmNlKS5hZGQoJ093bmVyJywgYCR7ZW52LnRlYW1zW2kgLSAxXX0tbGVhZEBleGFtcGxlLmNvbWApO1xuICAgICAgICBjZGsuVGFncy5vZihpbnN0YW5jZSkuYWRkKCdDb21wbGlhbmNlRnJhbWV3b3JrcycsIGVudi5jb21wbGlhbmNlRnJhbWV3b3Jrc1tpIC0gMV0pO1xuICAgICAgICAvLyBQZXItaW5zdGFuY2UgU0xBIG92ZXJyaWRlcyAob3B0aW9uYWwg4oCUIGRlbW9uc3RyYXRlcyB0YWctYmFzZWQgU0xBKVxuICAgICAgICBjb25zdCBzbGFPdmVycmlkZSA9IGVudi5zbGFPdmVycmlkZXM/LltpIC0gMV0gPz8ge307XG4gICAgICAgIGZvciAoY29uc3QgW2tleSwgdmFsdWVdIG9mIE9iamVjdC5lbnRyaWVzKHNsYU92ZXJyaWRlKSkge1xuICAgICAgICAgIGNkay5UYWdzLm9mKGluc3RhbmNlKS5hZGQoa2V5LCB2YWx1ZSBhcyBzdHJpbmcpO1xuICAgICAgICB9XG5cbiAgICAgICAgdGhpcy5pbnN0YW5jZXMucHVzaChpbnN0YW5jZSk7XG5cbiAgICAgICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgYCR7ZW52Lm5hbWV9LWluc3RhbmNlLSR7aX0taWRgLCB7XG4gICAgICAgICAgdmFsdWU6IGluc3RhbmNlLmluc3RhbmNlSWQsXG4gICAgICAgICAgZGVzY3JpcHRpb246IGBJbnN0YW5jZSBJRCBmb3IgJHtlbnYubmFtZX0gd2Vic2VydmVyICR7aX1gLFxuICAgICAgICB9KTtcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIC8vIOKUgOKUgCBNYWludGVuYW5jZSB3aW5kb3dzIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgFxuICAgIGNvbnN0IGRldldpbmRvdyA9IG5ldyBzc20uQ2ZuTWFpbnRlbmFuY2VXaW5kb3codGhpcywgJ0Rldk1haW50ZW5hbmNlV2luZG93Jywge1xuICAgICAgbmFtZTogJ2Rldi1kYWlseS1wYXRjaGluZycsXG4gICAgICBkZXNjcmlwdGlvbjogJ0RhaWx5IG1haW50ZW5hbmNlIHdpbmRvdyBmb3IgZGV2ZWxvcG1lbnQgZW52aXJvbm1lbnQnLFxuICAgICAgc2NoZWR1bGU6ICdjcm9uKDAgMSA/ICogKiAqKScsXG4gICAgICBkdXJhdGlvbjogMiwgY3V0b2ZmOiAwLCBhbGxvd1VuYXNzb2NpYXRlZFRhcmdldHM6IGZhbHNlLFxuICAgIH0pO1xuICAgIGNvbnN0IHN0YWdpbmdXaW5kb3cgPSBuZXcgc3NtLkNmbk1haW50ZW5hbmNlV2luZG93KHRoaXMsICdTdGFnaW5nTWFpbnRlbmFuY2VXaW5kb3cnLCB7XG4gICAgICBuYW1lOiAnc3RhZ2luZy13ZWVrbHktcGF0Y2hpbmcnLFxuICAgICAgZGVzY3JpcHRpb246ICdXZWVrbHkgbWFpbnRlbmFuY2Ugd2luZG93IGZvciBzdGFnaW5nIGVudmlyb25tZW50JyxcbiAgICAgIHNjaGVkdWxlOiAnY3JvbigwIDIgPyAqIFRVRSAqKScsXG4gICAgICBkdXJhdGlvbjogMiwgY3V0b2ZmOiAwLCBhbGxvd1VuYXNzb2NpYXRlZFRhcmdldHM6IGZhbHNlLFxuICAgIH0pO1xuICAgIGNvbnN0IHByb2RXaW5kb3cgPSBuZXcgc3NtLkNmbk1haW50ZW5hbmNlV2luZG93KHRoaXMsICdQcm9kTWFpbnRlbmFuY2VXaW5kb3cnLCB7XG4gICAgICBuYW1lOiAncHJvZC1tb250aGx5LXBhdGNoaW5nJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnTW9udGhseSBtYWludGVuYW5jZSB3aW5kb3cgZm9yIHByb2R1Y3Rpb24gZW52aXJvbm1lbnQnLFxuICAgICAgc2NoZWR1bGU6ICdjcm9uKDAgMiAxICogPyAqKScsXG4gICAgICBkdXJhdGlvbjogNCwgY3V0b2ZmOiAxLCBhbGxvd1VuYXNzb2NpYXRlZFRhcmdldHM6IGZhbHNlLFxuICAgIH0pO1xuXG4gICAgLy8gUmVnaXN0ZXIgdGFyZ2V0c1xuICAgIGNvbnN0IHNjb3BlVGFnS2V5ID0gcHJvY2Vzcy5lbnYuU1NNX1NDT1BFX1RBR19LRVkgfHwgJ1BhdGNoQXV0b21hdGlvbic7XG4gICAgY29uc3Qgc2NvcGVUYWdWYWx1ZSA9IHByb2Nlc3MuZW52LlNTTV9TQ09QRV9UQUdfVkFMVUUgfHwgJ2VuYWJsZWQnO1xuXG4gICAgbmV3IHNzbS5DZm5NYWludGVuYW5jZVdpbmRvd1RhcmdldCh0aGlzLCAnRGV2V2luZG93VGFyZ2V0Jywge1xuICAgICAgd2luZG93SWQ6IGRldldpbmRvdy5yZWYsIHJlc291cmNlVHlwZTogJ0lOU1RBTkNFJyxcbiAgICAgIHRhcmdldHM6IFtcbiAgICAgICAgeyBrZXk6ICd0YWc6RW52aXJvbm1lbnQnLCB2YWx1ZXM6IFsnZGV2J10gfSxcbiAgICAgICAgeyBrZXk6IGB0YWc6JHtzY29wZVRhZ0tleX1gLCB2YWx1ZXM6IFtzY29wZVRhZ1ZhbHVlXSB9LFxuICAgICAgXSxcbiAgICAgIG5hbWU6ICdkZXYtaW5zdGFuY2VzJyxcbiAgICB9KTtcbiAgICBuZXcgc3NtLkNmbk1haW50ZW5hbmNlV2luZG93VGFyZ2V0KHRoaXMsICdTdGFnaW5nV2luZG93VGFyZ2V0Jywge1xuICAgICAgd2luZG93SWQ6IHN0YWdpbmdXaW5kb3cucmVmLCByZXNvdXJjZVR5cGU6ICdJTlNUQU5DRScsXG4gICAgICB0YXJnZXRzOiBbXG4gICAgICAgIHsga2V5OiAndGFnOkVudmlyb25tZW50JywgdmFsdWVzOiBbJ3N0YWdpbmcnXSB9LFxuICAgICAgICB7IGtleTogYHRhZzoke3Njb3BlVGFnS2V5fWAsIHZhbHVlczogW3Njb3BlVGFnVmFsdWVdIH0sXG4gICAgICBdLFxuICAgICAgbmFtZTogJ3N0YWdpbmctaW5zdGFuY2VzJyxcbiAgICB9KTtcbiAgICBuZXcgc3NtLkNmbk1haW50ZW5hbmNlV2luZG93VGFyZ2V0KHRoaXMsICdQcm9kV2luZG93VGFyZ2V0Jywge1xuICAgICAgd2luZG93SWQ6IHByb2RXaW5kb3cucmVmLCByZXNvdXJjZVR5cGU6ICdJTlNUQU5DRScsXG4gICAgICB0YXJnZXRzOiBbXG4gICAgICAgIHsga2V5OiAndGFnOkVudmlyb25tZW50JywgdmFsdWVzOiBbJ3Byb2QnXSB9LFxuICAgICAgICB7IGtleTogYHRhZzoke3Njb3BlVGFnS2V5fWAsIHZhbHVlczogW3Njb3BlVGFnVmFsdWVdIH0sXG4gICAgICBdLFxuICAgICAgbmFtZTogJ3Byb2QtaW5zdGFuY2VzJyxcbiAgICB9KTtcblxuICAgIC8vIOKUgOKUgCBTU00gQXNzb2NpYXRpb25zIChpbnZlbnRvcnkgKyBwYXRjaCBzY2FuKSDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICAvLyBJbnZlbnRvcnkgY29sbGVjdGlvbiDigJQgZW5zdXJlcyBpbnN0YW5jZXMgYXBwZWFyIGluIFNTTSBFeHBsb3JlclxuICAgIC8vIHJlZ2FyZGxlc3Mgb2Ygd2hldGhlciBRdWljayBTZXR1cCBpcyBjb25maWd1cmVkIGluIHRoaXMgYWNjb3VudC5cbiAgICBuZXcgc3NtLkNmbkFzc29jaWF0aW9uKHRoaXMsICdJbnZlbnRvcnlBc3NvY2lhdGlvbicsIHtcbiAgICAgIG5hbWU6ICdBV1MtR2F0aGVyU29mdHdhcmVJbnZlbnRvcnknLFxuICAgICAgdGFyZ2V0czogW3sga2V5OiBgdGFnOiR7c2NvcGVUYWdLZXl9YCwgdmFsdWVzOiBbc2NvcGVUYWdWYWx1ZV0gfV0sXG4gICAgICBzY2hlZHVsZUV4cHJlc3Npb246ICdyYXRlKDMwIG1pbnV0ZXMpJyxcbiAgICAgIGFzc29jaWF0aW9uTmFtZTogJ1BhdGNoeS1TYW1wbGVFbnYtSW52ZW50b3J5JyxcbiAgICB9KTtcblxuICAgIC8vIFBhdGNoIHNjYW4g4oCUIHBvcHVsYXRlcyBwYXRjaCBjb21wbGlhbmNlIGRhdGEgKE1pc3NpbmdDb3VudCwgSW5zdGFsbGVkQ291bnQpXG4gICAgLy8gc28gdGhlIGRhc2hib2FyZCBhbmQgYWdlbnQgaGF2ZSBkYXRhIGltbWVkaWF0ZWx5IGFmdGVyIGRlcGxveS5cbiAgICAvLyBTY2FuIG9ubHkg4oCUIGRvZXMgTk9UIGluc3RhbGwgcGF0Y2hlcy5cbiAgICBuZXcgc3NtLkNmbkFzc29jaWF0aW9uKHRoaXMsICdQYXRjaFNjYW5Bc3NvY2lhdGlvbicsIHtcbiAgICAgIG5hbWU6ICdBV1MtUnVuUGF0Y2hCYXNlbGluZScsXG4gICAgICB0YXJnZXRzOiBbeyBrZXk6IGB0YWc6JHtzY29wZVRhZ0tleX1gLCB2YWx1ZXM6IFtzY29wZVRhZ1ZhbHVlXSB9XSxcbiAgICAgIHNjaGVkdWxlRXhwcmVzc2lvbjogJ3JhdGUoMTIgaG91cnMpJyxcbiAgICAgIGFzc29jaWF0aW9uTmFtZTogJ1BhdGNoeS1TYW1wbGVFbnYtUGF0Y2hTY2FuJyxcbiAgICAgIHBhcmFtZXRlcnM6IHtcbiAgICAgICAgT3BlcmF0aW9uOiBbJ1NjYW4nXSxcbiAgICAgIH0sXG4gICAgfSk7XG5cbiAgICAvLyDilIDilIAgQUxCcyArIHRhcmdldCBncm91cHMgKGZvciBkZXBlbmRlbmN5IGFuYWx5c2lzKSDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICBbJ2RldicsICdzdGFnaW5nJywgJ3Byb2QnXS5mb3JFYWNoKGVudk5hbWUgPT4ge1xuICAgICAgY29uc3QgYWxiID0gbmV3IGVsYnYyLkFwcGxpY2F0aW9uTG9hZEJhbGFuY2VyKHRoaXMsIGAke2Vudk5hbWV9QUxCYCwge1xuICAgICAgICB2cGM6IHZwYywgaW50ZXJuZXRGYWNpbmc6IGZhbHNlLFxuICAgICAgICBsb2FkQmFsYW5jZXJOYW1lOiBgJHtlbnZOYW1lfS1wYXRjaC1hdXRvbWF0aW9uLWFsYmAsXG4gICAgICAgIHNlY3VyaXR5R3JvdXA6IGluc3RhbmNlU0csXG4gICAgICB9KTtcbiAgICAgIGNvbnN0IHRnID0gbmV3IGVsYnYyLkFwcGxpY2F0aW9uVGFyZ2V0R3JvdXAodGhpcywgYCR7ZW52TmFtZX1UR2AsIHtcbiAgICAgICAgdnBjOiB2cGMsIHBvcnQ6IDgwLFxuICAgICAgICBwcm90b2NvbDogZWxidjIuQXBwbGljYXRpb25Qcm90b2NvbC5IVFRQLFxuICAgICAgICB0YXJnZXRUeXBlOiBlbGJ2Mi5UYXJnZXRUeXBlLklOU1RBTkNFLFxuICAgICAgICB0YXJnZXRHcm91cE5hbWU6IGAke2Vudk5hbWV9LXBhdGNoLWF1dG9tYXRpb24tdGdgLFxuICAgICAgICBoZWFsdGhDaGVjazogeyBwYXRoOiAnLycsIGludGVydmFsOiBjZGsuRHVyYXRpb24uc2Vjb25kcygzMCksIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDUpLCBoZWFsdGh5VGhyZXNob2xkQ291bnQ6IDIsIHVuaGVhbHRoeVRocmVzaG9sZENvdW50OiAzIH0sXG4gICAgICB9KTtcbiAgICAgIGFsYi5hZGRMaXN0ZW5lcihgJHtlbnZOYW1lfUxpc3RlbmVyYCwgeyBwb3J0OiA4MCwgZGVmYXVsdFRhcmdldEdyb3VwczogW3RnXSB9KTtcbiAgICAgIHRoaXMuaW5zdGFuY2VzLmZpbHRlcihpID0+IGkubm9kZS5pZC50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKGVudk5hbWUpKVxuICAgICAgICAuZm9yRWFjaChpbnN0ID0+IHRnLmFkZFRhcmdldChuZXcgdGFyZ2V0cy5JbnN0YW5jZVRhcmdldChpbnN0LCA4MCkpKTtcbiAgICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsIGAke2Vudk5hbWV9QUxCRE5TYCwge1xuICAgICAgICB2YWx1ZTogYWxiLmxvYWRCYWxhbmNlckRuc05hbWUsXG4gICAgICAgIGRlc2NyaXB0aW9uOiBgJHtlbnZOYW1lfSBBTEIgRE5TIG5hbWVgLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICAvLyDilIDilIAgQ2xvdWRXYXRjaCBhbGFybXMgKHN0YWdpbmcgc2FtcGxlKSDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIBcbiAgICB0aGlzLmluc3RhbmNlcy5maWx0ZXIoaSA9PiBpLm5vZGUuaWQudG9Mb3dlckNhc2UoKS5pbmNsdWRlcygnc3RhZ2luZycpKS5zbGljZSgwLCAzKVxuICAgICAgLmZvckVhY2goKGluc3RhbmNlLCBpZHgpID0+IHtcbiAgICAgICAgbmV3IGNsb3Vkd2F0Y2guQWxhcm0odGhpcywgYHN0YWdpbmctaHR0cGQtYWxhcm0tJHtpZHggKyAxfWAsIHtcbiAgICAgICAgICBhbGFybU5hbWU6IGBzdGFnaW5nLWluc3RhbmNlLSR7aWR4ICsgMX0taHR0cGQtaGVhbHRoYCxcbiAgICAgICAgICBhbGFybURlc2NyaXB0aW9uOiBgQXBhY2hlIGh0dHBkIGhlYWx0aCBmb3Igc3RhZ2luZyBpbnN0YW5jZSAke2lkeCArIDF9YCxcbiAgICAgICAgICBtZXRyaWM6IG5ldyBjbG91ZHdhdGNoLk1ldHJpYyh7XG4gICAgICAgICAgICBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ3Byb2NzdGF0X3J1bm5pbmcnLFxuICAgICAgICAgICAgZGltZW5zaW9uc01hcDogeyBJbnN0YW5jZUlkOiBpbnN0YW5jZS5pbnN0YW5jZUlkLCBwcm9jZXNzX25hbWU6ICdodHRwZCcgfSxcbiAgICAgICAgICAgIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnLCBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpLFxuICAgICAgICAgIH0pLFxuICAgICAgICAgIHRocmVzaG9sZDogMSwgZXZhbHVhdGlvblBlcmlvZHM6IDIsXG4gICAgICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjbG91ZHdhdGNoLkNvbXBhcmlzb25PcGVyYXRvci5MRVNTX1RIQU5fVEhSRVNIT0xELFxuICAgICAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IGNsb3Vkd2F0Y2guVHJlYXRNaXNzaW5nRGF0YS5CUkVBQ0hJTkcsXG4gICAgICAgIH0pO1xuICAgICAgfSk7XG5cbiAgICAvLyDilIDilIAgUGF0Y2ggYmFzZWxpbmVzIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgFxuICAgIG5ldyBzc20uQ2ZuUGF0Y2hCYXNlbGluZSh0aGlzLCAnRGV2UGF0Y2hCYXNlbGluZScsIHtcbiAgICAgIG5hbWU6ICdkZXYtYmFzZWxpbmUnLCBkZXNjcmlwdGlvbjogJ0RldiAtIGltbWVkaWF0ZSBwYXRjaGluZycsIG9wZXJhdGluZ1N5c3RlbTogJ0FNQVpPTl9MSU5VWF8yJyxcbiAgICAgIGFwcHJvdmFsUnVsZXM6IHsgcGF0Y2hSdWxlczogW3sgcGF0Y2hGaWx0ZXJHcm91cDogeyBwYXRjaEZpbHRlcnM6IFt7IGtleTogJ1NFVkVSSVRZJywgdmFsdWVzOiBbJ0NyaXRpY2FsJywgJ0ltcG9ydGFudCcsICdNZWRpdW0nLCAnTG93J10gfV0gfSwgYXBwcm92ZUFmdGVyRGF5czogMCwgZW5hYmxlTm9uU2VjdXJpdHk6IHRydWUsIGNvbXBsaWFuY2VMZXZlbDogJ0NSSVRJQ0FMJyB9XSB9LFxuICAgICAgdGFnczogW3sga2V5OiAnTWFuYWdlZEJ5JywgdmFsdWU6ICdJbnRlbGxpZ2VudFBhdGNoQXV0b21hdGlvbicgfV0sXG4gICAgfSk7XG4gICAgbmV3IHNzbS5DZm5QYXRjaEJhc2VsaW5lKHRoaXMsICdTdGFnaW5nUGF0Y2hCYXNlbGluZScsIHtcbiAgICAgIG5hbWU6ICdzdGFnaW5nLWJhc2VsaW5lJywgZGVzY3JpcHRpb246ICdTdGFnaW5nIC0gY3JpdGljYWwvaW1wb3J0YW50IG9ubHknLCBvcGVyYXRpbmdTeXN0ZW06ICdBTUFaT05fTElOVVhfMicsXG4gICAgICBhcHByb3ZhbFJ1bGVzOiB7IHBhdGNoUnVsZXM6IFt7IHBhdGNoRmlsdGVyR3JvdXA6IHsgcGF0Y2hGaWx0ZXJzOiBbeyBrZXk6ICdTRVZFUklUWScsIHZhbHVlczogWydDcml0aWNhbCcsICdJbXBvcnRhbnQnXSB9XSB9LCBhcHByb3ZlQWZ0ZXJEYXlzOiAwLCBlbmFibGVOb25TZWN1cml0eTogZmFsc2UsIGNvbXBsaWFuY2VMZXZlbDogJ0hJR0gnIH1dIH0sXG4gICAgICB0YWdzOiBbeyBrZXk6ICdNYW5hZ2VkQnknLCB2YWx1ZTogJ0ludGVsbGlnZW50UGF0Y2hBdXRvbWF0aW9uJyB9XSxcbiAgICB9KTtcbiAgICBuZXcgc3NtLkNmblBhdGNoQmFzZWxpbmUodGhpcywgJ1Byb2RQYXRjaEJhc2VsaW5lJywge1xuICAgICAgbmFtZTogJ3Byb2QtYmFzZWxpbmUnLCBkZXNjcmlwdGlvbjogJ1Byb2QgLSA3LWRheSBhcHByb3ZhbCBkZWxheScsIG9wZXJhdGluZ1N5c3RlbTogJ0FNQVpPTl9MSU5VWF8yJyxcbiAgICAgIGFwcHJvdmFsUnVsZXM6IHsgcGF0Y2hSdWxlczogW3sgcGF0Y2hGaWx0ZXJHcm91cDogeyBwYXRjaEZpbHRlcnM6IFt7IGtleTogJ1NFVkVSSVRZJywgdmFsdWVzOiBbJ0NyaXRpY2FsJywgJ0ltcG9ydGFudCddIH1dIH0sIGFwcHJvdmVBZnRlckRheXM6IDcsIGVuYWJsZU5vblNlY3VyaXR5OiBmYWxzZSwgY29tcGxpYW5jZUxldmVsOiAnQ1JJVElDQUwnIH1dIH0sXG4gICAgICB0YWdzOiBbeyBrZXk6ICdNYW5hZ2VkQnknLCB2YWx1ZTogJ0ludGVsbGlnZW50UGF0Y2hBdXRvbWF0aW9uJyB9XSxcbiAgICB9KTtcblxuICAgIC8vIOKUgOKUgCBPdXRwdXRzIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgFxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdUb3RhbEluc3RhbmNlcycsIHsgdmFsdWU6IHRoaXMuaW5zdGFuY2VzLmxlbmd0aC50b1N0cmluZygpIH0pO1xuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdEZXZNYWludGVuYW5jZVdpbmRvd0lkJywgeyB2YWx1ZTogZGV2V2luZG93LnJlZiB9KTtcbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnU3RhZ2luZ01haW50ZW5hbmNlV2luZG93SWQnLCB7IHZhbHVlOiBzdGFnaW5nV2luZG93LnJlZiB9KTtcbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnUHJvZE1haW50ZW5hbmNlV2luZG93SWQnLCB7IHZhbHVlOiBwcm9kV2luZG93LnJlZiB9KTtcbiAgfVxufVxuIl19