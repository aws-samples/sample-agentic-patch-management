# Extending the Solution — Intelligent Patch Automation

The multi-agent architecture is designed for extensibility. Each specialist agent has its own tool set -- adding a new capability is a single tool function with no changes to routing or decision logic.

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
| **Cross-account patching** | Manage instances across multiple AWS accounts | Add assume-role logic to operate in spoke accounts. The dashboard API's `scopes` array already supports multiple accounts. |

Each extension is a single tool function added to the relevant specialist agent -- no changes to the supervisor routing, memory, or UI.

---

## Adding a New Tool

### 1. Write the tool function

Add to `agent/helper/tools.py`:

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

### 2. Register with the specialist agent

Import and add to the relevant specialist's tool list. For example, in `patch_manager.py`:

```python
from helper.tools import my_new_tool

# In patching_operations():
return create_agent(
    name="patch-manager",
    system_prompt=SYSTEM_PROMPT,
    tools=[
        ...existing tools...,
        my_new_tool,
    ],
    context=context
)
```

### 3. Update the system prompt

Add instructions for when and how to use the new tool in the specialist's `SYSTEM_PROMPT`.

### 4. Add a response template (optional)

If the tool's output needs a specific presentation format, add a template to `agent/config/response_templates.yaml`:

```yaml
my_new_result:
  description: "Results from my new tool"
  agent: patch_manager
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

See `docs/feature-container-vulnerability-support.md` for implementation details.
