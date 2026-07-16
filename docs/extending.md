# Extending the Solution — Intelligent Patch Automation

The agent is designed for extensibility. Adding a new capability is a single tool function with no changes to routing or decision logic.

---

## What the Solution Covers Today

- **Vulnerability discovery** -- scan your fleet for CVEs, assess severity, and identify affected environments and instances
- **Fleet-wide impact assessment** -- understand how widespread a vulnerability is across dev, staging, and prod
- **Patch impact preview** -- see exactly which packages will be updated before committing, including packages beyond the target CVE
- **SLA-driven decision making** -- automatically determine EMERGENCY vs SCHEDULED based on per-instance SLA tags
- **Patch policy awareness** -- detect existing SSM Patch Policies and respect them for scheduled operations
- **Severity-scoped execution** -- apply patches at a specific severity level using BaselineOverride files
- **Application health verification** -- check SSM connectivity and CloudWatch alarms after patching
- **Automated rollback** -- reverse patches if health checks fail, with verification
- **Compliance reporting** -- structured audit reports with before/after delta, SLA assessment, operator identity
- **Cross-session memory** -- remembers previous patching outcomes, CVE history, and operator preferences
- **Real-time operations console** -- dashboard with fleet status, vulnerabilities, compliance metrics, audit trail

---

## What Customers Can Extend

