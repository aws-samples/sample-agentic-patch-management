# Extending the Solution — Intelligent Patch Automation

The tool-based architecture is built to be extended. Adding a new capability is usually a single tool function — no changes to routing or decision logic.

---

## What the solution covers today

- Vulnerability discovery — scan the fleet for CVEs, weigh severity, and pin down which environments and instances are affected
- Fleet-wide impact assessment — see how far a vulnerability spreads across dev, staging, and prod
- Patch impact preview — see exactly which packages will change before you commit, including ones beyond the target CVE
- SLA-driven decisions — EMERGENCY vs SCHEDULED, worked out automatically from per-instance SLA tags
- Patch policy awareness — detect existing SSM Patch Policies and defer to them for scheduled operations
- Severity-scoped execution — apply patches at a specific severity level using BaselineOverride files
- Application health verification — check SSM connectivity and CloudWatch alarms after patching
- Automated rollback — reverse patches if health checks fail, with verification
- Compliance reporting — structured audit reports with before/after delta, SLA assessment, and operator identity
- Cross-account, cross-region operations — fan out patching across accounts and regions via SSM Automation TargetLocations
- Real-time operations console — a dashboard with fleet status, vulnerabilities, compliance metrics, and audit trail

---

## What you can extend

| Capability | What it adds | How to integrate |
|-----------|-------------|-----------------|
| CMDB-driven blast radius | Show the application dependencies affected by patching | Add a tool that queries your CMDB (ServiceNow, Device42) for instance dependencies. The agent presents the dependency map before the operator confirms. |
| Change management integration | Check your change calendar before patching | Add a tool that queries your change management system (ServiceNow, Jira). The agent checks both SSM windows and your change calendar. |
| Third-party vulnerability scanners | Use Qualys, Tenable, or Rapid7 alongside Inspector | Add a tool that queries your scanner's API. The agent uses it next to the Inspector findings. |
| Multi-OS patching | Support Ubuntu, RHEL, Windows | Create baseline override JSON files per OS and adapt the rollback commands for apt/zypper/Windows Update. |
| Slack/Teams notifications | Get pinged when patching completes or fails | POST to `/api/chat` from a Lambda triggered by SNS. The agent's structured output maps to Slack message blocks. |
| Per-CVE surgical patching | Install only the specific fix package | Use SSM's `InstallOverrideList` parameter with a CVE → package name mapping from Inspector's `vulnerablePackages` field. |
| Cross-account patching | Manage instances across multiple AWS accounts | Built in. Set `MULTI_ACCOUNT_ENABLED=true` in `.env` and deploy spoke roles + SSM documents via `./deploy.sh spoke && ./deploy.sh docs`. See [Multi-Account Setup](../README.md#multi-account-setup). |
| Container image patching (ECS/EKS) | Scan and remediate vulnerabilities in container images | See [Container Workloads](#container-workloads) below. |

Each of these is a single tool function added to the agent's tool list — routing, memory, and UI stay put.

---

## Adding a new tool

### 1. Write the tool function

Add it to the right domain module in `agent/helper/tools/` (`vulnerability_tools.py` for scanner tools, `patch_tools.py` for patching, `maintenance_tools.py` for scheduling, and so on):

```python
@tool
def my_new_tool(param1: str, param2: Optional[int] = None) -> dict:
    """Description of what this tool does.

    Args:
        param1: What param1 is
        param2: What param2 is (optional)

    Returns:
        dict: {'result': ..., 'count': ...}
    """
    try:
        logger.info(f"[TOOL:my_new_tool] param1={param1} param2={param2}")

        # ... your logic here ...

        logger.info(f"[TOOL:my_new_tool] RESULT: key_metric={value}")
        return {"result": value, "count": count}
    except ClientError as e:
        error_info = classify_error(e)
        logger.error(f"[TOOL:my_new_tool] ERROR: {error_info}")
        return error_info
```

### 2. Register the tool

Import it and add it to the tool list in `supervisor.py`:

```python
from helper.tools import my_new_tool

# In the tools list:
tools = [
    ...existing tools...,
    my_new_tool,
]
```

### 3. Update the system prompt (only if you need to)

If the tool's usage is counter-intuitive, add a `Decision:` line to its docstring. Only touch the system prompt if the structural hints aren't enough.

### 4. Add a response template (optional)

If the tool's output needs a specific presentation, add a template to `agent/config/response_templates.yaml`:

```yaml
my_new_result:
  description: "Results from my new tool"
  agent: unified
  structure: |
    STATUS_LINE: {emoji} My result -- {count} items found
    TABLE:
    | Column A | Column B |
```

---

## Multi-OS support

### Where things stand

Patching uses `AWS-RunPatchBaseline`, which supports all OS types. Rollback uses `yum history undo`, which is Amazon Linux 2 / RHEL only.

### Adding Ubuntu/Debian support

1. Baseline overrides: create override JSON files with `"OperatingSystem": "UBUNTU"` in `setup_baseline_overrides.py`.
2. Rollback: add an apt-based rollback script in `rollback_patches()`:
   ```bash
   # Detect OS
   if command -v apt-get &>/dev/null; then
       # apt-based rollback
       apt-get install --reinstall -y $(dpkg --get-selections | grep install | awk '{print $1}')
   elif command -v yum &>/dev/null; then
       # yum-based rollback (existing)
       LAST_TRANSACTION=$(yum history list | grep -E '^[[:space:]]*[0-9]+' | head -1 | awk '{print $1}')
       yum history undo $LAST_TRANSACTION -y
   fi
   ```
3. OS detection: read `platform_type` from `ssm:DescribeInstanceInformation` to pick the package manager before rollback.

### Adding Windows support

1. Baseline overrides: create override JSON files with `"OperatingSystem": "WINDOWS"`.
2. Patch execution: use `AWS-RunPatchBaseline` (already works for Windows via SSM).
3. Rollback: use `AWS-RunPowerShellScript` with `wusa.exe /uninstall` or System Restore.
4. Health checks: adapt the CloudWatch alarm patterns for Windows services.

---

## Container vulnerability support

To include container image vulnerabilities alongside EC2:

1. Set `INSPECTOR_RESOURCE_TYPES=EC2,ECR` in `.env`.
2. Redeploy: `./deploy.sh agent` and `./deploy.sh ui`.

The agent and dashboard will now include ECR container image findings alongside the EC2 instance findings. Inspector has to be enabled for ECR scanning in your account. Both the agent and dashboard filter findings by resource type based on this setting.

---

## Container workloads

Container patching is a fundamentally different model from EC2 in-place patching. You don't SSH into a running container — you rebuild the image and redeploy.

### How it differs from EC2

| Aspect | EC2 (current) | Containers (ECS/EKS) |
|--------|--------------|---------------------|
| Discovery | SSM Explorer → EC2 instances | Inspector → ECR image findings |
| Vulnerability scan | Inspector EC2 scanning | Inspector ECR scanning |
| Remediation | In-place patch via SSM `RunPatchBaseline` | Rebuild the image with an updated base layer → push to ECR → redeploy the service |
| Rollback | `yum history undo` on the instance | Roll back to the previous ECS task definition or EKS deployment revision |

### What already works

- Vulnerability scanning: set `INSPECTOR_RESOURCE_TYPES=EC2,ECR` in `.env`. Inspector scans ECR images and the agent reports container CVEs alongside the EC2 findings.
- Fleet discovery: ECS on EC2 and EKS managed node groups run on EC2 instances, so they show up in SSM Explorer. The host-level patching workflow applies to those hosts.

### What needs new tools

For Fargate workloads (no host access) or image-level remediation, add these tools:

| Tool | Purpose | Integration |
|------|---------|------------|
| `scan_ecr_images` | List vulnerable images and their services | Query Inspector with `resourceType=ECR`, correlate with ECS task definitions |
| `trigger_image_rebuild` | Rebuild the image with a patched base layer | Invoke a CodeBuild project or CodePipeline. Pass the target ECR repo and tag. |
| `update_ecs_service` | Deploy the rebuilt image | Call `ecs:UpdateService` with the new task definition pointing at the patched image |
| `rollback_ecs_deployment` | Revert to the previous image | Call `ecs:UpdateService` with the previous task definition revision |

The routing and memory architecture don't change — these are just additional tools in the agent's list.

---

## Delegated administrator deployment

By default, the [Multi-Account Setup](../README.md#multi-account-setup) runs from the Organizations management account. Plenty of teams would rather not run workloads there, so here's how to deploy from a delegated administrator (DA) account instead.

### How it works

Registering a DA for [Quick Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-delegated-administrator.html) automatically grants DA status for:

- CloudFormation StackSets — deploy spoke roles via SERVICE_MANAGED StackSets
- SSM Explorer — create org-wide Resource Data Syncs and query `GetOpsSummary`

[Inspector DA](https://docs.aws.amazon.com/inspector/latest/user/designating-admin.html) is registered separately and gives you org-wide vulnerability visibility.

The one capability that can't be delegated is [Quick Setup Patch Policies](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) — those have to be created from the management account. But the solution doesn't create Patch Policies; it detects existing ones and defers to them.

### What runs where

| Task | Account | Frequency |
|------|---------|-----------|
| Register DA for Quick Setup | Management | Once |
| Register DA for Inspector | Management | Once |
| Create Patch Policies (if using) | Management | As needed |
| Quick Setup (DHMC, Host Management, Config Recording) | DA | Once |
| SSM Explorer (enable + Resource Data Sync) | DA | Once |
| Inspector (activate scanning for member accounts) | DA | Once |
| Deploy the solution (`./deploy.sh`) | DA | As needed |
| Deploy spoke roles (`./deploy.sh spoke`) | DA | As needed |
| Operate the agent and dashboard | DA | Continuously |

### Step-by-step setup

Phase 1 — management account (one-time, ~10 min)

These steps need an administrator in the Organizations management account.

1. Register the DA for Quick Setup:
   ```bash
   # From the management account
   aws ssm-quicksetup register-delegated-administrator \
     --delegated-admin-account-id <DA_ACCOUNT_ID>
   ```
   This automatically registers the DA for CloudFormation StackSets and SSM Explorer too.

2. Register the DA for Inspector:
   ```bash
   aws inspector2 enable-delegated-admin-account \
     --delegated-admin-account-id <DA_ACCOUNT_ID>
   ```

3. Create Patch Policies (optional — only if you want org-wide scheduled patching):
   Console → Systems Manager → Quick Setup → Patch Policy → Create. This is the only Quick Setup type that requires the management account.

Phase 2 — DA account (setup + deploy, ~45 min)

Everything from here runs in the DA account.

4. Quick Setup — DHMC, Host Management, Config Recording:
   Console → Systems Manager → Quick Setup → Library. Create all three targeting the whole org (same steps as [Prerequisites — Step 2](../README.md#aws-account-setup)).

5. SSM Explorer:
   Console → Systems Manager → Explorer → Enable Explorer. `deploy.sh` creates the Resource Data Sync for you.

6. Inspector — activate scanning:
   Console → Inspector → Member accounts → Select all → Enable scanning.

7. Configure `.env`:
   ```bash
   cp .env.example .env
   # Set AWS_PROFILE to the DA account profile
   AWS_PROFILE=da-account
   AWS_REGION=us-east-1
   MULTI_ACCOUNT_ENABLED=true
   SPOKE_EXECUTION_ROLE=PatchySpokeRole
   AWS_ORG_ID=o-xxxxxxxxxx
   SPOKE_OU_IDS=ou-abc123,ou-def456
   ```

8. Deploy:
   ```bash
   ./deploy.sh
   ./sample-env.sh deploy
   ```

### Limitations and edge cases

| Limitation | Detail | Workaround |
|-----------|--------|------------|
| Patch Policies | Can't be created from a DA account ([docs](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-delegated-administrator.html)) | Create them from the management account. The agent detects and defers to them. |
| Opt-in regions | A DA can't create Resource Data Syncs in [opt-in regions](https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html#regions-opt-in-status) (Milan, Cape Town, Bahrain, Hong Kong) | Use the management account to create syncs for opt-in regions. |
| One DA per service | Only one account can be DA for Quick Setup, one for Explorer, one for Inspector | Coordinate with other teams using these DA roles. Registering DA for Quick Setup auto-registers Explorer + StackSets. |
| DA sync visibility | Resource Data Syncs created by a DA are only visible in the DA account | Not a problem — the solution runs in the DA account. The management account can't see DA-created syncs. |

### How `deploy.sh` handles DA

`deploy.sh deploy_spoke()` auto-detects whether the caller is the management account or a registered DA:

1. Calls `organizations describe-organization` to get the management account ID.
2. If the caller isn't the management account, checks `organizations list-delegated-administrators --service-principal ssm.amazonaws.com`.
3. If the caller is a registered DA, sets `--call-as DELEGATED_ADMIN` on all SERVICE_MANAGED StackSet API calls.
4. If it's neither the management account nor a DA, fails with the registration command.

No manual flags needed — `./deploy.sh` and `./deploy.sh spoke` work from either account type.

Why nothing else needs to change:

| Component | Why it works from a DA account |
|-----------|---------------------|
| `infra/bin/app.ts` | `hubAccountId` is passed as CDK context — set to the DA account automatically |
| `infra/lib/spoke-role-stack.ts` | The spoke role trusts the hub account root, which allows both runtime roles and deployment operations |
| `infra/lib/core-stack.ts` | The S3 bucket policy uses `aws:PrincipalOrgID` — org-level, not account-specific |
| `agent/helper/tools/_shared.py` | Fleet discovery uses `GetOpsSummary` with `SyncName` — a DA can create and query syncs |
| `agent/helper/cross_account.py` | `get_hub_account_id()` returns the caller's account (the DA account). Spoke roles trust this account. |
| `ui/api/server.py` | The dashboard queries Explorer from the DA account. Spoke session assume-role works because the spoke role trusts the DA account. |
| `deploy.sh ensure_explorer_sync()` | A DA can create `SyncFromSource` with the `AwsOrganizations` source type |
| `deploy.sh deploy_agent()` | AgentCore deploys to whichever account runs the CLI — the DA account |
| `deploy.sh deploy_infra()` | CDK deploys to the caller's account — the DA account |
| `deploy.sh deploy_ui()` | CDK deploys Fargate + ALB to the caller's account — the DA account |

> Note on spoke role trust: the default trust policy lets the entire hub account root (`arn:aws:iam::<hub>:root`) assume `PatchySpokeRole`. That's intentional for the sample/demo — it lets `./sample-env.sh deploy` push instances to spoke accounts via StackSet without extra setup. For production, see [Security — Cross-account trust model](security.md#cross-account-trust-model-patchyspokerole) to restrict the trust to specific role ARN patterns.

---

## Integration patterns

The solution exposes an HTTP API with SSE streaming, so it slots into existing tools:

- Slack / Microsoft Teams: POST to `/api/chat` with the message body, consume the SSE stream, render it in a channel. The agent's structured next-steps output maps to Slack interactive buttons.
- ServiceNow / Jira: trigger patch operations from incident tickets. On ticket creation with a CVE tag, call the API to assess impact; on approval, execute; write the compliance report URL back to the ticket.
- SNS notifications: after a long-running patch operation, publish a completion event to SNS. Subscribe Slack webhooks, email, or PagerDuty.
- Custom dashboards: the `/api/dashboard` endpoint returns all the fleet status, vulnerability, and compliance data as JSON — drop it into Grafana, Datadog, or an internal portal.
