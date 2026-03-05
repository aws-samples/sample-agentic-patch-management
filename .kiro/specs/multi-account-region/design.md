# Multi-Account/Region Patch Management — Design

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│              DELEGATED ADMIN ACCOUNT (Hub)                       │
│                                                                  │
│  ┌──────────────┐   ┌──────────────────────────────────────┐    │
│  │ Patchy       │   │ Inspector Delegated Admin             │    │
│  │ UI + Agents  │   │ (auto-aggregates findings from all    │    │
│  │ (ECS Fargate)│   │  member accounts — no code needed)   │    │
│  └──────┬───────┘   └──────────────────────────────────────┘    │
│         │                                                        │
│  ┌──────▼───────────────────────────────────────────────────┐   │
│  │ SSM Parameter Store: /patchy/accounts/{account_id}        │   │
│  │ (account registry — extensible to DynamoDB at scale)      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │ SSM Automation        │  │ S3 Compliance Bucket             │ │
│  │ start_automation_     │  │ /{account_id}/{region}/reports/  │ │
│  │ execution()           │  │ (Resource Data Sync target)      │ │
│  │ + TargetLocations     │  └──────────────────────────────────┘ │
│  └──────────────────────┘                                        │
└──────────────┬──────────────────────────────────────────────────┘
               │  TargetLocations (SSM Automation)
               │  + STS AssumeRole (read-only calls)
        ┌──────┴──────┐
        ▼             ▼
┌──────────────┐  ┌──────────────┐
│  SPOKE A     │  │  SPOKE B     │
│              │  │              │
│ AWS-SSM-     │  │ AWS-SSM-     │
│ Automation   │  │ Automation   │
│ Execution    │  │ Execution    │
│ Role (std)   │  │ Role (std)   │
│              │  │              │
│ EC2 + SSM    │  │ EC2 + SSM    │
│ Resource     │  │ Resource     │
│ Data Sync ──►│  │ Data Sync ──►│──► Hub S3
└──────────────┘  └──────────────┘
```

**Key design decisions:**
- No custom spoke roles — uses `AWS-SystemsManager-AutomationExecutionRole` (standard AWS role)
- No account selector UI — agent resolves accounts from registry via natural language
- No DynamoDB — SSM Parameter Store is sufficient and requires no new infrastructure
- Inspector and Resource Data Sync handle aggregation natively — no polling loops

---

## Feature 1: Account Registry (SSM Parameter Store)

### Design
Each spoke account is stored as a JSON parameter at `/patchy/accounts/{account_id}`:

```json
{
  "account_id": "111111111111",
  "friendly_name": "Production",
  "regions": ["us-east-1", "us-west-2"],
  "environments": ["prod"],
  "enabled": true
}
```

New helper module `agent/helper/account_helper.py`:

```python
import boto3, json
from functools import lru_cache

SSM_PREFIX = '/patchy/accounts/'

def get_accounts(environment: str = None, region: str = None) -> list[dict]:
    """List registered spoke accounts, optionally filtered by environment or region."""
    ssm = boto3.client('ssm')
    paginator = ssm.get_paginator('get_parameters_by_path')
    accounts = []
    for page in paginator.paginate(Path=SSM_PREFIX, Recursive=False):
        for param in page['Parameters']:
            account = json.loads(param['Value'])
            if not account.get('enabled', True):
                continue
            if environment and environment not in account.get('environments', []):
                continue
            if region and region not in account.get('regions', []):
                continue
            accounts.append(account)
    return accounts

# STS session cache — avoids re-assuming role on every call
_session_cache: dict = {}

def get_client(service: str, account_id: str, region: str):
    """Return a boto3 client for a spoke account via STS AssumeRole."""
    import time
    cache_key = f"{account_id}:{region}"
    cached = _session_cache.get(cache_key)
    if cached and cached['expiry'] > time.time() + 300:  # refresh 5min before expiry
        creds = cached['creds']
    else:
        sts = boto3.client('sts')
        resp = sts.assume_role(
            RoleArn=f"arn:aws:iam::{account_id}:role/AWS-SystemsManager-AutomationExecutionRole",
            RoleSessionName=f"patchy-{service}"
        )
        creds = resp['Credentials']
        _session_cache[cache_key] = {
            'creds': creds,
            'expiry': creds['Expiration'].timestamp()
        }
    return boto3.client(
        service, region_name=region,
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken']
    )

