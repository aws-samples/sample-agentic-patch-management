import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as elbv2_actions from 'aws-cdk-lib/aws-elasticloadbalancingv2-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import * as path from 'path';

export interface UIStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  agentCoreRoleArn?: string;
  certificateArn?: string;
  /** Enable Cognito auth + internet-facing ALB. Requires certificateArn. */
  cognitoEnabled?: boolean;
  /** Cognito hosted UI domain prefix (must be globally unique). */
  cognitoDomainPrefix?: string;
}

export class UIStack extends cdk.Stack {
  public readonly albDnsName: string;

  constructor(scope: Construct, id: string, props: UIStackProps) {
    super(scope, id, props);

    const cognitoEnabled = props.cognitoEnabled ?? false;
    const accountId = cdk.Stack.of(this).account;

    // ── ECS Cluster ────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, 'UICluster', {
      vpc: props.vpc,
      containerInsights: true,
    });

    // ── Docker image ───────────────────────────────────────────────
    const image = new ecr_assets.DockerImageAsset(this, 'UIImage', {
      directory: path.join(__dirname, '..', '..'),
      file: 'Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,
      exclude: ['infra/node_modules', 'infra/cdk.out', 'venv', '.git', 'node_modules'],
    });

    // ── IAM roles ──────────────────────────────────────────────────
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });

    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ec2:DescribeInstances', 'ec2:DescribeTags',
        'ssm:DescribeInstanceInformation', 'inspector2:ListFindings',
        'sts:GetCallerIdentity',
      ],
      resources: ['*'],
    }));
    const complianceBucketArn = `arn:aws:s3:::patch-compliance-reports-${accountId}`;
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:ListBucket', 's3:HeadBucket'],
      resources: [complianceBucketArn, `${complianceBucketArn}/*`],
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: props.agentCoreRoleArn
        ? [`arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:*`]
        : ['*'],
    }));

    // ── Log group ──────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'UILogGroup', {
      logGroupName: '/patch-automation/ui',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Fargate task ───────────────────────────────────────────────
    const taskDef = new ecs.FargateTaskDefinition(this, 'UITaskDef', {
      memoryLimitMiB: 512,
      cpu: 256,
      executionRole,
      taskRole,
    });
    const containerEnv: Record<string, string> = {
      'STATIC_DIR': '/app/static',
      'AWS_DEFAULT_REGION': cdk.Stack.of(this).region,
    };
    if (cognitoEnabled && props.cognitoDomainPrefix) {
      containerEnv['COGNITO_DOMAIN_PREFIX'] = props.cognitoDomainPrefix;
    }

    taskDef.addContainer('ui', {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      portMappings: [{ containerPort: 8000 }],
      environment: containerEnv,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'ui' }),
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/api/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ── ALB ────────────────────────────────────────────────────────
    // Cognito mode: internet-facing + auth. Default: internal + SSM tunnel.
    const alb = new elbv2.ApplicationLoadBalancer(this, 'UIALB', {
      vpc: props.vpc,
      internetFacing: cognitoEnabled,
    });
    alb.setAttribute('idle_timeout.timeout_seconds', '300');

    // ── Fargate service ────────────────────────────────────────────
    const service = new ecs.FargateService(this, 'UIService', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
    service.connections.allowFrom(alb, ec2.Port.tcp(8000), 'ALB to Fargate');

    // ── Target group (shared) ──────────────────────────────────────
    // Logical ID includes Cognito flag so switching ALB scheme forces a new TG
    // (a TG can't be shared between two ALBs during CloudFormation replacement).
    const tgId = cognitoEnabled ? 'UITargetGroupPublic' : 'UITargetGroup';
    const targetGroup = new elbv2.ApplicationTargetGroup(this, tgId, {
      vpc: props.vpc,
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

    // ── Cognito (opt-in) ───────────────────────────────────────────
    // cognitoEnabled=true  → HTTPS listener with Cognito auth action (internet-facing ALB)
    // cognitoEnabled=false → HTTPS or HTTP listener, no auth (internal ALB + bastion)
    if (cognitoEnabled && props.certificateArn) {
      const domainPrefix = props.cognitoDomainPrefix ?? `patchy-${accountId}`;

      const userPool = new cognito.UserPool(this, 'UserPool', {
        userPoolName: 'patchy-users',
        selfSignUpEnabled: false,
        signInAliases: { email: true },
        autoVerify: { email: true },
        passwordPolicy: {
          minLength: 8,
          requireUppercase: true,
          requireDigits: true,
          requireSymbols: false,
        },
        // DESTROY: when COGNITO_ENABLED=false the pool and all users are deleted.
        // A dormant pool with credentials is a security liability.
        // Re-enabling Cognito creates a fresh pool — re-invite users.
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      new cognito.CfnUserPoolGroup(this, 'OperatorsGroup', {
        userPoolId: userPool.userPoolId,
        groupName: 'operators',
        description: 'Full access: dashboard + chat + patching',
      });
      new cognito.CfnUserPoolGroup(this, 'ViewersGroup', {
        userPoolId: userPool.userPoolId,
        groupName: 'viewers',
        description: 'Read-only: dashboard only, no chat or patching',
      });

      const userPoolDomain = userPool.addDomain('CognitoDomain', {
        cognitoDomain: { domainPrefix },
      });

      // ALB DNS names are mixed-case (e.g., Patchy-UIALB-xYz...) but browsers
      // lowercase them. Cognito compares callback URLs case-sensitively.
      // We register the mixed-case URL here (only option at synth time since
      // CloudFormation has no lowercase intrinsic), then deploy.sh runs a
      // post-deploy fixup to add the lowercase variant via CLI.
      const albDns = alb.loadBalancerDnsName;

      const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
        userPool,
        generateSecret: true,
        oAuth: {
          flows: { authorizationCodeGrant: true },
          scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
          callbackUrls: [
            `https://${albDns}/oauth2/idpresponse`,
          ],
          logoutUrls: [
            `https://${albDns}`,
          ],
        },
        supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      });

      const httpsListener = alb.addListener('HTTPSListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(props.certificateArn)],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        defaultAction: new elbv2_actions.AuthenticateCognitoAction({
          userPool,
          userPoolClient,
          userPoolDomain,
          scope: 'openid email profile',
          next: elbv2.ListenerAction.forward([targetGroup]),
        }),
      });

      httpsListener.addAction('HealthBypass', {
        priority: 1,
        conditions: [elbv2.ListenerCondition.pathPatterns(['/api/health'])],
        action: elbv2.ListenerAction.forward([targetGroup]),
      });

      alb.addListener('HTTPRedirect', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS', port: '443', permanent: true,
        }),
      });

      new cdk.CfnOutput(this, 'CognitoUserPoolId', {
        value: userPool.userPoolId,
        description: 'Cognito User Pool ID — create users here',
      });
      new cdk.CfnOutput(this, 'CognitoDomainPrefix', {
        value: domainPrefix,
        description: 'Cognito hosted UI domain prefix',
      });

    } else if (props.certificateArn) {
      // HTTPS without Cognito auth enforcement (internal ALB + SSM tunnel)
      const httpsListener = alb.addListener('HTTPSListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(props.certificateArn)],
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
      });
      httpsListener.addTargetGroups('UITarget', { targetGroups: [targetGroup] });

      alb.addListener('HTTPRedirect', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS', port: '443', permanent: true,
        }),
      });

    } else {
      // HTTP only (no cert)
      const listener = alb.addListener('HTTPListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
      });
      listener.addTargetGroups('UITarget', { targetGroups: [targetGroup] });
    }

    this.albDnsName = alb.loadBalancerDnsName;

    // ── Bastion instance (internal ALB mode only) ──────────────────
    // Dedicated t3.nano for SSM port forwarding — no customer instances used.
    if (!cognitoEnabled) {
      const bastionRole = new iam.Role(this, 'BastionRole', {
        assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        ],
      });

      const bastion = new ec2.Instance(this, 'Bastion', {
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.NANO),
        machineImage: ec2.MachineImage.latestAmazonLinux2023(),
        role: bastionRole,
      });
      cdk.Tags.of(bastion).add('Name', 'patchy-bastion');
      cdk.Tags.of(bastion).add('ManagedBy', 'IntelligentPatchAutomation');

      new cdk.CfnOutput(this, 'BastionInstanceId', {
        value: bastion.instanceId,
        description: 'Bastion instance for SSM port forwarding to internal ALB',
      });
    }

    const protocol = props.certificateArn ? 'https' : 'http';
    new cdk.CfnOutput(this, 'UIUrl', {
      value: `${protocol}://${alb.loadBalancerDnsName}`,
      description: cognitoEnabled ? 'Public URL (Cognito-protected)' : 'Internal ALB URL (access via SSM port forwarding)',
    });
    new cdk.CfnOutput(this, 'UILogGroupName', {
      value: logGroup.logGroupName,
      description: 'CloudWatch log group for UI container',
    });
  }
}
