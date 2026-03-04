# Multi-Account/Region Patch Management — Design

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    HUB ACCOUNT (Central)                     │
│                                                              │
│  ┌──────────┐  ┌───────────┐  ┌──────────────────────────┐  │
│  │ Patchy   │  │ Account   │  │ Inspector Delegated Admin│  │
│  │ UI/Agents│──│ Registry  │  │ (aggregated findings)    │  │
│  │ (ECS)    │  │ (DynamoDB)│  └──────────────────────────┘  │
│  └────┬─────┘  └───────────┘                                 │
│       │                                                      │
│       │  STS AssumeRole                                      │
│       ▼                                                      │
│  ┌──────────────────┐  ┌─────────────────────────────────┐  │
│  │ SSM Automation    │  │ Central Compliance Bucket (S3)  │  │
│  │ (TargetLocations) │  │ /{account_id}/{region}/reports/ │  │
│  └──────────────────┘  └─────────────────────────────────┘  │
└──────────┬──────────────────────────────────────────────────┘
           │
    ┌──────┴──────┐
    ▼             ▼
┌────────┐   ┌────────┐
│ SPOKE  │   │ SPOKE  │
│ Acct A │   │ Acct B │
│        │   │        │
│ IAM:   │   │ IAM:   │
│ Patchy │   │ Patchy │
│ Spoke  │   │ Spoke  │
│ Role   │   │ Role   │
│        │   │        │
│ EC2s   │   │ EC2s   │
│ SSM    │   │ SSM    │
└────────┘   └────────┘
```

## Feature 1: Account Registry & Assume-Role Helper

### Design
- DynamoDB table `patchy-account-registry` in hub account
- Schema: `account_id` (PK), `regions` (SS), `role_arn` (S), `friendly_name` (S), `environments` (SS), `enabled` (BOOL)
- New helper module `agent/helper/account_helper.py` with:
  - `get_accounts(environment=None, region=None)` → list of account configs
  - `get_client(service, account_id, region)` → boto3 client via STS assume-role with session caching
  - `get_all_target_locations(account_ids, regions)` → TargetLocations param for SSM Automation

### Session Caching
```python
# Cache assumed-role sessions (1hr TTL, refresh at 50min)
_session_cache: Dict[str, Dict] = {}  # key: f"{account_id}:{region}"

def get_client(service: str, account_id: str, region: str) -> boto3.client:
    cache_key = f"{account_id}:{region}"
    if cache_key not in _session_cache or _is_expired(_session_cache[cache_key]):
        sts = boto3.client('sts')
        creds = sts.assume_role(
            RoleArn=f"arn:aws:iam::{account_id}:role/PatchySpokeRole",
            RoleSessionName=f"patchy-{service}-{account_id}"
        )['Credentials']
        _session_cache[cache_key] = {
            'credentials': creds,
            'expires': creds['Expiration']
        }
    return boto3.client(service, region_name=region, **_cached_creds(cache_key))
```

### API Endpoints (UI backend)
- `GET /api/accounts` — list registered accounts
- `POST /api/accounts` — register new spoke account
- `PUT /api/accounts/{account_id}` — update account config
- `DELETE /api/accounts/{account_id}` — deregister account

### Files Changed
- New: `agent/helper/account_helper.py`
- New: `ui/api/routes/accounts.py`
- Modified: `infra/lib/core-stack.ts` (DynamoDB table)
- Modified: `infra/lib/ui-stack.ts` (task role DynamoDB + STS permissions)

---

## Feature 2: Spoke Account CDK Stack

### Design
- New CDK stack `PatchySpokeStack` deployable standalone or via StackSets
- Creates IAM role `PatchySpokeRole` with trust to hub account
- Configures Resource Data Sync to push inventory to hub's S3 bucket

### IAM Role Policy
```typescript
// Permissions for the spoke role
const spokePolicy = new iam.PolicyDocument({
  statements: [
    // SSM operations
    new iam.PolicyStatement({
      actions: [
        'ssm:SendCommand', 'ssm:GetCommandInvocation', 'ssm:ListCommandInvocations',
        'ssm:DescribeInstancePatchStates', 'ssm:DescribeInstancePatches',
        'ssm:DescribeInstanceInformation', 'ssm:DescribeMaintenanceWindows',
        'ssm:DescribeMaintenanceWindowTargets', 'ssm:DescribeMaintenanceWindowTasks',
        'ssm:GetMaintenanceWindow', 'ssm:RegisterTaskWithMaintenanceWindow',
        'ssm:CreateMaintenanceWindow', 'ssm:RegisterTargetWithMaintenanceWindow',
        'ssm:StartAutomationExecution', 'ssm:DescribeAutomationExecutions',
      ],
      resources: ['*'],
    }),
    // EC2 read-only
    new iam.PolicyStatement({
      actions: ['ec2:DescribeInstances', 'ec2:DescribeTags'],
      resources: ['*'],
    }),
    // CloudWatch read-only
    new iam.PolicyStatement({
      actions: ['cloudwatch:DescribeAlarms'],
      resources: ['*'],
    }),
    // S3 write to central compliance bucket
    new iam.PolicyStatement({
      actions: ['s3:PutObject', 's3:GetObject'],
      resources: [`arn:aws:s3:::patch-compliance-reports-${hubAccountId}/*`],
    }),
  ],
});
```

### Files
- New: `infra/lib/spoke-stack.ts`
- New: `infra/bin/spoke-app.ts` (separate entry point for spoke deployment)
- Modified: `deploy.sh` (add `spoke` target)

---

## Feature 3: Cross-Account Patching (SSM Automation TargetLocations)

### Design
Replace `send_command()` with `start_automation_execution()` in three tools:

#### `patch_dry_run()` — Change
```python
# Before (single-account):
response = ssm_client.send_command(
    DocumentName='AWS-RunPatchBaseline',
    InstanceIds=instance_ids,
    Parameters={'Operation': ['Scan']},
)

