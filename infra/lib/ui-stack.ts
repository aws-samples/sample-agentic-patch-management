import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as elbv2_actions from 'aws-cdk-lib/aws-elasticloadbalancingv2-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as events_targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subs from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';
import { assumeSpokeRoleStatement, requireOrgId } from './common';
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
    // The build context is only the files the Dockerfile copies, so the asset
    // hash (and the image) changes when they do and not when docs or infra do.
    const image = new ecr_assets.DockerImageAsset(this, 'UIImage', {
      directory: path.join(__dirname, '..', '..'),
      file: 'Dockerfile',
      platform: ecr_assets.Platform.LINUX_AMD64,
      exclude: ['**', '!Dockerfile', '!ui/api/requirements.lock', '!ui/api/server.py', '!ui/api/reconcile.py',
                '!ui/frontend/dist/**'],
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
        'ssm:DescribeAutomationStepExecutions',
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
      actions: ['s3:GetObject', 's3:ListBucket', 's3:PutObject'],
      resources: [complianceBucketArn, `${complianceBucketArn}/*`],
    }));
    // The reconciler deletes a pending context once its report is written;
    // nothing else in the bucket may be deleted by the UI.
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:DeleteObject'],
      resources: [`${complianceBucketArn}/pending-reports/*`],
    }));
    // Plans, run markers, run ownership records and CVE install lists are
    // written only by the agent, and baseline overrides (which can set yum
    // repositories on every patched instance) only by the deploy machine.
    // Denying them here means a compromised UI can approve existing plans but
    // can't invent one, claim another conversation's run, change what a run
    // installs, or fake a verified stage.
    taskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'NoPlanWrites',
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject', 's3:DeleteObject*'],
      resources: ['plans', 'plan-runs', 'run-executions', 'plan-executions', 'install-overrides', 'promotions', 'baseline-overrides',
                  'session-owners', 'run-locks']
        .map((prefix) => `${complianceBucketArn}/${prefix}/*`),
    }));
    // Final reports (YYYY/MM/DD/<execution>.json) are written once by the
    // reconciler, so neither the UI nor the report Lambda can replace one.
    const createOnlyReports = (sid: string) => new iam.PolicyStatement({
      sid,
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject'],
      resources: [`${complianceBucketArn}/2*`],
      conditions: { Null: { 's3:if-none-match': 'true' } },
    });
    taskRole.addToPolicy(createOnlyReports('CreateOnlyReports'));
    // Pending contexts are the runtime's; the UI and the Lambda only delete them.
    const noPendingWrites = (sid: string) => new iam.PolicyStatement({
      sid,
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject'],
      resources: [`${complianceBucketArn}/pending-reports/*`],
    });
    taskRole.addToPolicy(noPendingWrites('NoPendingReportWrites'));
    // A decision (approve or cancel) is written once; the first one wins.
    taskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CreateOnlyDecisions',
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject'],
      resources: ['plan-decisions'].map((prefix) => `${complianceBucketArn}/${prefix}/*`),
      conditions: { Null: { 's3:if-none-match': 'true' } },
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
    // Only Patchy's runtime and its endpoints (deploy.sh exports
    // AGENTCORE_AGENT_ARN). Without it, the same deny-all sentinel: never
    // every runtime in the account.
    const agentArn = props.agentCoreRoleArn ? process.env.AGENTCORE_AGENT_ARN : undefined;
    if (props.agentCoreRoleArn && !agentArn) {
      console.warn(
        '[Patchy-UI] AGENTCORE_AGENT_ARN not set — InvokeAgentRuntime will be scoped to a ' +
        'deny-all sentinel ARN. Deploy via ./deploy.sh, which resolves it.'
      );
    }
    const invokeRuntimeResources = agentArn
      ? [agentArn, `${agentArn}/*`]
      : [`arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${accountId}:runtime/__patchy_unresolved_agent_arn__`];
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: invokeRuntimeResources,
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
      requireOrgId('the UI task role assume-role policy');
      // The dashboard only reads from spokes, so it assumes the read role.
      // The write role (PatchySpokeRole) doesn't trust this task role.
      taskRole.addToPolicy(assumeSpokeRoleStatement(process.env.SPOKE_READ_ROLE || 'PatchySpokeReadRole',
                                                    'the UI task role assume-role policy'));
      taskRole.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'organizations:ListAccounts',
          // Required for SPOKE_OU_IDS-based scope resolution
          // (_resolve_ou_member_accounts → _get_configured_scope_accounts).
          // Without this, OU-based filtering silently falls through and the
          // dashboard would surface mgmt-account findings/instances.
          'organizations:ListAccountsForParent',
          'organizations:ListOrganizationalUnitsForParent',
        ],
        // AWS Organizations APIs do not accept resource ARN scoping — they
        // require Resource: '*' by AWS service design.
        resources: ['*'],
      }));
    }

    // ── Log group ──────────────────────────────────────────────────
    // NOTIFY_KMS=true encrypts the run-outcome topic and the log groups with a
    // customer managed key (rotated yearly); otherwise AWS-owned keys are used.
    const dataKey = process.env.NOTIFY_KMS === 'true' ? new kms.Key(this, 'UIDataKey', {
      description: 'Patchy UI: run notifications and logs',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    }) : undefined;
    // CloudWatch Logs may use the key only for this stack's two log groups (the
    // report Lambda's has a generated name under the stack name).
    dataKey?.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'LogsUseForPatchyUiLogGroups',
      principals: [new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
      actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
      resources: ['*'],
      conditions: {
        ArnLike: {
          'kms:EncryptionContext:aws:logs:arn': [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/patch-automation/ui`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:${this.stackName}-ReportFnLogs*`,
          ],
        },
      },
    }));

    const logGroup = new logs.LogGroup(this, 'UILogGroup', {
      encryptionKey: dataKey,
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
    // Scope, Inspector, approval and report-tag settings must match the agent's, or
    // the dashboard counts a different fleet than the agent acts on.
    for (const name of ['SSM_SCOPE_TAG_KEY', 'SSM_SCOPE_TAG_VALUE', 'INSPECTOR_RESOURCE_TYPES',
                        'REQUIRE_SEPARATE_APPROVER', 'RUNTIME_AUTH', 'REPORT_TAGS']) {
      if (process.env[name]) containerEnv[name] = process.env[name]!;
    }
    // Multi-account dashboard support
    if (process.env.MULTI_ACCOUNT_ENABLED === 'true') {
      containerEnv['MULTI_ACCOUNT_ENABLED'] = 'true';
      if (process.env.SPOKE_ACCOUNT_IDS) containerEnv['SPOKE_ACCOUNT_IDS'] = process.env.SPOKE_ACCOUNT_IDS;
      if (process.env.SPOKE_OU_IDS) containerEnv['SPOKE_OU_IDS'] = process.env.SPOKE_OU_IDS;
      if (process.env.SPOKE_READ_ROLE) containerEnv['SPOKE_READ_ROLE'] = process.env.SPOKE_READ_ROLE;
      if (process.env.SPOKE_REGIONS) containerEnv['SPOKE_REGIONS'] = process.env.SPOKE_REGIONS;
    }

    // The report Lambda below writes compliance reports, so the dashboard only lists running runs.
    containerEnv['REPORT_LAMBDA_ENABLED'] = 'true';
    containerEnv['PYTHONDONTWRITEBYTECODE'] = '1';
    // API keys (internal ALB mode) come from the secret deploy.sh writes; the
    // version ID changes with the keys, so the service restarts to pick them up.
    const containerSecrets: Record<string, ecs.Secret> = {};
    if (process.env.API_KEYS_SECRET_NAME) {
      const apiKeys = secretsmanager.Secret.fromSecretNameV2(this, 'UIApiKeys', process.env.API_KEYS_SECRET_NAME);
      for (const name of ['API_KEY', 'API_KEY_OPERATOR', 'API_KEY_VIEWER']) {
        containerSecrets[name] = ecs.Secret.fromSecretsManager(apiKeys, name);
      }
      if (process.env.API_KEYS_SECRET_VERSION) containerEnv['API_KEYS_VERSION'] = process.env.API_KEYS_SECRET_VERSION;
    }
    const uiContainer = taskDef.addContainer('ui', {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      portMappings: [{ containerPort: 8000 }],
      environment: containerEnv,
      secrets: containerSecrets,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'ui' }),
      // Nothing in the image is written at run time; /tmp is a task volume.
      readonlyRootFilesystem: true,
      healthCheck: {
        command: ['CMD', 'python', '-c',
                  "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    taskDef.addVolume({ name: 'tmp' });
    uiContainer.addMountPoints({ containerPath: '/tmp', sourceVolume: 'tmp', readOnly: false });

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
      // A task that never becomes healthy fails the deploy and rolls back to
      // the last working task definition, instead of retrying for hours.
      circuitBreaker: { enable: true, rollback: true },
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
          minLength: 12,
          requireUppercase: true,
          requireDigits: true,
          requireSymbols: false,
        },
        // COGNITO_MFA: off (default), optional, or required. Required can only
        // be set when the pool is created; existing pools can go to optional.
        mfa: ({ optional: cognito.Mfa.OPTIONAL, required: cognito.Mfa.REQUIRED } as Record<string, cognito.Mfa>)[
          (process.env.COGNITO_MFA || 'off').toLowerCase()] ?? cognito.Mfa.OFF,
        mfaSecondFactor: { otp: true, sms: false },
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
      // Adds to either role: a read-only list of every operator's plans (GET /api/changes).
      new cognito.CfnUserPoolGroup(this, 'AuditorsGroup', {
        userPoolId: userPool.userPoolId,
        groupName: 'auditors',
        description: "Read-only list of every operator's plans; approving stays with each plan's conversation",
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
      // A custom domain (PUBLIC_URL) signs in and out through its own host, so
      // Cognito must accept it as well as the ALB hostname.
      const publicUrl = (process.env.PUBLIC_URL || '').replace(/\/+$/, '');
      const publicUrls = publicUrl.startsWith('https://') ? [publicUrl] : [];

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
            ...publicUrls.map((u) => `${u}/oauth2/idpresponse`),
          ],
          logoutUrls: [
            `https://${albDns}`,
            `https://${albDns}/signed-out`,
            ...publicUrls.flatMap((u) => [u, `${u}/signed-out`]),
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
          // Signed-in sessions last a working day, not the ALB's 7-day default.
          sessionTimeout: cdk.Duration.hours(12),
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
        open: false,  // only the bastion (below) reaches an internal ALB with no sign-in
      });
      httpsListener.addTargetGroups('UITarget', { targetGroups: [targetGroup] });

      alb.addListener('HTTPRedirect', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        open: false,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS', port: '443', permanent: true,
        }),
      });

    } else {
      // HTTP only (no cert)
      const listener = alb.addListener('HTTPListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        open: false,  // only the bastion (below) reaches an internal ALB with no sign-in
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
        requireImdsv2: true,
      });
      // The internal ALB's listeners are closed; the bastion is the way in.
      alb.connections.allowFrom(bastion, ec2.Port.tcp(props.certificateArn ? 443 : 80), 'Bastion to UI');
      if (props.certificateArn) alb.connections.allowFrom(bastion, ec2.Port.tcp(80), 'Bastion to HTTP redirect');
      cdk.Tags.of(bastion).add('Name', 'patchy-bastion');
      cdk.Tags.of(bastion).add('ManagedBy', 'IntelligentPatchAutomation');

      new cdk.CfnOutput(this, 'BastionInstanceId', {
        value: bastion.instanceId,
        description: 'Bastion instance for SSM port forwarding to internal ALB',
      });
    }

    const protocol = props.certificateArn ? 'https' : 'http';
    // The server checks that ALB tokens were signed for this load balancer, and
    // builds the logout redirect from the configured URL, not the Host header.
    uiContainer.addEnvironment('ALB_ARN', alb.loadBalancerArn);
    uiContainer.addEnvironment('PUBLIC_URL', process.env.PUBLIC_URL || `${protocol}://${alb.loadBalancerDnsName}`);

    // ── Reports and notifications when a run finishes ──────────────
    // SSM emits a status-change event when an Automation execution ends. The
    // report Lambda runs the dashboard's reconciliation (ui/api/reconcile.py),
    // so reports are written without waiting for a dashboard load, and with
    // NOTIFY_TOPIC=true publishes each approved run's outcome to SNS. The spoke
    // read role trusts its exact role ARN (ReportFnRoleArn output); it gets the
    // reconciler's reads and nothing that changes a run.
    const reportFn = new lambda.Function(this, 'ReportFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'report_handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'ui', 'api'), {
        exclude: ['**', '!reconcile.py', '!report_handler.py'],
      }),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      description: 'Writes Patchy compliance reports when runs finish',
      environment: {
        UI_URL: `${protocol}://${alb.loadBalancerDnsName}`,
        ...(process.env.SPOKE_READ_ROLE ? { SPOKE_READ_ROLE: process.env.SPOKE_READ_ROLE } : {}),
      },
      logGroup: new logs.LogGroup(this, 'ReportFnLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        encryptionKey: dataKey,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    reportFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetAutomationExecution', 'ssm:DescribeAutomationExecutions',
                'ssm:DescribeAutomationStepExecutions', 'ssm:ListCommandInvocations', 'ssm:ListCommands',
                'ssm:DescribeInstancePatchStates', 'sts:GetCallerIdentity'],
      resources: ['*'],
    }));
    reportFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket', 's3:PutObject'],
      resources: [complianceBucketArn, `${complianceBucketArn}/*`],
    }));
    reportFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:DeleteObject'],
      resources: [`${complianceBucketArn}/pending-reports/*`],
    }));
    reportFn.addToRolePolicy(createOnlyReports('CreateOnlyReports'));
    reportFn.addToRolePolicy(noPendingWrites('NoPendingReportWrites'));
    reportFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'NoPlanOrApprovalWrites',
      effect: iam.Effect.DENY,
      actions: ['s3:PutObject', 's3:DeleteObject*'],
      resources: ['plans', 'plan-runs', 'run-executions', 'plan-executions', 'plan-decisions',
                  'install-overrides', 'promotions', 'baseline-overrides', 'session-owners', 'run-locks']
        .map((prefix) => `${complianceBucketArn}/${prefix}/*`),
    }));
    if (process.env.MULTI_ACCOUNT_ENABLED === 'true') {
      reportFn.addToRolePolicy(assumeSpokeRoleStatement(process.env.SPOKE_READ_ROLE || 'PatchySpokeReadRole',
                                                        'the report Lambda assume-role policy'));
    }
    new events.Rule(this, 'RunFinishedRule', {
      description: 'Patchy runbook executions that reached a final state',
      eventPattern: {
        source: ['aws.ssm'],
        detailType: ['EC2 Automation Execution Status-change Notification'],
        detail: {
          Definition: [{ prefix: 'Patchy-' }],
          Status: ['Success', 'Failed', 'TimedOut', 'Cancelled', 'CompletedWithFailure'],
        },
      },
      targets: [new events_targets.LambdaFunction(reportFn, { retryAttempts: 2 })],
    });
    // A run whose final event arrived before its pending context existed is
    // reported by this sweep instead of waiting for another run to finish. So
    // is a stopped run, whose report waits for the commands it had already
    // sent: their end raises no Automation event, and spoke commands raise
    // theirs in the spoke account. Every five minutes keeps that wait short.
    new events.Rule(this, 'ReportSweepRule', {
      description: 'Patchy compliance report sweep',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new events_targets.LambdaFunction(reportFn, { retryAttempts: 2 })],
    });
    let topic: sns.Topic | undefined;
    if (process.env.NOTIFY_TOPIC === 'true') {
      topic = new sns.Topic(this, 'RunNotifications', {
        displayName: 'Patchy run outcomes',
        enforceSSL: true,
        masterKey: dataKey,
      });
      if (process.env.NOTIFY_EMAIL) {
        topic.addSubscription(new sns_subs.EmailSubscription(process.env.NOTIFY_EMAIL));
      }
      topic.grantPublish(reportFn);
      reportFn.addEnvironment('NOTIFY_TOPIC_ARN', topic.topicArn);
      new cdk.CfnOutput(this, 'RunNotificationTopicArn', {
        value: topic.topicArn,
        description: 'SNS topic for run outcomes; subscribe email, or Slack/Teams through AWS Chatbot',
      });
    }

    // ── Alarms ─────────────────────────────────────────────────────
    // With NOTIFY_TOPIC=true they go to the run-outcome topic; otherwise they
    // have no actions and show in the CloudWatch console only.
    const alarms = [
      // Container Insights metric; no data means no running task.
      new cloudwatch.Alarm(this, 'UINoRunningTaskAlarm', {
        alarmDescription: 'Patchy UI has no running task',
        metric: new cloudwatch.Metric({
          namespace: 'ECS/ContainerInsights',
          metricName: 'RunningTaskCount',
          dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName },
          statistic: 'Minimum',
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 3,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      }),
      new cloudwatch.Alarm(this, 'UIUnhealthyHostAlarm', {
        alarmDescription: 'Patchy UI target is failing its health check',
        metric: targetGroup.metrics.unhealthyHostCount({ statistic: 'Maximum', period: cdk.Duration.minutes(1) }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 3,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
      new cloudwatch.Alarm(this, 'UIMemoryAlarm', {
        alarmDescription: 'Patchy UI memory above 80%',
        metric: service.metricMemoryUtilization({ statistic: 'Maximum', period: cdk.Duration.minutes(5) }),
        threshold: 80,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 2,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
      new cloudwatch.Alarm(this, 'ReportFnErrorAlarm', {
        alarmDescription: 'Patchy report Lambda failed',
        metric: reportFn.metricErrors({ statistic: 'Sum', period: cdk.Duration.minutes(15) }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
    ];
    if (topic) {
      const action = new cloudwatch_actions.SnsAction(topic);
      alarms.forEach((alarm) => alarm.addAlarmAction(action));
    }
    // CloudWatch publishes alarms to an encrypted topic only if the key lets it.
    if (topic && dataKey) {
      dataKey.addToResourcePolicy(new iam.PolicyStatement({
        sid: 'CloudWatchAlarmsPublish',
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['kms:Decrypt', 'kms:GenerateDataKey*'],
        resources: ['*'],
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }));
    }

    // deploy.sh reads these to check the service after a deploy.
    new cdk.CfnOutput(this, 'UIClusterName', {
      value: cluster.clusterName,
      description: 'ECS cluster running the UI',
    });
    new cdk.CfnOutput(this, 'UIServiceName', {
      value: service.serviceName,
      description: 'ECS service running the UI',
    });

    // deploy.sh passes these to the spoke read role's trust policy, so it
    // trusts exactly the dashboard and the report Lambda.
    new cdk.CfnOutput(this, 'TaskRoleArn', {
      value: taskRole.roleArn,
      description: 'UI task role, trusted by the spoke read role',
    });
    new cdk.CfnOutput(this, 'ReportFnRoleArn', {
      value: reportFn.role!.roleArn,
      description: 'Report Lambda role, trusted by the spoke read role',
    });
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