def get_target_locations(accounts: list[dict], max_concurrency: str = '10%', max_errors: str = '1') -> list[dict]:
    """Build SSM Automation TargetLocations parameter from account list."""
    locations = []
    for account in accounts:
        for region in account['regions']:
            locations.append({
                'Accounts': [account['account_id']],
                'Regions': [region],
                'ExecutionRoleName': 'AWS-SystemsManager-AutomationExecutionRole',
                'TargetLocationMaxConcurrency': max_concurrency,
                'TargetLocationMaxErrors': max_errors,
            })
    return locations
```

### Registration CLI (no UI needed)
```bash
# Register a spoke account
aws ssm put-parameter \
  --name "/patchy/accounts/111111111111" \
  --type String \
  --value '{"account_id":"111111111111","friendly_name":"Production","regions":["us-east-1"],"environments":["prod"],"enabled":true}'

# List registered accounts
aws ssm get-parameters-by-path --path /patchy/accounts/
```

### Files
- New: `agent/helper/account_helper.py`
- Modified: `infra/lib/ui-stack.ts` — task role SSM + STS permissions
- Modified: `infra/lib/core-stack.ts` — S3 bucket policy for spoke writes

---

## Feature 2: Cross-Account Patching (SSM Automation TargetLocations)

### Design
Replace `send_command()` with `start_automation_execution()` + TargetLocations for multi-account operations. Single-account fast path preserved.

```python
from helper.account_helper import get_accounts, get_target_locations

def _get_target_locations_for_scope(account_ids: list[str] = None, regions: list[str] = None) -> list[dict] | None:
    """Return TargetLocations if multi-account scope, None for single-account."""
    accounts = get_accounts()
    if not accounts:
        return None  # single-account mode — use send_command
    if account_ids:
        accounts = [a for a in accounts if a['account_id'] in account_ids]
    if regions:
        for a in accounts:
            a['regions'] = [r for r in a['regions'] if r in regions]
    return get_target_locations(accounts) if accounts else None
```

#### `patch_dry_run()` — updated
```python
target_locations = _get_target_locations_for_scope(account_ids, regions)

if target_locations:
    # Multi-account: SSM Automation with TargetLocations
    response = ssm_client.start_automation_execution(
        DocumentName='AWS-RunPatchBaseline',
        Parameters={
            'Operation': ['Scan'],
            'BaselineOverride': [json.dumps(baseline_override)] if baseline_override else [],
        },
        TargetLocations=target_locations,
    )
    execution_id = response['AutomationExecutionId']
    # Poll with get_automation_execution() instead of list_command_invocations()
    return _poll_automation_execution(execution_id)
else:
    # Single-account: existing send_command path (unchanged)
    return _send_command_dry_run(instance_ids, baseline_override)