| Capability | What it adds | How to integrate |
|-----------|-------------|-----------------|
| **CMDB-driven blast radius** | Show application dependencies affected by patching | Add a tool that queries your CMDB (ServiceNow, Device42) for instance dependencies. The agent presents the dependency map before the operator confirms. |
| **Change management integration** | Check your change calendar before patching | Add a tool that queries your change management system (ServiceNow, Jira). The agent checks both SSM windows and your change calendar. |
| **Third-party vulnerability scanners** | Use Qualys, Tenable, or Rapid7 alongside Inspector | Add a tool that queries your scanner's API. The vulnerability analyst uses it alongside Inspector findings. |
| **Multi-OS patching** | Support Ubuntu, RHEL, Windows | Create baseline override JSON files per OS. Adapt rollback commands for apt/zypper/Windows Update. |
| **Slack/Teams notifications** | Get notified when patching completes or fails | POST to `/api/chat` from a Lambda triggered by SNS. The agent's structured output maps to Slack message blocks. |
| **Per-CVE surgical patching** | Install only the specific fix package | Use SSM's `InstallOverrideList` parameter with CVE -> package name mapping from Inspector's `vulnerablePackages` field. |
| **Cross-account patching** | Manage instances across multiple AWS accounts | Built-in. Set `MULTI_ACCOUNT_ENABLED=true` in `.env` and deploy spoke roles via `./deploy.sh spoke`. See [Multi-Account Setup](../README.md#multi-account-setup). |
| **Container image patching (ECS/EKS)** | Scan and remediate vulnerabilities in container images | See [Container Workloads](#container-workloads) below. |

Each extension is a single tool function — no changes to memory, routing, or UI required.

---

## Adding a New Tool

### 1. Write the tool function

Add to the appropriate domain module in `agent/helper/tools/` (e.g., `vulnerability_tools.py` for scanner tools, `patch_tools.py` for patching, `maintenance_tools.py` for scheduling):

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

Import and add to the tool list in `supervisor.py`:

```python
from helper.tools import my_new_tool

# In the tools list:
tools = [
    ...existing tools...,
    my_new_tool,
]
```

### 3. Update the system prompt (if needed)

If the tool's usage is counter-intuitive, add a `Decision:` line to the tool's docstring. Only add to the system prompt if structural hints aren't sufficient.

### 4. Add a response template (optional)

If the tool's output needs a specific presentation format, add a template to `agent/config/response_templates.yaml`:

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

## Multi-OS Support

### Current State

Patching uses `AWS-RunPatchBaseline` which supports all OS types. Rollback uses `yum history undo` which is Amazon Linux 2 / RHEL only.

### Adding Ubuntu/Debian Support

1. **Baseline overrides**: Create override JSON files with `"OperatingSystem": "UBUNTU"` in `setup_baseline_overrides.py`
2. **Rollback**: Add an apt-based rollback script in `rollback_patches()`:
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
3. **OS detection**: Query `platform_type` from `ssm:DescribeInstanceInformation` to determine the package manager before rollback

### Adding Windows Support

1. **Baseline overrides**: Create override JSON files with `"OperatingSystem": "WINDOWS"`
2. **Patch execution**: Use `AWS-RunPatchBaseline` (already works for Windows via SSM)
3. **Rollback**: Use `AWS-RunPowerShellScript` with `wusa.exe /uninstall` or System Restore
4. **Health checks**: Adapt CloudWatch alarm patterns for Windows services

---

## Container Vulnerability Support

To include container image vulnerabilities alongside EC2:

1. Set `INSPECTOR_RESOURCE_TYPES=EC2,ECR` in `.env`
2. Redeploy: `./deploy.sh agent` and `./deploy.sh ui`

The agent and dashboard will now include ECR container image findings alongside EC2 instance findings. Inspector must be enabled for ECR scanning in your account.

The agent and dashboard filter findings by resource type based on this setting.

---

## Container Workloads

Container patching is a fundamentally different model from EC2 in-place patching. You don't SSH into a running container — you rebuild the image and redeploy.

### How it differs from EC2

| Aspect | EC2 (current) | Containers (ECS/EKS) |
|--------|--------------|---------------------|
| Discovery | SSM Explorer → EC2 instances | Inspector → ECR image findings |
| Vulnerability scan | Inspector EC2 scanning | Inspector ECR scanning |
| Remediation | In-place patch via SSM `RunPatchBaseline` | Rebuild image with updated base layer → push to ECR → redeploy service |
| Rollback | `yum history undo` on the instance | Roll back to previous ECS task definition or EKS deployment revision |

### What already works

- **Vulnerability scanning**: Set `INSPECTOR_RESOURCE_TYPES=EC2,ECR` in `.env`. Inspector scans ECR images and the vulnerability analyst reports container CVEs alongside EC2 findings.
- **Fleet discovery**: ECS on EC2 and EKS managed node groups use EC2 instances — these appear in SSM Explorer. The host-level patching workflow applies to these hosts.

### What needs new tools

For Fargate workloads (no host access) or image-level remediation, add these tools to the patch manager:

| Tool | Purpose | Integration |
|------|---------|------------|
| `scan_ecr_images` | List vulnerable images and their services | Query Inspector with `resourceType=ECR`, correlate with ECS task definitions |
| `trigger_image_rebuild` | Rebuild image with patched base layer | Invoke CodeBuild project or CodePipeline. Pass the target ECR repo and tag. |
| `update_ecs_service` | Deploy rebuilt image | Call `ecs:UpdateService` with new task definition pointing to the patched image |
| `rollback_ecs_deployment` | Revert to previous image | Call `ecs:UpdateService` with the previous task definition revision |

These are additional tools on the existing agent — same memory, steering, and UI integration.

---

## Delegated Administrator Deployment

By default, the [Multi-Account Setup](../README.md#multi-account-setup) runs from the Organizations management account. Many organizations prefer not to run workloads there. This section describes how to deploy the solution from a **delegated administrator (DA) account** instead.

### How it works

Registering a DA for [Quick Setup](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-delegated-administrator.html) automatically grants DA status for:
- **CloudFormation StackSets** — deploy spoke roles via SERVICE_MANAGED StackSets
- **SSM Explorer** — create org-wide Resource Data Syncs and query `GetOpsSummary`

[Inspector DA](https://docs.aws.amazon.com/inspector/latest/user/designating-admin.html) is registered separately and provides org-wide vulnerability visibility.

The only capability that **cannot** be delegated is [Quick Setup Patch Policies](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager-policies.html) — these must be created from the management account. However, the solution does not create Patch Policies. It detects existing ones and defers to them.

### What runs where

| Task | Account | Frequency |
|------|---------|-----------|
| Register DA for Quick Setup | Management | Once |
| Register DA for Inspector | Management | Once |
| Create Patch Policies (if using) | Management | As needed |
| Quick Setup (DHMC, Host Management, Config Recording) | **DA** | Once |
| SSM Explorer (enable + Resource Data Sync) | **DA** | Once |
| Inspector (activate scanning for member accounts) | **DA** | Once |
| Deploy the solution (`./deploy.sh`) | **DA** | As needed |
| Deploy spoke roles (`./deploy.sh spoke`) | **DA** | As needed |
| Operate the agent and dashboard | **DA** | Continuously |

### Step-by-step setup

**Phase 1 — Management account (one-time, ~10 min)**

These steps require an administrator in the Organizations management account.

1. **Register DA for Quick Setup:**
   ```bash
   # From the management account
   aws ssm-quicksetup register-delegated-administrator \
     --delegated-admin-account-id <DA_ACCOUNT_ID>
   ```
   This automatically registers the DA for CloudFormation StackSets and SSM Explorer.

2. **Register DA for Inspector:**
   ```bash
   aws inspector2 enable-delegated-admin-account \
     --delegated-admin-account-id <DA_ACCOUNT_ID>
   ```

3. **Create Patch Policies** (optional — only if you want org-wide scheduled patching):
   Console → Systems Manager → Quick Setup → Patch Policy → Create. This is the only Quick Setup type that requires the management account.

**Phase 2 — DA account (setup + deploy, ~45 min)**

All remaining steps run from the DA account.

4. **Quick Setup — DHMC, Host Management, Config Recording:**
   Console → Systems Manager → Quick Setup → Library. Create all three targeting the entire org (same steps as [Prerequisites — Step 2](../README.md#aws-account-setup)).

5. **SSM Explorer:**
   Console → Systems Manager → Explorer → Enable Explorer. `deploy.sh` creates the Resource Data Sync automatically.

6. **Inspector — activate scanning:**
   Console → Inspector → Member accounts → Select all → Enable scanning.

7. **Configure `.env`:**
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

8. **Deploy:**
   ```bash
   ./deploy.sh
   ./sample-env.sh deploy
   ```

### Limitations and edge cases

| Limitation | Detail | Workaround |
|-----------|--------|------------|
| **Patch Policies** | Cannot be created from DA account ([docs](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-delegated-administrator.html)) | Create from management account. The agent detects and defers to them. |
| **Opt-in regions** | DA cannot create Resource Data Syncs in [opt-in regions](https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html#regions-opt-in-status) (Milan, Cape Town, Bahrain, Hong Kong) | Use management account to create syncs for opt-in regions. |
| **One DA per service** | Only one account can be DA for Quick Setup, one for Explorer, one for Inspector | Coordinate with other teams using these DA roles. Registering DA for Quick Setup auto-registers for Explorer + StackSets. |
| **DA sync visibility** | Resource Data Syncs created by DA are only visible in the DA account | Not an issue — the solution runs in the DA account. Management account cannot see DA-created syncs. |
### How `deploy.sh` handles DA

`deploy.sh deploy_spoke()` auto-detects whether the caller is the management account or a registered DA:

1. Calls `organizations describe-organization` to get the management account ID
2. If the caller is not the management account, checks `organizations list-delegated-administrators --service-principal ssm.amazonaws.com`
3. If the caller is a registered DA, sets `--call-as DELEGATED_ADMIN` on all SERVICE_MANAGED StackSet API calls
4. If neither management account nor DA, fails with a registration command

No manual flags needed — `./deploy.sh` and `./deploy.sh spoke` work from either account type.

**Why nothing else needs to change:**

| Component | Why it works from DA |
|-----------|---------------------|
| `infra/bin/app.ts` | `hubAccountId` is passed as CDK context — set to the DA account automatically |
| `infra/lib/spoke-role-stack.ts` | Spoke role trusts the hub account root — allows both runtime roles and deployment operations |
| `infra/lib/core-stack.ts` | S3 bucket policy uses `aws:PrincipalOrgID` — org-level, not account-specific |
| `agent/helper/tools/_shared.py` | Fleet discovery uses `GetOpsSummary` with `SyncName` — DA can create and query syncs |
| `agent/helper/cross_account.py` | `get_hub_account_id()` returns the caller's account (DA account). Spoke roles trust this account. |
| `ui/api/server.py` | Dashboard queries Explorer from the DA account. Spoke session assume-role works because spoke role trusts the DA account. |
| `deploy.sh ensure_explorer_sync()` | DA can create `SyncFromSource` with `AwsOrganizations` source type |
| `deploy.sh deploy_agent()` | AgentCore deploys to whichever account runs the CLI — DA account |
| `deploy.sh deploy_infra()` | CDK deploys to the caller's account — DA account |

> **Note on spoke role trust**: The default trust policy allows the entire hub account root (`arn:aws:iam::<hub>:root`) to assume `PatchySpokeRole`. This is intentional for the sample/demo — it enables `./sample-env.sh deploy` to deploy instances to spoke accounts via StackSet without additional setup. For production deployments, see [Security — Production hardening](security.md#cross-account-trust-model-patchyspokerole) to restrict the trust to specific role ARN patterns.
| `deploy.sh deploy_ui()` | CDK deploys Fargate + ALB to the caller's account — DA account |
