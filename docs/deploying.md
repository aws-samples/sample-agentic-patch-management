# Deploying Patchy

How to deploy Patchy into one account or across an organisation, what each setting does, how to update and upgrade, and how to remove it. For using Patchy once it's running, see [Operations](operations.md).

- [Prerequisites](#prerequisites)
- [Deploy into one account](#deploy-into-one-account)
- [Multi-account and multi-Region](#multi-account-and-multi-region)
- [Updating a deployment](#updating-a-deployment)
- [Configuration reference](#configuration-reference)
- [Upgrading from the October 2026 release](#upgrading-from-the-october-2026-release)
- [Removing Patchy](#removing-patchy)

## Prerequisites

On the machine you deploy from:

- Python 3.11 or later
- Node.js 20 or later
- AgentCore CLI 0.20.1 (`@aws/agentcore`). `deploy.sh` installs it if it's missing, and stops if a different version is installed globally, because another version's built-in CDK toolkit may not read this release's templates
- Docker or [Finch](https://github.com/runfinch/finch), to build the UI image
- AWS CLI v2, and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) if you use the internal load balancer

In the hub account and Region:

1. [Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access-modify.html) to Claude Sonnet 5. The agent uses the `us.anthropic.claude-sonnet-5` inference profile unless you set `BEDROCK_MODEL_ID`.
2. Systems Manager Quick Setup: [Default Host Management Configuration](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-default-host-management-configuration.html), [Host Management](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-host-management.html) and [Config recording](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-config.html), so every instance runs the SSM Agent and reports its inventory.
3. [SSM Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-setup.html). The dashboard and the agent list the fleet from it. After AWS Config is first turned on, instances can take up to six hours to appear.
4. [Amazon Inspector](https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html) scanning for EC2.
5. A way to reach the UI, one of:
   - an ACM certificate that browsers trust (`ACM_CERTIFICATE_ARN`), for an internet-facing load balancer with Cognito sign-in;
   - `ALLOW_SELF_SIGNED_CERT=true`, which generates a self-signed certificate for a demo (browsers warn);
   - `COGNITO_ENABLED=false`, for an internal load balancer you reach through a bastion with `./connect-ui.sh`.

Then tag the instances Patchy may manage. `PatchAutomation=enabled` puts an instance in scope; without it Patchy can't see or patch the instance. `Environment=dev`, `staging` or `prod` sets its rollout rules. The full rules, including SLA tags, are in [Operations: tags and SLAs](operations.md#tags-and-slas).

## Deploy into one account

```bash
cp .env.example .env
# Set AWS_PROFILE and AWS_REGION, and one of ACM_CERTIFICATE_ARN,
# ALLOW_SELF_SIGNED_CERT=true or COGNITO_ENABLED=false.
./deploy.sh
./deploy.sh create-user
./deploy.sh status
```

The first deploy takes 25 to 30 minutes. Before changing anything, every command prints the profile, account and Region it will use. If the shell's `AWS_PROFILE` differs from the one in `.env`, the shell wins and the script asks you to type the account ID to confirm.

`./deploy.sh` runs these steps in order:

1. Creates a Python virtual environment and bootstraps the CDK in `AWS_REGION`.
2. Deploys the agent with the AgentCore CLI: the runtime, its memory and its IAM role. The runtime's settings from `.env` are written into `agent/agentcore/agentcore.json` for the deploy, and the file is restored afterwards.
3. Deploys `Patchy-Network` (skipped when `EXISTING_VPC_ID` is set) and `Patchy-Core`, the compliance bucket and the runtime role's policy. It then creates the `patchy-fleet-sync` Resource Data Sync for Explorer.
4. Uploads the severity baseline override files to the compliance bucket.
5. Deploys the Patchy roles (`Patchy-SpokeIam`) and SSM documents (`Patchy-SsmDocs`) into the hub as plain stacks, the documents in each Region of `SPOKE_REGIONS`. Every run uses SSM Automation with target locations, even in a single account, so the hub needs both. This bootstraps the CDK in each of those Regions too.
6. Deploys `Patchy-UI`: the frontend and API image, the Fargate service, the load balancer, Cognito, and the report Lambda.

If the certificate is missing when Cognito is on, the deploy stops at step 6. Steps 1 to 5 are already in place by then, so fix `.env` and run `./deploy.sh` again.

`./deploy.sh create-user` asks for an email address, a temporary password of 12 characters or more, a role (`operator`, the default, or `viewer`), and whether the user is also an auditor, who sees every operator's plans read-only. The user sets a new password at first sign-in. `./deploy.sh status` prints the UI address, along with the state of every stack, the runtime, the Explorer sync and Inspector.

To try Patchy without touching your own instances, `./sample-env.sh deploy` creates five tagged instances: two in dev, one in staging and two in prod, with maintenance windows, patch baselines and CloudWatch alarms. `./sample-env.sh destroy` removes them.

## Multi-account and multi-Region

In multi-account mode the hub drives every change through [SSM Automation with target locations](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html). Each spoke account holds only:

- `PatchySpokeRole`, which the runtime role alone can assume, to scan, patch and roll back;
- `PatchySpokeReadRole`, which only the UI and the report Lambda can assume, to read instances, findings and run results;
- the Patchy SSM documents, in each Region of `SPOKE_REGIONS`.

### Before you start

- The hub must be the organisation's management account or a delegated administrator for Systems Manager. The deploy checks this and prints the registration command if it isn't.
- [Trusted access](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-orgs-activate-trusted-access.html) for CloudFormation StackSets, and Quick Setup targeting the accounts and OUs you will patch.
- The hub must be [Amazon Inspector's delegated administrator](https://docs.aws.amazon.com/inspector/latest/user/designating-admin.html). Patchy reads findings once per Region with the hub's credentials, so without this, spoke findings don't appear in the dashboard, in CVE plans or in forecasts. The deploy warns when the hub isn't, and `./deploy.sh status` shows the state per Region.
- The role on each spoke instance must be able to read the hub bucket's override files. Instances download them during every patch run: `baseline-overrides/` for a severity plan, `install-overrides/` for a CVE plan. The bucket policy allows readers from the organisation, but a cross-account read also needs the instance's own role to allow it. Add this to the instance profile role, or to the Default Host Management role for instances without one:

  ```json
  {
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": [
      "arn:aws:s3:::patch-compliance-reports-<hub account>/baseline-overrides/*",
      "arn:aws:s3:::patch-compliance-reports-<hub account>/install-overrides/*"
    ]
  }
  ```

  Without it, patch runs fail on spoke instances with a 403 from S3 (see [Troubleshooting](troubleshooting.md#run-problems)). The sample fleet's role already has it.

### Configure

```bash
MULTI_ACCOUNT_ENABLED=true
AWS_ORG_ID=o-xxxxxxxxxx          # required
SPOKE_REGIONS=us-east-1,eu-west-1

# One of:
SPOKE_ACCOUNT_IDS=111111111111,222222222222   # include the hub
SPOKE_OU_IDS=ou-abc1-23456789                 # nested OUs included; accounts that join later are enrolled
```

Plans can only target accounts and Regions in this scope. With multi-account on and neither list set, nothing is in scope. Run `./deploy.sh`.

### What the deploy adds

| Resource | How | Where |
|---|---|---|
| `Patchy-SpokeIam` | StackSet | Every target account, in `AWS_REGION` (IAM is global) |
| `Patchy-SsmDocs` | StackSet | Every target account, in each of `SPOKE_REGIONS` |
| `patchy-fleet-sync` | Resource Data Sync | Hub, covering the organisation and `SPOKE_REGIONS` |

From the management account, an account list gives self-managed StackSets. From a delegated administrator, or with `SPOKE_OU_IDS`, the StackSets are service-managed and target OUs. When the OU StackSets don't reach the hub (for example, because it's the management account), the deploy adds the hub's roles and documents as plain stacks.

`PatchySpokeRole` trusts the runtime role's exact ARN, and `PatchySpokeReadRole` trusts the exact ARNs of the UI task role and the report Lambda role. On a first deploy those UI roles don't exist yet when the spoke stacks go out. The read role starts on a name pattern instead, with a warning, and the deploy updates it again once the UI is up. Until that second pass finishes, the dashboard shows only the hub.

If neither target list is set, `./deploy.sh spoke` asks for account IDs or OU IDs, or discovers every active account in the organisation, the hub included.

### Adding accounts and Regions

- With `SPOKE_OU_IDS`, an account that joins a target OU gets the roles and documents automatically.
- With `SPOKE_OU_IDS`, Patchy reads the OUs' member accounts from AWS Organizations, at most every 15 minutes. If that read fails, for example when Organizations throttles, it keeps using the accounts it last read within the past hour, and findings and forecasts say the scope couldn't be resolved. With no recent read, only the hub is in scope until the next successful one.
- With `SPOKE_ACCOUNT_IDS`, add the account and run `./deploy.sh`.
- For a new Region, add it to `SPOKE_REGIONS` and run `./deploy.sh`.

The deploy creates every missing pair of account (or OU) and Region and updates the rest. Removing an account or Region from `.env` doesn't delete its stacks; remove them from the StackSet yourself.

### From a delegated administrator

If you don't run workloads in the management account, deploy from a delegated administrator account instead. In the management account, once:

```bash
aws ssm-quicksetup register-delegated-administrator --delegated-admin-account-id <DA_ACCOUNT_ID>
aws inspector2 enable-delegated-admin-account --delegated-admin-account-id <DA_ACCOUNT_ID>
```

The first command also makes the account a delegated administrator for CloudFormation StackSets and SSM Explorer. Then, in the delegated administrator account, set up Quick Setup (Default Host Management, Host Management, Config recording) for the organisation, turn on Explorer, and enable Inspector scanning for member accounts. Finally, set `SPOKE_OU_IDS` in `.env` and run `./deploy.sh`.

Three limits apply. Quick Setup patch policies can only be created from the management account. A delegated administrator can't create a Resource Data Sync in an opt-in Region. Each service allows only one delegated administrator.

## Updating a deployment

| Command | What it updates | Use it after changing |
|---|---|---|
| `./deploy.sh` | Everything | IAM, infrastructure, multi-account settings, scope tags, a new release |
| `./deploy.sh agent` | Runs the tool-selection eval, then the agent and `Patchy-UI` | Agent code, prompt or tools, and any setting marked "agent" below |
| `./deploy.sh ui` | `Patchy-UI` only | The UI and API, and any setting marked "ui" below |
| `./deploy.sh spoke` | Spoke roles (the StackSet). In one account, the hub's role and documents stacks. | New accounts, a replaced runtime or UI role |
| `./deploy.sh docs` | Patchy SSM documents (the StackSet). In one account, as `spoke`. | Document changes, new Regions |
| `./deploy.sh status` | Nothing | Checking what's deployed |

`./deploy.sh agent` stops if the eval scores below `EVAL_THRESHOLD`; see [the eval](../agent/eval/README.md).

Some changes need a second command:

- If a UI deploy replaces the UI task role or the report Lambda role, run `./deploy.sh spoke` so the spoke read role trusts the new ARNs. Until then, the dashboard and reports read only the hub.
- If the agent is redeployed into a new runtime role, run `./deploy.sh` so `Patchy-Core` attaches to it and the spoke role trusts it.
- To renew the self-signed demo certificate (valid for a year), remove `ACM_CERTIFICATE_ARN` from `.env` and run `./deploy.sh ui` with `ALLOW_SELF_SIGNED_CERT=true`.

If `./deploy.sh ui` reports that the task definition didn't change after a code change, run `rm -rf infra/cdk.out && ./deploy.sh ui`.

## Configuration reference

Every setting lives in `.env`. `.env.example` lists the common ones, and the tables below are complete. The "Applied by" column names the command that puts a change into effect: `./deploy.sh agent`, `./deploy.sh ui`, `full` for `./deploy.sh`, or `sample` for `./sample-env.sh deploy`. Settings without a value in `.env` take the default shown.

The runtime never reads `.env`. It gets only the settings `deploy.sh` writes into its configuration, and they are marked "agent" below. The UI container gets its settings from the `Patchy-UI` stack.

### Account, Region and network

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `AWS_PROFILE` | `default` | all | Profile to deploy with. A value exported in the shell wins over `.env`. |
| `AWS_REGION` | `us-east-1` | full | Hub Region. |
| `PATCHY_CONFIRM_ACCOUNT` | unset | all | Account ID that confirms an unattended deploy when the shell's profile differs from `.env`. Any other value stops the run. |
| `PATCHY_CONFIRM_REGION` | unset | all | Region that confirms an unattended deploy when the shell's `AWS_REGION` differs from `.env`. Any other value stops the run. |
| `EXISTING_VPC_ID` | unset | full | Use this VPC instead of creating `Patchy-Network`. It needs private subnets with NAT for Fargate. |
| `CDK_DOCKER` | Docker, else Finch | all | Container CLI the CDK builds the UI image with. |
| `AGENTCORE_ROLE_ARN` | found | full | The runtime role. The deploy finds it from the AgentCore CLI's state and stops if it finds none or several; set it only to choose. |

### Agent

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-5` | agent | Model or inference profile. |
| `THINKING_EFFORT` | `medium` | agent | `low`, `medium` or `high`, for Claude Sonnet 5 and 6. Other models ignore it. |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | unset | agent | Bedrock guardrail applied to each message. Both are needed. See [Operations: Bedrock Guardrails](operations.md#bedrock-guardrails). |
| `MAX_FLEET_SIZE` | `5000` | agent | Most instances the agent reads per fleet refresh. An environment-wide plan is refused (`FleetViewTruncated`) when the fleet is bigger. |
| `RUNTIME_AUTH` | `iam` | agent | `jwt` makes the runtime accept only the signed-in user's Cognito token and check their group itself. Needs Cognito and an existing `Patchy-UI` stack. See [Security](security.md#runtime-authorisation). |
| `PATCHY_DEBUG_LOGS` | `false` | agent | `true` logs full prompts, tool inputs and outputs at DEBUG. For diagnosis only: conversation content goes to CloudWatch. |

### Scope and change controls

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `SSM_SCOPE_TAG_KEY`, `SSM_SCOPE_TAG_VALUE` | `PatchAutomation`, `enabled` | full | The tag that puts an instance in scope. The agent, the dashboard and the spoke role's `SendCommand` permission all require it. |
| `REPORT_TAGS` | `Team,Product` | agent | Instance tags recorded in each run's report, comma-separated, up to 10. Reports and the CSV export show them, and the SLA summary is grouped by the first. Instances in a run with different values record `mixed` and the list of values. See [Operations: report tags](operations.md#report-tags). |
| `INSPECTOR_RESOURCE_TYPES` | `EC2` | agent | Inspector finding types to read: `EC2`, `ECR`, `LAMBDA`. |
| `CHANGE_CALENDAR` | unset | agent | SSM Change Calendar name or ARN. While it's closed, or can't be read, no plan is made and no approved plan starts. Patchy checks it when the plan is made and when the run starts, not during the run: a run that started while the calendar was open carries on after it closes. To stop a run when a freeze starts, press Stop run on it. |
| `ABORT_ALARMS` | unset | agent | One CloudWatch alarm that stops a run, which must exist in every target account and Region. SSM watches one alarm per run, so the deploy and planning refuse a list; combine several in a composite alarm. See [Operations: stop alarms](operations.md#stop-alarms). |
| `REQUIRE_STAGED_ROLLOUT` | `false` | agent | `true` refuses a staging plan without a verified dev run, and a prod plan without a verified staging run, for the same CVE or severity in the last 14 days. Otherwise the plan only warns. |
| `REBOOT_POLICY` | none | agent | Environments whose installs may reboot, such as `dev=RebootIfNeeded,staging=RebootIfNeeded`. Values are `NoReboot` or `RebootIfNeeded`. Unlisted environments don't reboot. |

### Sign-in and access

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `ACM_CERTIFICATE_ARN` | unset | ui | Certificate for the HTTPS listener. With Cognito, a self-signed certificate is refused unless `ALLOW_SELF_SIGNED_CERT=true`. |
| `ALLOW_SELF_SIGNED_CERT` | `false` | ui | Generate and accept a self-signed certificate, for demos. |
| `COGNITO_ENABLED` | `true` | ui | `false` gives an internal load balancer reached through a bastion. Switching an existing deployment to `false` deletes the user pool and every user. |
| `COGNITO_DOMAIN_PREFIX` | `patchy-<account-id>` | ui | Prefix for the hosted sign-in domain. It must be globally unique, and the deploy saves it to `.env`. |
| `COGNITO_MFA` | `off` | ui | `optional` lets users add an authenticator app. `required` makes it mandatory and works only on a new user pool. |
| `PUBLIC_URL` | the load balancer's address | ui | The address users browse to, for a custom domain. It becomes the sign-out target and, when it starts with `https://`, a Cognito callback URL. |
| `REQUIRE_SEPARATE_APPROVER` | `false` | ui | The operator who asked for a plan can't approve it. Needs Cognito; without it every approval returns 503. |
| `DEFAULT_ROLE` | `viewer` | ui | Role for every request when there's neither Cognito nor an API key. `operator` gives everyone who reaches the UI full control; use it only for a trusted pilot on the internal load balancer. |
| `API_KEY_OPERATOR`, `API_KEY_VIEWER`, `API_KEY` | unset | ui | API keys for `COGNITO_ENABLED=false`. `API_KEY` is an operator key. The deploy stores the keys in the Secrets Manager secret `patchy/ui-api-keys` and passes its name and version to the stack (`API_KEYS_SECRET_NAME`, `API_KEYS_SECRET_VERSION`); the browser asks for a key once per tab. Ignored when Cognito is on. |

### Multi-account

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `MULTI_ACCOUNT_ENABLED` | `false` | full | Turn on cross-account operation. |
| `AWS_ORG_ID` | unset | full | Required in multi-account mode. Cross-account role assumptions and reads of the override files are limited to this organisation. |
| `SPOKE_ACCOUNT_IDS` | unset | full | Target accounts, comma-separated. Include the hub. |
| `SPOKE_OU_IDS` | unset | full | Target OUs, nested OUs included. Takes precedence over `SPOKE_ACCOUNT_IDS`. |
| `SPOKE_REGIONS` | `AWS_REGION` | full | Regions to patch in. |
| `SPOKE_EXECUTION_ROLE`, `SPOKE_READ_ROLE` | `PatchySpokeRole`, `PatchySpokeReadRole` | full | Names of the two spoke roles. |
| `SAMPLE_ENV_ACCOUNTS` | first of `SPOKE_ACCOUNT_IDS` | sample | Accounts that `./sample-env.sh deploy` puts the sample instances in. |
| `SAMPLE_AMI_ID` | Amazon Linux AMI for the Region | sample | AMI for the sample instances. |

### Notifications, logs and traces

| Setting | Default | Applied by | What it does |
|---|---|---|---|
| `NOTIFY_TOPIC` | `false` | ui | Create an SNS topic that receives each run's outcome and the UI alarms (no running task, unhealthy target, memory above 80%, report Lambda errors). The topic ARN is the `RunNotificationTopicArn` output of `Patchy-UI`. Without it the alarms still exist, with no actions. |
| `NOTIFY_EMAIL` | unset | ui | Email address subscribed to the topic. SNS asks the recipient to confirm. |
| `NOTIFY_KMS` | `false` | ui | Encrypt the topic and the UI and report Lambda log groups with a customer managed key. |
| `ENABLE_RUNTIME_LOGS` | `false` | agent | Add a structured record per invocation to a vended log group. See [Troubleshooting: logs](troubleshooting.md#logs-and-traces). |
| `ENABLE_TRACING` | `false` | agent | Turn on CloudWatch Transaction Search and trace delivery for the GenAI Observability console. |

### Deploy script

| Setting | Default | What it does |
|---|---|---|
| `SKIP_EVAL` | `false` | Skip the eval in `./deploy.sh agent`. |
| `EVAL_THRESHOLD` | `80` | Minimum eval score, in percent. |
| `EVAL_MODEL_ID` | `BEDROCK_MODEL_ID` | Model the eval uses. |
| `AGENTCORE_CLI_VERSION` | `0.20.1` | `@aws/agentcore` CLI version to install. |
| `STACKSET_WAIT` | `true` | Wait for each StackSet operation to finish. |
| `STACKSET_REGION_CONCURRENCY` | unbounded | Regions a StackSet operation deploys to at once. |
| `STACKSET_FAILURE_TOLERANCE` | `0` | Percentage of stack instances allowed to fail. |
| `DESTROY_SPOKE_STACKSET`, `DESTROY_FLEET_SYNC` | `false` | Let `./deploy.sh destroy` also remove the StackSets, or the Resource Data Sync. |

`AGENT_NAME`, `PROJECT_NAME` and `AGENTCORE_TARGET` exist for the AgentCore CLI's project names; leave them at their defaults.

## Upgrading from the October 2026 release

These steps move a deployment of the public release (12 October 2026) to this version. The public release confirmed a change when the operator typed "yes" in the chat. This version replaces that with stored plans that an operator approves in the UI, which changes the runtime's permissions, the spoke roles, the SSM documents and the UI.

1. Let running patch runs finish. Nothing carries over from a conversation in progress.
2. Compare `.env` with `.env.example`:
   - With Cognito on, a self-signed certificate now needs `ALLOW_SELF_SIGNED_CERT=true`. A missing certificate stops the deploy instead of generating one.
   - Remove VITE_API_KEY and `BYPASS_TOOL_CONSENT` if present. The browser now asks for an API key instead of having it built into the page.
   - In multi-account mode, an empty account and OU list now means nothing is in scope, where it used to mean every account. Include the hub in `SPOKE_ACCOUNT_IDS`.
   - New settings that change nothing unless you set them: `CHANGE_CALENDAR`, `ABORT_ALARMS`, `REQUIRE_STAGED_ROLLOUT`, `REBOOT_POLICY`, `REQUIRE_SEPARATE_APPROVER`, `RUNTIME_AUTH` (default `iam`), `COGNITO_MFA`, `NOTIFY_TOPIC`, `NOTIFY_EMAIL`, `NOTIFY_KMS`, `PUBLIC_URL`. `THINKING_EFFORT` sets the model's thinking effort, `medium` by default.
3. Run the full deploy, `./deploy.sh`, not `./deploy.sh agent`. In a single account, this adds the hub's Patchy roles and SSM documents, without which the public release couldn't patch in one account.
4. In multi-account mode, check that the spoke read role trusts exact role ARNs:

   ```bash
   aws iam get-role --role-name PatchySpokeReadRole --profile <spoke-profile> \
     --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
   ```

   It should show `ArnEquals` with two role ARNs. If it shows `ArnLike` with `Patchy-UI-*`, run `./deploy.sh spoke`.
5. Ask users to reload open browser tabs, and to set a new password of at least 12 characters the next time they change it.
6. Sign in, ask for a patch plan for one dev instance, and check that the change record opens. Cancel it, or approve it on a test instance and follow the run.

What changes for the people using it:

- Every patch, rollback and maintenance window request becomes a plan. Nothing runs until an operator clicks Approve and run in its change record, and typing "yes" runs nothing. A plan expires after 15 minutes and runs once.
- A CVE request installs only that CVE's OS packages. A severity request installs approved patches at that level and above.
- Each environment has fixed rollout limits that a request can't change, and the run uses the limits stored in its plan.
- Untagged instances count as prod. An instance's environment comes from its tag, not from the request.
- Patchy creates maintenance windows with a patch task, and waits for a window when one meets the SLA.
- The Change Calendar, a stop alarm, staged rollout and a separate approver can be turned on.
- Stop run in the run view cancels every account and Region of the plan. The agent itself can't stop runs.
- Rollback undoes only the recorded Patchy run's package transactions, and is refused when there is no recorded run.
- Verification checks each instance against Inspector and says why a finding is still open. Promotion to the next environment waits until every location of the plan has finished.
- Forecasts list the findings that scheduled patching will fix too late.
- The report Lambda writes the compliance report when a run ends, and can notify an SNS topic.
- Reports record the instances' `Team` and `Product` tags, or the tags in `REPORT_TAGS`. The public release recorded `unknown` for both.
- The UI is new: chat on the left, and the change record, run, overview, findings and reports on the right, in light or dark.
- Sign-in sessions last 12 hours instead of 7 days.

For the IAM changes, see [Security: IAM by principal](security.md#iam-by-principal).

## Removing Patchy

```bash
./sample-env.sh destroy   # first, if the sample fleet exists
./deploy.sh destroy
```

`./deploy.sh destroy` refuses to run while the sample stack exists, because the sample instances use the Patchy network. After you type `yes` it:

1. Destroys `Patchy-UI` and `Patchy-Core`. This deletes the compliance bucket with every report, and the user pool with every user. `Patchy-Core` goes first because its policy is attached to the runtime role, which can't be deleted while it is.
2. Removes the AgentCore runtime and memory.
3. Destroys `Patchy-Network`.
4. In a single account, destroys the hub's `Patchy-SpokeIam` and `Patchy-SsmDocs` stacks.

Some resources stay by default:

- In multi-account mode, the StackSets stay unless `DESTROY_SPOKE_STACKSET=true`. The hub's plain stacks, if the deploy added them, stay too, and go with the StackSets.
- The `patchy-fleet-sync` Resource Data Sync stays unless `DESTROY_FLEET_SYNC=true`. Re-creating it starts an ingestion that takes hours.
- The `patchy/ui-api-keys` secret and the CDK bootstrap stacks in each Region stay. Delete them yourself if you don't need them.

`./deploy.sh destroy --spoke-only` and `--docs-only` remove just the spoke role StackSet or the documents StackSet, and the hub's plain stack of the same name if the deploy added one. Those StackSets exist only in multi-account mode. A StackSet destroy waits for each Region's instance delete and stops at the first one that fails, leaving the StackSet in place and the destroy exiting with an error; fix the cause and run it again.