# After (multi-account):
from helper.account_helper import get_target_locations, group_instances_by_account

instance_groups = group_instances_by_account(instance_ids)  # {account_id: [ids]}

if len(instance_groups) == 1 and list(instance_groups.keys())[0] == LOCAL_ACCOUNT:
    # Single-account fast path — keep existing send_command for backward compat
    response = ssm_client.send_command(...)
else:
    # Multi-account path
    response = ssm_client.start_automation_execution(
        DocumentName='AWS-RunPatchBaseline',
        Parameters={'Operation': ['Scan']},
        TargetLocations=get_target_locations(instance_groups),
    )
```

The same pattern applies to `execute_patch_operation()` and `rollback_patches()`.

#### Instance-to-Account Mapping
Instances carry their account ID in the ARN or we look it up from the registry. For efficiency, `assess_fleet_impact()` already gets instance IDs from Inspector findings which include the account ID in the resource ARN. We parse it:
```python
# Inspector finding resource ID: "arn:aws:ec2:us-east-1:111111111111:instance/i-abc123"
# or just "i-abc123" — need to handle both
```

### Backward Compatibility
- If no spoke accounts registered → all tools behave exactly as today (single-account)
- The `group_instances_by_account()` function returns `{local_account: all_ids}` when no registry exists
- Zero config change needed for existing single-account deployments

### Files Changed
- Modified: `agent/helper/tools.py` — `patch_dry_run()`, `execute_patch_operation()`, `rollback_patches()`
- New helper functions in `agent/helper/account_helper.py`

---

## Feature 4: Cross-Account Discovery & Health

### Design

#### `get_vulnerability_findings()` — No Code Change
Inspector delegated admin aggregates findings. Response already includes resource ARNs with account IDs. Just need to parse and group.

#### `assess_fleet_impact()` — Minor Change
- Parse account_id from Inspector resource ARNs
- Use `get_client('ec2', account_id, region)` for cross-account `describe_instances`
- Add `account_id` field to environment grouping

#### `get_patch_compliance()` — Moderate Change
- Accept optional `account_id` parameter
- If provided, use assumed-role client for that account
- If not provided, query all registered accounts and aggregate

#### `check_instance_health()` — Minor Change
- Group instance_ids by account
- Use assumed-role SSM client per account
- Merge results

#### `check_cloudwatch_alarms()` — Minor Change
- Same pattern as health checks — group by account, assume role, merge

### Files Changed
- Modified: `agent/helper/tools.py` — 5 functions updated
- Uses `account_helper.get_client()` for cross-account calls

---

## Feature 5: Supervisor Account Awareness

### Design
- Supervisor queries account registry when resolving user intent
- New tool: `list_registered_accounts()` available to supervisor
- Supervisor enriches specialist queries with account scope:
  - "patch prod" → resolves to accounts tagged with `prod` environment
  - "patch account 111111111111" → direct account targeting
  - "patch all accounts in us-west-2" → region filter

### System Prompt Addition
```
ACCOUNT RESOLUTION:
When the user references environments, accounts, or regions:
1. Call list_registered_accounts() to get the registry
2. Map user intent to specific account_ids and regions
3. Pass account scope to specialist agents via enriched query

If no accounts are registered, operate in single-account mode (current behavior).
```

### Files Changed
- Modified: `agent/supervisor.py` — system prompt + new tool
- New tool in `agent/helper/tools.py`: `list_registered_accounts()`

---

## Feature 6: UI Account/Region Support

### Design
- Account selector component (dropdown, populated from `/api/accounts`)
- Region filter (derived from selected account's registered regions)
- Dashboard cards show per-account metrics
- Compliance reports table adds Account column
- Chat input includes account context in API payload

### Component Changes
- New: `ui/frontend/src/components/AccountSelector.tsx`
- Modified: `ui/frontend/src/App.tsx` — account context in state
- Modified: `ui/frontend/src/pages/Dashboard.tsx` — per-account cards
- Modified: `ui/frontend/src/pages/Compliance.tsx` — account column
- Modified: `ui/frontend/src/hooks/useChat.ts` — pass account scope

---

## Feature 7: Hub CDK Stack Updates

### Design
- DynamoDB table for account registry
- S3 bucket policy update to allow spoke account writes
- ECS task role: add `sts:AssumeRole` for spoke roles
- SSM Automation service role for cross-account execution

### Files Changed
- Modified: `infra/lib/core-stack.ts` — DynamoDB table, S3 bucket policy
- Modified: `infra/lib/ui-stack.ts` — task role STS permissions

---

## Implementation Order

```
Phase 1 (Foundation) — 2 days
├── Feature 1: Account Registry + assume-role helper
└── Feature 2: Spoke CDK Stack

Phase 2 (Core Multi-Account) — 3 days
├── Feature 3: Cross-account patching (TargetLocations)
├── Feature 4: Cross-account discovery & health
└── Feature 5: Supervisor account awareness

Phase 3 (UI + Polish) — 2 days
├── Feature 6: UI account/region support
└── Feature 7: Hub CDK stack updates

Total: ~7 days
```

## Testing Strategy
- Phase 1: Deploy spoke stack to a second account, verify assume-role works
- Phase 2: Run integration tests with `--accounts` flag targeting both hub + spoke
- Phase 3: E2E test through UI with account selector
- Backward compat: All existing single-account tests must pass unchanged
