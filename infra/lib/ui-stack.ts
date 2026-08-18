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
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // ── Docker image ───────────────────────────────────────────────
    // Frontend is pre-built on the host by deploy.sh (avoids esbuild crash
    // under Finch/x86 emulation on Apple Silicon). The Dockerfile just copies
    // the pre-built dist/ into the image.
    // extraHash forces CDK to compute a new asset hash every deploy,
    // ensuring the Docker image is always rebuilt with latest source code.
    const image = new ecr_assets.DockerImageAsset(this, 'UIImage', {
      directory: path.join(__dirname, '..', '..'),
      file: 'Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,
      exclude: ['infra/node_modules', 'infra/cdk.out', 'venv', '.git', 'node_modules'],
      extraHash: new Date().toISOString(),
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
        'ssm:DescribeInstanceInformation', 'ssm:DescribeInstancePatchStates',
        'ssm:GetOpsSummary',
        'ssm:ListResourceDataSync',
        // Reconciliation: read automation status + child command outcomes to
        // populate success_count, failure_count, instance_count on the final
        // compliance report.
        'ssm:GetAutomationExecution', 'ssm:DescribeAutomationExecutions',
        'ssm:ListCommandInvocations', 'ssm:ListCommands',
        'inspector2:ListFindings',
        'sts:GetCallerIdentity',
        // Needed when AGENTCORE_AGENT_ARN env var isn't set and the UI falls back
        // to reading the runtime ARN from the AgentCore CFN stack outputs.
        'cloudformation:DescribeStacks',
      ],
      resources: ['*'],
    }));
    const complianceBucketArn = `arn:aws:s3:::patch-compliance-reports-${accountId}`;
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:ListBucket', 's3:HeadBucket', 's3:PutObject', 's3:DeleteObject'],
      resources: [complianceBucketArn, `${complianceBucketArn}/*`],
    }));
    // bedrock-agentcore:InvokeAgentRuntime — scoped to runtime resources in
    // this account/region. agentCoreRoleArn is required at deploy time;
    // deploy.sh resolves it automatically (from agentcore CLI deployed-state,
    // CloudFormation outputs, or IAM role list) and passes it via CDK context.
    //
    // When the prop is missing (e.g., during `cdk synth` of a sibling stack
    // without context, or before the agent has been deployed), we synthesize
    // with a deny-all sentinel resource so the IAM policy never widens to '*'.
    // The deployment is functionally broken (InvokeAgentRuntime calls fail)
    // but the security property holds, and this stack can co-exist with
    // sibling stacks that don't need the agent ARN (e.g., Patchy-SampleEnv).
    if (!props.agentCoreRoleArn) {
      console.warn(
        '[Patchy-UI] agentCoreRoleArn not provided — InvokeAgentRuntime ' +
        'will be scoped to a deny-all sentinel ARN. Deploy via ./deploy.sh ' +
        '(which resolves the ARN automatically) or pass `-c agentCoreRoleArn=<arn>`.'
      );
    }
    const invokeRuntimeResource = props.agentCoreRoleArn
      ? `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:runtime/*`
      : `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:runtime/__patchy_unresolved_agent_arn__`;
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [invokeRuntimeResource],
    }));
    // AgentCore Memory read access — used by /api/session/{id}/messages
    // to rehydrate chat history on page refresh. ListEvents is the
    // underlying API that MemoryClient.get_last_k_turns calls.
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:GetEvent',
      ],
      resources: [`arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:memory/*`],
    }));
    // Cross-account: assume spoke role for dashboard queries. Only added when
    // MULTI_ACCOUNT_ENABLED=true. AWS_ORG_ID is mandatory in that case so the
    // assume-role permission is scoped to the organization (defense-in-depth
    // matching the BBR cross-account hardening rules).
    if (process.env.MULTI_ACCOUNT_ENABLED === 'true') {
      if (!process.env.AWS_ORG_ID) {
        throw new Error(
          'AWS_ORG_ID is required when MULTI_ACCOUNT_ENABLED=true. ' +
          'Set it in .env so the UI task role assume-role policy is ' +
          'scoped to your organization. Get it via: ' +
          "aws organizations describe-organization --query 'Organization.Id' --output text"
        );
      }
      const spokeRole = process.env.SPOKE_EXECUTION_ROLE || 'PatchySpokeRole';
      taskRole.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: [`arn:aws:iam::*:role/${spokeRole}`],
        conditions: {
          StringEquals: { 'aws:PrincipalOrgID': process.env.AWS_ORG_ID },
        },
      }));
      taskRole.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'organizations:ListAccounts',
          // Required for SPOKE_OU_IDS-based scope resolution
          // (_resolve_ou_member_accounts → _get_configured_scope_accounts).
          // Without this, OU-based filtering silently falls through and the
          // dashboard would surface mgmt-account findings/instances.
          'organizations:ListAccountsForParent',
        ],
        // AWS Organizations APIs do not accept resource ARN scoping — they
        // require Resource: '*' by AWS service design.
        resources: ['*'],
      }));
    }

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
    if (process.env.DEFAULT_ROLE) containerEnv['DEFAULT_ROLE'] = process.env.DEFAULT_ROLE;
    // Agent runtime ARN — primary mechanism for the UI to find the agent.
    // deploy.sh exports AGENTCORE_AGENT_ARN after deploy_agent() resolves it
    // from CloudFormation outputs / deployed-state.json. Without this var the
    // UI falls back to scanning the bundled deployed-state.json, which the
    // current @aws/agentcore CLI version doesn't always populate with the
    // runtime ARN (only memories), causing 500s on /api/chat.
    if (process.env.AGENTCORE_AGENT_ARN) {
      containerEnv['AGENTCORE_AGENT_ARN'] = process.env.AGENTCORE_AGENT_ARN;
    }
    // Memory ID for chat rehydration. deploy.sh exports this after
    // deploy_agent resolves it from agentcore.json / deployed-state.
    if (process.env.MEMORY_PATCHMEMORYV2_ID) {
      containerEnv['MEMORY_PATCHMEMORYV2_ID'] = process.env.MEMORY_PATCHMEMORYV2_ID;
    }
    // Multi-account dashboard support
    if (process.env.MULTI_ACCOUNT_ENABLED === 'true') {
      containerEnv['MULTI_ACCOUNT_ENABLED'] = 'true';
      if (process.env.SPOKE_ACCOUNT_IDS) containerEnv['SPOKE_ACCOUNT_IDS'] = process.env.SPOKE_ACCOUNT_IDS;
      if (process.env.SPOKE_OU_IDS) containerEnv['SPOKE_OU_IDS'] = process.env.SPOKE_OU_IDS;
      if (process.env.SPOKE_EXECUTION_ROLE) containerEnv['SPOKE_EXECUTION_ROLE'] = process.env.SPOKE_EXECUTION_ROLE;
      if (process.env.SPOKE_REGIONS) containerEnv['SPOKE_REGIONS'] = process.env.SPOKE_REGIONS;
    }

    const uiContainer = taskDef.addContainer('ui', {
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
      // Select one subnet per AZ — ALBs reject multiple subnets in the same AZ
      vpcSubnets: {
        subnetType: cognitoEnabled
          ? cdk.aws_ec2.SubnetType.PUBLIC
          : cdk.aws_ec2.SubnetType.PRIVATE_WITH_EGRESS,
        onePerAz: true,
      },
    });
    alb.setAttribute('idle_timeout.timeout_seconds', '300');

    // ── Fargate service ────────────────────────────────────────────
    const service = new ecs.FargateService(this, 'UIService', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      minHealthyPercent: 100,
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
      // Enable managed login v2 (modern UI)
      const cfnDomain = userPoolDomain.node.defaultChild as cognito.CfnUserPoolDomain;
      cfnDomain.addPropertyOverride('ManagedLoginVersion', 2);

      // ALB DNS names are mixed-case (e.g., Patchy-UIALB-xYz...) but browsers
      // lowercase them. Cognito compares callback URLs case-sensitively.
      // We register the mixed-case URL here (only option at synth time since
      // CloudFormation has no lowercase intrinsic), then deploy.sh runs a
      // post-deploy fixup to add the lowercase variant via CLI.
      const albDns = alb.loadBalancerDnsName;

      const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
        userPool,
        generateSecret: true,
        authFlows: {
          userSrp: true,
        },
        oAuth: {
          flows: { authorizationCodeGrant: true },
          scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
          callbackUrls: [
            `https://${albDns}/oauth2/idpresponse`,
          ],
          logoutUrls: [
            `https://${albDns}`,
            `https://${albDns}/signed-out`,
          ],
        },
        supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      });

      // Enable managed login branding (v2 UI)
      new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
        userPoolId: userPool.userPoolId,
        clientId: userPoolClient.userPoolClientId,
        useCognitoProvidedValues: true,
      });

      // The API verifies the access token's cognito:groups claim against the
      // pool's JWKS — that claim decides operator vs viewer, and the ALB does
      // not sign the access token. Added here rather than in containerEnv
      // because the pool is created after the task definition.
      uiContainer.addEnvironment('COGNITO_USER_POOL_ID', userPool.userPoolId);
      uiContainer.addEnvironment('COGNITO_CLIENT_ID', userPoolClient.userPoolClientId);

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

      // Signed-out page must be unauthenticated per AWS docs:
      // "Client logout landing pages are unauthenticated. This means that they
      // cannot be behind an Application Load Balancer rule that requires authentication."
      httpsListener.addAction('SignedOutBypass', {
        priority: 2,
        conditions: [elbv2.ListenerCondition.pathPatterns(['/signed-out'])],
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