```

Same pattern for `execute_patch_operation()` and `rollback_patches()`.

#### Status polling (new helper)
```python
def _poll_automation_execution(execution_id: str, max_wait: int = 1800) -> dict:
    """Poll SSM Automation execution until complete. Returns aggregated results."""
    import time
    for _ in range(max_wait // 10):
        time.sleep(10)  # nosemgrep
        resp = ssm_client.get_automation_execution(AutomationExecutionId=execution_id)
        status = resp['AutomationExecution']['AutomationExecutionStatus']
        if status in ('Success', 'Failed', 'Cancelled', 'TimedOut'):
            return _parse_automation_results(resp['AutomationExecution'])
    return {'status': 'TimedOut', 'execution_id': execution_id}
```

### Files Changed
- Modified: `agent/helper/tools.py` — `patch_dry_run()`, `execute_patch_operation()`, `rollback_patches()`
- New helpers in `agent/helper/account_helper.py`

---

## Feature 3: Cross-Account Discovery & Health

### `get_vulnerability_findings()` — no code change
Inspector delegated admin aggregates findings. Resource ARNs already include account IDs:
`arn:aws:ec2:us-east-1:111111111111:instance/i-abc123`

Minor addition: parse and include `account_id` in the grouped response.

### `assess_fleet_impact()` — minor change
```python
# Parse account_id from Inspector resource ARN
account_id = resource_arn.split(':')[4] if ':' in resource_arn else LOCAL_ACCOUNT
ec2 = account_helper.get_client('ec2', account_id, region)
# ... existing describe_instances logic unchanged
```

### `get_patch_compliance()` — query aggregated S3 data
Resource Data Sync pushes compliance data to hub S3 under `{account_id}/`. Query the aggregated data directly — no assume-role needed for this path.

### `check_instance_health()` / `check_cloudwatch_alarms()` — assume-role per account
```python
for account in get_accounts():
    ssm = get_client('ssm', account['account_id'], region)
    # ... existing logic, merge results with account_id context
```

### Files Changed
- Modified: `agent/helper/tools.py` — 4 functions, minor additions

---

## Feature 4: Supervisor Account Awareness

### New tool: `list_registered_accounts()`
```python
def list_registered_accounts(environment: str = None) -> dict:
    """List accounts registered in the Patchy account registry."""
    accounts = account_helper.get_accounts(environment=environment)
    return {
        'accounts': accounts,
        'count': len(accounts),
        'single_account_mode': len(accounts) == 0
    }
```

### Supervisor system prompt addition
```
MULTI-ACCOUNT OPERATIONS:
When the user references environments, accounts, or regions:
1. Call list_registered_accounts() to get the registry
2. Map user intent to specific account_ids and regions:
   - "patch prod" → accounts with environments containing "prod"
   - "patch account 111111111111" → direct account targeting
   - "patch us-west-2" → all accounts with us-west-2 in regions
3. Before executing cross-account bulk operations, state the scope explicitly:
   "I will patch X instances across Y accounts: [list]. Proceeding..."
4. If no accounts registered → single-account mode (current behavior, no change)
```

### Files Changed
- Modified: `agent/supervisor.py` — system prompt + tool registration
- New tool in `agent/helper/tools.py`: `list_registered_accounts()`

---

## Feature 5: Compliance Schema & Hub IAM

### Compliance report schema update
```json
{
  "account_id": "111111111111",
  "region": "us-east-1",
  "environment": "prod",
  "cve_id": "CVE-2024-1234",
  ...
}
```
S3 key: `{account_id}/{region}/{date}/{cve_id}-report.json`

### Hub IAM additions (ui-stack.ts task role)
```typescript
taskRole.addToPolicy(new iam.PolicyStatement({
  actions: [
    'ssm:StartAutomationExecution',
    'ssm:GetAutomationExecution',
    'ssm:DescribeAutomationExecutions',
    'ssm:GetParameter',
    'ssm:GetParametersByPath',
  ],
  resources: ['*'],
}));
taskRole.addToPolicy(new iam.PolicyStatement({
  actions: ['sts:AssumeRole'],
  resources: ['arn:aws:iam::*:role/AWS-SystemsManager-AutomationExecutionRole'],
}));
```

### S3 bucket policy (core-stack.ts)
Allow spoke accounts to write Resource Data Sync output:
```typescript
complianceReportsBucket.addToResourcePolicy(new iam.PolicyStatement({
  principals: [new iam.OrganizationPrincipal(organizationId)],
  actions: ['s3:PutObject'],
  resources: [`${complianceReportsBucket.bucketArn}/*`],
}));
```

---

## Implementation Order

```
Phase 1 (Foundation) — 1.5 days
├── account_helper.py (registry + get_client + get_target_locations)
├── Hub IAM updates (ui-stack.ts + core-stack.ts)
└── list_registered_accounts() tool + supervisor prompt

Phase 2 (Core Multi-Account) — 2 days
├── patch_dry_run() → TargetLocations
├── execute_patch_operation() → TargetLocations
├── rollback_patches() → TargetLocations
├── assess_fleet_impact() account_id parsing
├── check_instance_health() + check_cloudwatch_alarms() assume-role
└── Compliance schema update

Total: ~3.5 days
```

## Testing Strategy
- Phase 1: Register a second account in Parameter Store, verify `list_registered_accounts()` returns it
- Phase 2: Run `patch_dry_run()` targeting both hub + spoke, verify TargetLocations execution
- Backward compat: all existing single-account integration tests pass with no accounts registered
- E2E: register spoke → "patch prod" in chat → verify cross-account Automation execution

## Production Hardening (document in README)

| Built | Production path |
|-------|-----------------|
| SSM Parameter Store registry | DynamoDB with TTL, conditional writes, audit trail |
| Manual account registration | StackSets + Organizations auto-enrollment |
| Default MaxConcurrency 10% | Per-operation configuration via agent parameters |
| Assume-role for CW alarms | CloudWatch cross-account observability (Organizations) |
| No account health validation | Scheduled Lambda validates connectivity per account |
| No audit aggregation | EventBridge cross-account forwarding to central log |
