import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import * as path from 'path';

export interface UIStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  agentCoreRoleArn?: string;
  certificateArn?: string;
}

export class UIStack extends cdk.Stack {
  public readonly albDnsName: string;

  constructor(scope: Construct, id: string, props: UIStackProps) {
    super(scope, id, props);

    // ECS Cluster
    const cluster = new ecs.Cluster(this, 'UICluster', {
      vpc: props.vpc,
      containerInsights: true,
    });

    // Build Docker image from project root
    const image = new ecr_assets.DockerImageAsset(this, 'UIImage', {
      directory: path.join(__dirname, '..', '..'),  // project root
      file: 'Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,  // Fargate runs x86_64
      exclude: ['infra/node_modules', 'infra/cdk.out', 'venv', '.git', 'node_modules'],
    });

    // Task execution role (ECR pull, CloudWatch logs)
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });

    // Task role — needs same AWS permissions as the local server.py
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });

    // The UI backend calls: EC2, SSM, Inspector, STS, AgentCore
    // Read-only describe/list actions — resource-level scoping not supported
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ec2:DescribeInstances',
        'ec2:DescribeTags',
        'ssm:DescribeInstanceInformation',
        'inspector2:ListFindings',
        'sts:GetCallerIdentity',
      ],
      resources: ['*'],
    }));

    // S3 — scoped to compliance reports bucket only
    const accountId = cdk.Stack.of(this).account;
    const complianceBucketArn = `arn:aws:s3:::patch-compliance-reports-${accountId}`;
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject',
        's3:ListBucket',
        's3:HeadBucket',
      ],
      resources: [
        complianceBucketArn,
        `${complianceBucketArn}/*`,
      ],
    }));

    // AgentCore — scoped to specific agent ARN if provided, otherwise wildcard
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: props.agentCoreRoleArn
        ? [`arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:*`]
        : ['*'],
    }));

    // Log group
    const logGroup = new logs.LogGroup(this, 'UILogGroup', {
      logGroupName: '/patch-automation/ui',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Fargate task definition
    const taskDef = new ecs.FargateTaskDefinition(this, 'UITaskDef', {
      memoryLimitMiB: 512,
      cpu: 256,
      executionRole,
      taskRole,
    });

    taskDef.addContainer('ui', {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      portMappings: [{ containerPort: 8000 }],
      environment: {
        'STATIC_DIR': '/app/static',
        'AWS_DEFAULT_REGION': cdk.Stack.of(this).region,
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'ui',
      }),
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/api/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ALB — internet-facing for operator access
    const alb = new elbv2.ApplicationLoadBalancer(this, 'UIALB', {
      vpc: props.vpc,
      internetFacing: true,
    });

    // Fargate service
    const service = new ecs.FargateService(this, 'UIService', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    // ALB listeners — HTTPS if certificate provided, HTTP fallback otherwise
    if (props.certificateArn) {
      // HTTPS listener (primary)
      const httpsListener = alb.addListener('HTTPSListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(props.certificateArn)],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
      });

      httpsListener.addTargets('UITarget', {
        port: 8000,
        protocol: elbv2.ApplicationProtocol.HTTP,
        targets: [service],
        healthCheck: {
          path: '/api/health',
          interval: cdk.Duration.seconds(30),
          timeout: cdk.Duration.seconds(5),
          healthyThresholdCount: 2,
          unhealthyThresholdCount: 3,
        },
        deregistrationDelay: cdk.Duration.seconds(30),
      });

      // HTTP → HTTPS redirect
      alb.addListener('HTTPRedirect', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
    } else {
      // HTTP only (no certificate)
      const listener = alb.addListener('HTTPListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
      });

      listener.addTargets('UITarget', {
        port: 8000,
        protocol: elbv2.ApplicationProtocol.HTTP,
        targets: [service],
        healthCheck: {
          path: '/api/health',
          interval: cdk.Duration.seconds(30),
          timeout: cdk.Duration.seconds(5),
          healthyThresholdCount: 2,
          unhealthyThresholdCount: 3,
        },
        deregistrationDelay: cdk.Duration.seconds(30),
      });
    }

    // Allow ALB to reach Fargate tasks
    service.connections.allowFrom(alb, ec2.Port.tcp(8000), 'ALB to Fargate');

    // Increase ALB idle timeout for SSE streaming (chat responses can take minutes)
    alb.setAttribute('idle_timeout.timeout_seconds', '300');

    this.albDnsName = alb.loadBalancerDnsName;

    // Outputs
    const protocol = props.certificateArn ? 'https' : 'http';
    new cdk.CfnOutput(this, 'UIUrl', {
      value: `${protocol}://${alb.loadBalancerDnsName}`,
      description: 'Patch Automation UI URL',
    });

    new cdk.CfnOutput(this, 'UILogGroupName', {
      value: logGroup.logGroupName,
      description: 'CloudWatch log group for UI container',
    });
  }
}
