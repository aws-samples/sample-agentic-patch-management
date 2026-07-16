#!/bin/bash

# Intelligent Patch Automation — Deployment Script
# Usage: ./deploy.sh [agent|ui|spoke|docs|status|create-user|destroy|destroy --spoke-only|destroy --docs-only]
#
# Sample environment lifecycle is managed by ./sample-env.sh (deploy | destroy | status)
#
# Destroy preserves the spoke + docs StackSets and Resource Data Sync by default.
# Opt in by exporting DESTROY_SPOKE_STACKSET=true and/or DESTROY_FLEET_SYNC=true,
# or use ./deploy.sh destroy --spoke-only or --docs-only for the StackSets alone.

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Preserve caller's env vars — .env only fills in unset values
_saved_profile="${AWS_PROFILE:-}"
_saved_region="${AWS_REGION:-}"

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# Caller's env vars take precedence over .env
AWS_PROFILE="${_saved_profile:-${AWS_PROFILE:-default}}"
AWS_REGION="${_saved_region:-${AWS_REGION:-us-east-1}}"
AGENTCORE_ROLE_ARN="${AGENTCORE_ROLE_ARN:-}"
export AWS_PROFILE AWS_REGION AGENTCORE_ROLE_ARN
# CDK uses these to determine target account/region for synthesis.
# Must use --profile explicitly since export may not propagate to subshell in time.
CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text 2>/dev/null)
if [ -z "$CDK_DEFAULT_ACCOUNT" ]; then
    echo -e "${RED}❌ Failed to resolve AWS account. Check credentials (aws sts get-caller-identity --profile $AWS_PROFILE).${NC}"
    exit 1
fi
export CDK_DEFAULT_ACCOUNT
export CDK_DEFAULT_REGION=$AWS_REGION
unset _saved_profile _saved_region

print_status() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

# ── Destroy toggles ─────────────────────────────────────────────────
# By default, `./deploy.sh destroy` does NOT remove the Patchy-SpokeIam +
# Patchy-SsmDocs StackSets or the patchy-fleet-sync Resource Data Sync, because:
#   - The StackSets provision IAM and SSM Documents in customer-owned accounts.
#     Removing them has org-wide blast radius and operators usually want a
#     deliberate decision before doing so.
#   - The Resource Data Sync is org-level and may be relied on by other
#     tools (Trusted Advisor, Quick Setup, Inventory dashboards). Re-creation
#     also has an asynchronous ingestion window of minutes-to-hours.
#
# To opt in, flip these to true (or export the env var before running):
#   DESTROY_SPOKE_STACKSET=true ./deploy.sh destroy
#   DESTROY_FLEET_SYNC=true ./deploy.sh destroy
# Or run the targeted command:  ./deploy.sh destroy --spoke-only
DESTROY_SPOKE_STACKSET="${DESTROY_SPOKE_STACKSET:-false}"
DESTROY_FLEET_SYNC="${DESTROY_FLEET_SYNC:-false}"

# ── StackSet operation preferences ──────────────────────────────────
# Tunable via env vars. Defaults are reasonable for any fleet size:
#   - PARALLEL region rollouts (default cap unbounded — CFN handles concurrency)
#   - 100% account concurrency (each account in a target group rolls in parallel)
#   - Fail-fast (any per-instance failure aborts the operation)
#
# Override examples:
#   STACKSET_REGION_CONCURRENCY=3 ./deploy.sh   # cap regions to 3 at a time
#   STACKSET_FAILURE_TOLERANCE=25 ./deploy.sh   # tolerate 25% per-instance failures
#   STACKSET_WAIT=false ./deploy.sh             # don't block on stackset verification
STACKSET_REGION_CONCURRENCY="${STACKSET_REGION_CONCURRENCY:-}"
STACKSET_FAILURE_TOLERANCE="${STACKSET_FAILURE_TOLERANCE:-0}"
STACKSET_WAIT="${STACKSET_WAIT:-true}"

# Build the --operation-preferences string once for reuse across all StackSet
# create/update/instance calls. Region concurrency is capped only when the env
# var is set; otherwise CFN parallelises across all regions in the operation.
_build_stackset_op_prefs() {
    local prefs="RegionConcurrencyType=PARALLEL,MaxConcurrentPercentage=100,FailureTolerancePercentage=${STACKSET_FAILURE_TOLERANCE}"
    if [ -n "$STACKSET_REGION_CONCURRENCY" ]; then
        prefs="${prefs},ConcurrentRegions=${STACKSET_REGION_CONCURRENCY}"
    fi
    echo "$prefs"
}
_STACKSET_OP_PREFS="$(_build_stackset_op_prefs)"

# ── Help ────────────────────────────────────────────────────────────

show_help() {
    echo -e "${BLUE}Intelligent Patch Automation — Deployment${NC}"
    echo "=================================================="
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "Deploy:"
    echo "  ${GREEN}(no args)${NC}         Deploy full solution (infra + agent + UI)"
    echo "  ${GREEN}agent${NC}             Redeploy agent only (after code changes)"
    echo "  ${GREEN}ui${NC}               Redeploy UI only (after code changes)"
    echo "  ${GREEN}spoke${NC}            Deploy spoke IAM role to target accounts via StackSet"
    echo "                     For non-org accounts: cd infra && npx cdk deploy Patchy-SpokeIam"
    echo "                       --profile <spoke-profile> -c hubAccountId=<hub-account-id>"
    echo "  ${GREEN}docs${NC}             Deploy SSM Automation documents to (hub + spokes) × all SPOKE_REGIONS"
    echo ""
    echo "Sample environment (separate lifecycle):"
    echo "  ${BLUE}./sample-env.sh deploy${NC}   Deploy 5 sample EC2 instances + StackSet to spokes"
    echo "  ${BLUE}./sample-env.sh destroy${NC}  Tear down sample environment"
    echo "  ${BLUE}./sample-env.sh status${NC}   Show sample-env state"
    echo ""
    echo "Inspect:"
    echo "  ${BLUE}status${NC}           Show current deployment configuration (read-only)"
    echo ""
    echo "Users:"
    echo "  ${GREEN}create-user${NC}      Create a Cognito user (interactive — prompts for email, password, role)"
    echo ""
    echo "Cleanup:"
    echo "  ${RED}destroy${NC}                Destroy hub infrastructure only"
    echo "                          (preserves spoke + docs StackSets + sync by default)"
    echo "  ${RED}destroy --spoke-only${NC}    Destroy Patchy-SpokeIam StackSet (and legacy Patchy-SpokeRole)"
    echo "  ${RED}destroy --docs-only${NC}     Destroy Patchy-SsmDocs StackSet"
    echo ""
    echo "  Opt-in flags for full teardown:"
    echo "    ${YELLOW}DESTROY_SPOKE_STACKSET=true${NC}  also remove Patchy-SpokeIam + Patchy-SsmDocs StackSets"
    echo "    ${YELLOW}DESTROY_FLEET_SYNC=true${NC}      also remove patchy-fleet-sync"
    echo "    Example: DESTROY_SPOKE_STACKSET=true DESTROY_FLEET_SYNC=true $0 destroy"
    echo ""
    echo "Observability:"
    echo "    ${YELLOW}ENABLE_RUNTIME_LOGS=true${NC}    deliver runtime APPLICATION_LOGS to CloudWatch Logs"
    echo "    ${YELLOW}ENABLE_TRACING=true${NC}         enable CloudWatch Transaction Search + runtime trace spans"
    echo "    Both off by default. Independent — turn on whichever you need."
    echo ""
    echo "StackSet tuning (advanced, for spoke + docs StackSets):"
    echo "    ${YELLOW}STACKSET_REGION_CONCURRENCY=N${NC}  cap parallel regions per operation (default: unbounded)"
    echo "    ${YELLOW}STACKSET_FAILURE_TOLERANCE=N${NC}   percent of per-instance failures tolerated (default: 0 / fail-fast)"
    echo "    ${YELLOW}STACKSET_WAIT=false${NC}            don't block on operation success (default: true / verify)"
    echo ""
    echo "First-time setup:"
    echo "  1. cp .env.example .env"
    echo "  2. Edit .env — set AWS_PROFILE and AWS_REGION"
    echo "  3. ./deploy.sh"
    echo "  4. ./connect-ui.sh  (opens UI via SSM port forwarding)"
    echo ""
    echo "That's it. The script handles agent deployment, role detection,"
    echo "CDK bootstrap, infrastructure, and UI automatically."
    echo ""
}

# ── Prereq checks ──────────────────────────────────────────────────

check_prereqs() {
    local need_python=${1:-false}
    local need_node=${2:-false}
    local need_docker=${3:-false}

    if ! command -v aws &> /dev/null; then
        print_error "AWS CLI v2 required. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
        exit 1
    fi

    if [ "$need_python" = true ]; then
        if ! command -v python3 &> /dev/null; then
            print_error "Python 3.11+ required"
            exit 1
        fi
        local py_version
        py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        if python3 -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            print_status "Python $py_version"
        else
            print_error "Python 3.11+ required (found $py_version)"
            exit 1
        fi
    fi

    if [ "$need_node" = true ]; then
        if ! command -v node &> /dev/null; then
            print_error "Node.js 18+ required"
            exit 1
        fi
        local node_major
        node_major=$(node -e "console.log(process.versions.node.split('.')[0])")
        if [ "$node_major" -ge 18 ] 2>/dev/null; then
            print_status "Node.js $(node --version)"
        else
            print_error "Node.js 18+ required (found $(node --version))"
            exit 1
        fi
    fi

    if [ "$need_docker" = true ]; then
        local container_cli="${CDK_DOCKER:-}"
        if [ -z "$container_cli" ]; then
            if command -v docker &> /dev/null; then
                container_cli="docker"
            elif command -v finch &> /dev/null; then
                container_cli="finch"
                export CDK_DOCKER=finch
            else
                print_error "Container runtime required (CDK builds the UI container image locally)"
                echo "  Install Docker or Finch: brew install finch && finch vm start"
                exit 1
            fi
        fi
        if ! "$container_cli" info &> /dev/null; then
            print_error "'$container_cli' is installed but the daemon is not reachable."
            if [ "$container_cli" = "finch" ]; then
                echo "  Run: finch vm start"
            else
                echo "  Start your Docker runtime, or set DOCKER_HOST in .env"
            fi
            exit 1
        fi
        local cli_version
        cli_version=$("$container_cli" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        print_status "$container_cli $cli_version${CDK_DOCKER:+ (CDK_DOCKER=$CDK_DOCKER)}"
    fi
}

# ── Setup Python venv ───────────────────────────────────────────────

setup_environment() {
    echo -e "${BLUE}Setting up Python environment...${NC}"
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        print_status "Virtual environment created"
    fi
    source venv/bin/activate
    if [ ! -f "venv/.deps_installed" ]; then
        pip install --upgrade pip -q
        pip install -r agent/requirements.txt -q
        touch venv/.deps_installed
        print_status "Dependencies installed"
    else
        print_status "Dependencies already installed"
    fi
    export AWS_PROFILE=$AWS_PROFILE
    export AWS_DEFAULT_REGION=$AWS_REGION
    export AWS_DEFAULT_PROFILE=$AWS_PROFILE
    if ! aws sts get-caller-identity --output text &> /dev/null; then
        print_error "AWS credentials not valid. Run: aws configure --profile $AWS_PROFILE"
        exit 1
    fi
    print_status "AWS environment ready (profile: $AWS_PROFILE, region: $AWS_REGION)"
}

# ── CDK bootstrap ──────────────────────────────────────────────────

ensure_cdk_bootstrap() {
    local account_id
    account_id="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"

    echo "Checking CDK bootstrap for ${account_id}/${AWS_REGION}..."

    # Check if the bootstrap SSM parameter exists — this is the definitive check
    if aws ssm get-parameter --name "/cdk-bootstrap/hnb659fds/version" &> /dev/null; then
        print_status "CDK already bootstrapped"
    else
        echo "Bootstrapping CDK..."
        npx cdk bootstrap "aws://${account_id}/${AWS_REGION}"
        print_status "CDK bootstrapped"
    fi
}

# ── AgentCore Observability ─────────────────────────────────────────
#
# Two independent layers, two flags:
#   ENABLE_TRACING=true        — account-level Transaction Search +
#                                per-runtime TRACES delivery
#   ENABLE_RUNTIME_LOGS=true   — per-runtime APPLICATION_LOGS delivery
#                                to /aws/vendedlogs/bedrock-agentcore/...
#
# Both off by default. Turn on whichever you need.
#
# Per-runtime parts (TRACES + APPLICATION_LOGS) need the runtime ARN,
# which only exists after deploy_agent. The account-level part runs
# here, the per-runtime part runs in ensure_runtime_observability().
ensure_observability() {
    if [ "${ENABLE_TRACING:-false}" != "true" ]; then
        return 0
    fi

    echo -e "${BLUE}Configuring AgentCore Observability (CloudWatch Transaction Search)...${NC}"

    local account_id partition policy_name
    account_id="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
    partition="aws"
    policy_name="AgentCoreTransactionSearchXRayAccess"

    # ── Step 1: Check current trace segment destination ─────────────
    # If already CloudWatchLogs (and ACTIVE), nothing to do.
    local current_dest current_status
    current_dest=$(aws xray get-trace-segment-destination \
        --query 'Destination' --output text 2>/dev/null || echo "Unknown")
    current_status=$(aws xray get-trace-segment-destination \
        --query 'Status' --output text 2>/dev/null || echo "Unknown")

    if [ "$current_dest" = "CloudWatchLogs" ] && [ "$current_status" = "ACTIVE" ]; then
        print_status "Transaction Search already enabled (destination: CloudWatchLogs)"
        return 0
    fi

    # ── Step 2: Put resource policy on CloudWatch Logs ──────────────
    # Lets X-Ray ingest spans into the /aws/spans and Application Signals
    # log groups. SourceArn/SourceAccount conditions scope to this account.
    local policy_doc
    policy_doc=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TransactionSearchXRayAccess",
      "Effect": "Allow",
      "Principal": { "Service": "xray.amazonaws.com" },
      "Action": "logs:PutLogEvents",
      "Resource": [
        "arn:${partition}:logs:${AWS_REGION}:${account_id}:log-group:aws/spans:*",
        "arn:${partition}:logs:${AWS_REGION}:${account_id}:log-group:/aws/application-signals/data:*"
      ],
      "Condition": {
        "ArnLike": { "aws:SourceArn": "arn:${partition}:xray:${AWS_REGION}:${account_id}:*" },
        "StringEquals": { "aws:SourceAccount": "${account_id}" }
      }
    }
  ]
}
EOF
    )

    set +e
    aws logs put-resource-policy \
        --policy-name "$policy_name" \
        --policy-document "$policy_doc" \
        --region "$AWS_REGION" > /dev/null 2>&1
    local policy_rc=$?
    set -e

    if [ $policy_rc -ne 0 ]; then
        print_warning "Could not create CloudWatch Logs resource policy '$policy_name'"
        print_warning "AgentCore Observability traces may not appear. Continue manually in the CloudWatch console."
        return 0
    fi
    print_status "CloudWatch Logs resource policy ensured: $policy_name"

    # ── Step 3: Update trace segment destination to CloudWatchLogs ──
    set +e
    aws xray update-trace-segment-destination \
        --destination CloudWatchLogs \
        --region "$AWS_REGION" > /dev/null 2>&1
    local xray_rc=$?
    set -e

    if [ $xray_rc -ne 0 ]; then
        print_warning "Could not update X-Ray trace segment destination to CloudWatchLogs"
        print_warning "Enable manually: CloudWatch console → Settings → X-Ray traces → Transaction Search"
        return 0
    fi

    print_status "X-Ray trace destination set to CloudWatchLogs"
    print_status "AgentCore Observability enabled (spans appear in CloudWatch within ~10 min)"
}

# ── Per-runtime observability (APPLICATION_LOGS + TRACES) ───────────
# Runs after deploy_agent so the runtime ARN is available. Idempotent:
# each step describes existing resources first and skips if already
# configured.
#
# Wires up CloudWatch Logs vended-logs delivery for the AgentCore
# runtime. Uses three CWL APIs in sequence:
#   1. put-delivery-source     — declares the runtime as a log source
#   2. put-delivery-destination — declares where logs go (CWL group / XRAY)
#   3. create-delivery         — binds source to destination
#
# AgentCore's resource-side authorization (AllowVendedLogDeliveryForResource)
# fires automatically inside put-delivery-source — no separate call.
#
# Two independent deliveries, controlled by:
#   ENABLE_RUNTIME_LOGS=true → APPLICATION_LOGS to /aws/vendedlogs/...
#   ENABLE_TRACING=true     → TRACES to X-Ray (in addition to account-level
#                             Transaction Search wired by ensure_observability)
ensure_runtime_observability() {
    local want_logs="${ENABLE_RUNTIME_LOGS:-false}"
    local want_traces="${ENABLE_TRACING:-false}"

    if [ "$want_logs" != "true" ] && [ "$want_traces" != "true" ]; then
        return 0
    fi

    # Resolve runtime ARN from agentcore deployed state.
    local deployed_state="agent/agentcore/.cli/deployed-state.json"
    if [ ! -f "$deployed_state" ]; then
        print_warning "Runtime observability skipped — agent not deployed yet (no deployed-state.json)"
        return 0
    fi

    local runtime_arn runtime_id
    runtime_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'runtimeArn' in a:
        print(a['runtimeArn']); break
" 2>/dev/null || true)

    if [ -z "$runtime_arn" ]; then
        print_warning "Runtime observability skipped — could not resolve runtime ARN"
        return 0
    fi

    # Extract runtime ID (last segment of ARN, with the suffix after / removed for naming).
    runtime_id="${runtime_arn##*/}"
    runtime_id="${runtime_id%-*}"  # strip trailing -<random> id when naming resources

    local account_id
    account_id="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"

    if [ "$want_logs" = "true" ]; then
        _ensure_runtime_delivery "$runtime_arn" "$runtime_id" "$account_id" \
            "APPLICATION_LOGS" "patchy-runtime-app-logs" "CWL"
    fi

    if [ "$want_traces" = "true" ]; then
        _ensure_runtime_delivery "$runtime_arn" "$runtime_id" "$account_id" \
            "TRACES" "patchy-runtime-traces" "XRAY"
    fi
}

# Internal: wire one delivery (source → destination → create-delivery).
# Idempotent — describe-* first, skip if already wired.
#
# Args:
#   $1 runtime_arn    full ARN of the AgentCore runtime
#   $2 runtime_id     short id used as prefix for log group + delivery names
#   $3 account_id     AWS account ID
#   $4 log_type       APPLICATION_LOGS | TRACES
#   $5 name_prefix    base name for delivery source / destination
#   $6 dest_type      CWL | XRAY (CWL writes to a log group; XRAY ships to X-Ray)
_ensure_runtime_delivery() {
    local runtime_arn="$1" runtime_id="$2" account_id="$3"
    local log_type="$4" name_prefix="$5" dest_type="$6"

    local source_name="${name_prefix}-source"
    local destination_name="${name_prefix}-destination"

    # ── 1. Delivery source ──────────────────────────────────────────
    if ! aws logs get-delivery-source --name "$source_name" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        set +e
        aws logs put-delivery-source \
            --name "$source_name" \
            --resource-arn "$runtime_arn" \
            --log-type "$log_type" \
            --region "$AWS_REGION" >/dev/null 2>&1
        local rc=$?
        set -e
        if [ $rc -ne 0 ]; then
            print_warning "Could not create delivery source $source_name (logType=$log_type) — skipping"
            return 0
        fi
        print_status "Delivery source created: $source_name ($log_type)"
    fi

    # ── 2. Delivery destination ─────────────────────────────────────
    local dest_resource_arn=""
    if [ "$dest_type" = "CWL" ]; then
        # Vended log group — auto-created by CWL when the destination is added,
        # but we pre-create it so we can apply a sane retention policy.
        # Use lowercase log_type for the path segment for tidiness.
        local log_type_lc
        log_type_lc=$(echo "$log_type" | tr '[:upper:]' '[:lower:]')
        local log_group="/aws/vendedlogs/bedrock-agentcore/${runtime_id}/${log_type_lc}"
        if ! aws logs describe-log-groups --log-group-name-prefix "$log_group" \
                --region "$AWS_REGION" --query "logGroups[?logGroupName=='$log_group'].logGroupName" \
                --output text 2>/dev/null | grep -q "$log_group"; then
            aws logs create-log-group --log-group-name "$log_group" \
                --region "$AWS_REGION" >/dev/null 2>&1 || true
            # Bound the cost — keep 14 days of vended logs.
            aws logs put-retention-policy --log-group-name "$log_group" \
                --retention-in-days 14 --region "$AWS_REGION" >/dev/null 2>&1 || true
            print_status "Log group created: $log_group (retention 14d)"
        fi
        dest_resource_arn="arn:aws:logs:${AWS_REGION}:${account_id}:log-group:${log_group}"
    fi

    if ! aws logs get-delivery-destination --name "$destination_name" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        set +e
        if [ "$dest_type" = "CWL" ]; then
            aws logs put-delivery-destination \
                --name "$destination_name" \
                --delivery-destination-type "$dest_type" \
                --delivery-destination-configuration "destinationResourceArn=$dest_resource_arn" \
                --region "$AWS_REGION" >/dev/null 2>&1
        else
            # XRAY destinations don't take a destinationResourceArn.
            aws logs put-delivery-destination \
                --name "$destination_name" \
                --delivery-destination-type "$dest_type" \
                --region "$AWS_REGION" >/dev/null 2>&1
        fi
        local rc=$?
        set -e
        if [ $rc -ne 0 ]; then
            print_warning "Could not create delivery destination $destination_name — skipping"
            return 0
        fi
        print_status "Delivery destination created: $destination_name ($dest_type)"
    fi

    # ── 3. Bind source → destination ────────────────────────────────
    local destination_arn
    destination_arn=$(aws logs get-delivery-destination --name "$destination_name" \
        --region "$AWS_REGION" \
        --query 'deliveryDestination.arn' --output text 2>/dev/null || echo "")
    if [ -z "$destination_arn" ] || [ "$destination_arn" = "None" ]; then
        print_warning "Could not resolve delivery destination ARN for $destination_name — skipping bind"
        return 0
    fi

    # describe-deliveries doesn't filter by source name, so list and grep.
    local existing_delivery
    existing_delivery=$(aws logs describe-deliveries --region "$AWS_REGION" \
        --query "deliveries[?deliverySourceName=='$source_name'].id" \
        --output text 2>/dev/null || echo "")
    if [ -z "$existing_delivery" ] || [ "$existing_delivery" = "None" ]; then
        set +e
        aws logs create-delivery \
            --delivery-source-name "$source_name" \
            --delivery-destination-arn "$destination_arn" \
            --region "$AWS_REGION" >/dev/null 2>&1
        local rc=$?
        set -e
        if [ $rc -ne 0 ]; then
            print_warning "Could not bind delivery $source_name → $destination_name"
            return 0
        fi
        print_status "Delivery bound: $source_name → $destination_name"
    else
        print_status "Delivery already configured: $source_name → $destination_name"
    fi
}

# ── Auto-detect AgentCore role ARN ──────────────────────────────────

resolve_agentcore_role() {
    # If already set in .env or environment, use it
    if [ -n "$AGENTCORE_ROLE_ARN" ]; then
        print_status "Using AgentCore role: $AGENTCORE_ROLE_ARN"
        return 0
    fi

    # Check deployed state from agentcore CLI (new CLI)
    local deployed_state="agent/agentcore/.cli/deployed-state.json"
    if [ -f "$deployed_state" ]; then
        local role_arn
        role_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'roleArn' in a:
        print(a['roleArn']); break
" 2>/dev/null || true)
        if [ -n "$role_arn" ]; then
            AGENTCORE_ROLE_ARN="$role_arn"
            print_status "AgentCore role (from deployed state): $AGENTCORE_ROLE_ARN"
            return 0
        fi
    fi

    # Fallback: read from CloudFormation stack outputs
    local stack_name="AgentCore-patchy-default"
    local role_arn
    role_arn=$(aws cloudformation describe-stacks --stack-name "$stack_name" \
        --query 'Stacks[0].Outputs[?contains(OutputKey,`RoleArn`)].OutputValue' \
        --output text 2>/dev/null | head -1)
    if [ -n "$role_arn" ] && [ "$role_arn" != "None" ]; then
        AGENTCORE_ROLE_ARN="$role_arn"
        print_status "AgentCore role (from CloudFormation): $AGENTCORE_ROLE_ARN"
        return 0
    fi

    # Last resort: auto-detect from IAM
    echo "Detecting AgentCore role ARN from IAM..."
    role_arn=$(aws iam list-roles \
        --query 'Roles[?starts_with(RoleName,`AgentCore-`) || starts_with(RoleName,`AmazonBedrockAgentCoreSDKRuntime`)].Arn' \
        --output text 2>/dev/null | head -1)

    if [ -z "$role_arn" ] || [ "$role_arn" = "None" ]; then
        print_error "Could not find AgentCore runtime role"
        echo "  Deploy the agent first, or set AGENTCORE_ROLE_ARN in .env"
        exit 1
    fi

    AGENTCORE_ROLE_ARN="$role_arn"
    print_status "Auto-detected AgentCore role: $AGENTCORE_ROLE_ARN"
}




# ── Deploy functions ────────────────────────────────────────────────

run_eval_gate() {
    # Pre-deploy tool selection eval. Skipped if SKIP_EVAL=true.
    if [ "${SKIP_EVAL:-false}" = "true" ]; then
        print_warning "Skipping tool selection eval (SKIP_EVAL=true)"
        return 0
    fi

    local eval_script="agent/eval/run_eval.py"
    if [ ! -f "$eval_script" ]; then
        print_warning "Eval script not found ($eval_script) — skipping"
        return 0
    fi

    echo -e "${BLUE}Running tool selection eval...${NC}"
    local threshold="${EVAL_THRESHOLD:-80}"
    if python3 "$eval_script" --threshold "$threshold" --region "$AWS_REGION"; then
        print_status "Eval passed"
    else
        print_error "Eval failed (accuracy below ${threshold}%). Deploy blocked."
        echo "  Options:"
        echo "    - Fix the regression and retry"
        echo "    - Update baseline: python3 $eval_script --update-baseline"
        echo "    - Skip eval: SKIP_EVAL=true ./deploy.sh agent"
        return 1
    fi
}

deploy_agent() {
    echo -e "${BLUE}Deploying agent...${NC}"
    cd agent

    # ── Resolve npm @aws/agentcore CLI ────────────────────────────────
    # The pip package bedrock-agentcore (installed via requirements.txt for the
    # Python SDK) also provides an `agentcore` CLI entry point via the starter
    # toolkit. When the venv is active this shadows the npm CLI. Use the npm
    # binary path explicitly to avoid the conflict.
    local AGENTCORE_CLI
    AGENTCORE_CLI="$(npm prefix -g)/bin/agentcore"
    if [ ! -x "$AGENTCORE_CLI" ]; then
        echo "Installing @aws/agentcore CLI..."
        npm install -g @aws/agentcore 2>/dev/null || {
            print_error "Failed to install @aws/agentcore CLI. Run: npm install -g @aws/agentcore"
            cd ..; exit 1
        }
        print_status "AgentCore CLI installed"
    fi

    local agent_name="${AGENT_NAME:-patchy}"

    # Detect current account for stale config checks
    local deploy_account
    deploy_account=$(aws sts get-caller-identity --query Account --output text)

    # ── Migration: old pip CLI → new npm CLI ──────────────────────────
    # If .bedrock_agentcore.yaml exists (from the old pip-based agentcore CLI)
    # but no deployed-state.json, the user is upgrading. Warn them and remove
    # the old config so the npm CLI creates fresh resources.
    if [ -f ".bedrock_agentcore.yaml" ] && [ ! -f "agentcore/.cli/deployed-state.json" ]; then
        print_warning "Found legacy .bedrock_agentcore.yaml (old pip CLI)"
        print_warning "Migrating to @aws/agentcore npm CLI — old config will be archived."
        mv .bedrock_agentcore.yaml ".bedrock_agentcore.yaml.bak.$(date +%s)"
        print_status "Old config backed up. The npm CLI will create fresh resources."
    elif [ -f ".bedrock_agentcore.yaml" ]; then
        # Both old and new config exist — safe to remove the old one
        rm -f .bedrock_agentcore.yaml
    fi

    # ── Stale config detection ────────────────────────────────────────
    # If deployed-state.json references a different account (e.g., switching
    # from management account to DA account), remove it so agentcore deploy
    # creates fresh resources in the current account.
    local deployed_state="agentcore/.cli/deployed-state.json"
    if [ -f "$deployed_state" ]; then
        local state_account
        state_account=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    arn = a.get('runtimeArn','')
    parts = arn.split(':')
    if len(parts) >= 5:
        print(parts[4]); break
" 2>/dev/null || true)
        if [ -n "$state_account" ] && [ "$state_account" != "$deploy_account" ]; then
            print_warning "Stale deployed state detected (account: $state_account, current: $deploy_account)"
            print_warning "Removing stale state — agentcore deploy will create fresh resources."
            rm -rf agentcore/.cli
        fi
    fi

    # ── Resource reachability check ───────────────────────────────────
    # If deployed state exists, verify the agent runtime is still reachable.
    # Resources may be gone after a destroy/recreate cycle.
    if [ -f "$deployed_state" ]; then
        local agent_id
        agent_id=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'runtimeArn' in a:
        print(a['runtimeArn']); break
" 2>/dev/null || true)
        if [ -n "$agent_id" ]; then
            if ! "$AGENTCORE_CLI" status --json &> /dev/null 2>&1; then
                print_warning "AgentCore resources no longer reachable (runtime: $agent_id)"
                print_warning "Removing stale state — agentcore deploy will create fresh resources."
                rm -rf agentcore/.cli
            fi
        fi
    fi

    # Generate aws-targets.json from current environment
    cat > agentcore/aws-targets.json <<EOF
[
  {
    "name": "default",
    "account": "${deploy_account}",
    "region": "${AWS_REGION}"
  }
]
EOF
    print_status "AgentCore target: ${deploy_account}/${AWS_REGION}"

    # Ensure agentcore CDK dependencies are installed and match package.json.
    # The previous mtime-based check was fooled by a stale package-lock.json that
    # pinned an older @aws/agentcore-cdk (alpha.10) than package.json declared
    # (alpha.19). Old vs new alpha versions read different keys in agentcore.json
    # (alpha.10 reads `agents`, alpha.19+ reads `runtimes`), so a mismatched
    # install silently produces an empty CFN template — memory deployed, no
    # runtime. Now we verify the installed version matches and reinstall if not.
    if [ -d "agentcore/cdk" ]; then
        local _need_install=false
        if [ ! -d "agentcore/cdk/node_modules/@aws/agentcore-cdk" ]; then
            _need_install=true
        else
            local _installed_ver _wanted_ver
            _installed_ver=$(python3 -c "
import json, sys
try:
    print(json.load(open('agentcore/cdk/node_modules/@aws/agentcore-cdk/package.json'))['version'])
except Exception:
    pass
" 2>/dev/null)
            _wanted_ver=$(python3 -c "
import json, sys, re
try:
    v = json.load(open('agentcore/cdk/package.json'))['dependencies']['@aws/agentcore-cdk']
    # Strip range prefixes (^, ~, >=, etc.) for an exact-pin comparison
    print(re.sub(r'^[\\^~>=<]+', '', v))
except Exception:
    pass
" 2>/dev/null)
            if [ -z "$_installed_ver" ] || [ "$_installed_ver" != "$_wanted_ver" ]; then
                print_warning "AgentCore CDK installed=${_installed_ver:-none} but package.json wants ${_wanted_ver}"
                print_warning "Reinstalling — alpha versions read different agentcore.json keys, mismatch breaks runtime deployment"
                _need_install=true
            fi
        fi
        if [ "$_need_install" = true ]; then
            echo "Installing AgentCore CDK dependencies..."
            (cd agentcore/cdk && rm -rf node_modules dist package-lock.json && npm install --silent)
            print_status "AgentCore CDK dependencies installed"
        fi
    fi

    # First-time setup: create agentcore project if config doesn't exist
    if [ ! -f "agentcore/agentcore.json" ]; then
        print_warning "No agentcore config found — running first-time setup"
        "$AGENTCORE_CLI" create \
            --name "$agent_name" \
            --memory shortTerm \
            --defaults \
            --skip-git \
            --skip-python-setup \
            --output-dir .
        print_status "AgentCore project created"
    fi

    # Inject .env values into agentcore.json (AgentCore runtime has no .env access)
    python3 -c "
import json, os
config_path = 'agentcore/agentcore.json'
with open(config_path) as f:
    config = json.load(f)
env_var_names = [
    'AWS_REGION', 'MULTI_ACCOUNT_ENABLED',
    'SPOKE_EXECUTION_ROLE', 'SPOKE_REGIONS',
    'SPOKE_OU_IDS', 'SPOKE_ACCOUNT_IDS',
    'SSM_SCOPE_TAG_KEY', 'SSM_SCOPE_TAG_VALUE',
    'INSPECTOR_RESOURCE_TYPES', 'BEDROCK_MODEL_ID',
]
env_vars = [{'name': n, 'value': os.environ[n]} for n in env_var_names if os.environ.get(n)]
# v0.12+ uses 'runtimes', older versions used 'agents'
target = config.get('runtimes') or config.get('agents') or []
if target:
    target[0]['envVars'] = env_vars
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
"
    print_status "Agent env vars injected ($(python3 -c "
import json
c = json.load(open('agentcore/agentcore.json'))
t = c.get('runtimes') or c.get('agents') or [{}]
print(len(t[0].get('envVars',[])))
"))"

    # Deploy (creates/updates runtime, memory, IAM role, pushes code)
    "$AGENTCORE_CLI" deploy -y -v
    print_status "Agent deployed"

    # Extract role ARN and agent ARN from deployed state
    local role_arn="" agent_arn=""
    if [ -f "$deployed_state" ]; then
        role_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'roleArn' in a:
        print(a['roleArn']); break
" 2>/dev/null || true)
        agent_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'runtimeArn' in a:
        print(a['runtimeArn']); break
" 2>/dev/null || true)
    fi

    # Fallback: read CloudFormation stack outputs directly if deployed-state is empty
    if [ -z "$role_arn" ] || [ -z "$agent_arn" ]; then
        local stack_name="AgentCore-patchy-default"
        local stack_outputs
        stack_outputs=$(aws cloudformation describe-stacks --stack-name "$stack_name" \
            --query 'Stacks[0].Outputs' --output json 2>/dev/null || echo "[]")
        if [ -z "$role_arn" ]; then
            role_arn=$(echo "$stack_outputs" | python3 -c "
import json, sys
for o in json.load(sys.stdin):
    if 'RoleArn' in o.get('OutputKey',''):
        print(o['OutputValue']); break
" 2>/dev/null || true)
        fi
        if [ -z "$agent_arn" ]; then
            agent_arn=$(echo "$stack_outputs" | python3 -c "
import json, sys
for o in json.load(sys.stdin):
    if 'RuntimeArn' in o.get('OutputKey',''):
        print(o['OutputValue']); break
" 2>/dev/null || true)
        fi
        if [ -n "$role_arn" ] || [ -n "$agent_arn" ]; then
            print_warning "Deployed state was empty — extracted ARNs from CloudFormation stack outputs"
        fi
    fi

    if [ -n "$role_arn" ]; then
        AGENTCORE_ROLE_ARN="$role_arn"
        export AGENTCORE_ROLE_ARN
        print_status "AgentCore role: $AGENTCORE_ROLE_ARN"
    fi
    if [ -n "$agent_arn" ]; then
        export AGENTCORE_AGENT_ARN="$agent_arn"
        print_status "Agent ARN: $agent_arn"
    fi

    # Export memory ID so deploy_ui's CDK can wire it into the UI task
    # env. /api/session/{id}/messages uses it to rehydrate chat panel
    # from AgentCore Memory on page refresh.
    local _memory_id_export=""
    if [ -f "$deployed_state" ]; then
        _memory_id_export=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
mems = res.get('memories') or {}
for m in mems.values():
    if 'memoryId' in m:
        print(m['memoryId']); break
" 2>/dev/null || true)
    fi
    if [ -n "$_memory_id_export" ]; then
        export MEMORY_PATCHMEMORYV2_ID="$_memory_id_export"
        print_status "Memory ID: $_memory_id_export"
    fi

    cd ..
}

ensure_explorer_sync() {
    local sync_name="patchy-fleet-sync"

    # Build the desired SourceRegions list from SPOKE_REGIONS (falls back to AWS_REGION).
    # The Resource Data Sync's SourceRegions are immutable after creation — to change them
    # we must delete and recreate the sync. We compare desired vs actual below to decide.
    local regions_csv="${SPOKE_REGIONS:-$AWS_REGION}"
    local regions_json
    regions_json=$(echo "$regions_csv" | tr -d ' ' | tr ',' '\n' | awk 'NF{printf "\"%s\",", $0}' | sed 's/,$//')
    regions_json="[${regions_json}]"

    # Check existing sync's SourceRegions
    local actual_regions
    actual_regions=$(aws ssm list-resource-data-sync --sync-type SyncFromSource \
        --query "ResourceDataSyncItems[?SyncName=='${sync_name}'].SyncSource.SourceRegions" \
        --output json 2>/dev/null | tr -d ' \n')

    if [ -n "$actual_regions" ] && [ "$actual_regions" != "[]" ] && [ "$actual_regions" != "null" ]; then
        # Existing sync — compare desired vs actual (normalised, sorted)
        local desired_sorted actual_sorted
        desired_sorted=$(echo "$regions_csv" | tr ',' '\n' | tr -d ' ' | sort -u | tr '\n' ',')
        actual_sorted=$(echo "$actual_regions" | python3 -c "import sys,json; print(','.join(sorted(json.load(sys.stdin)[0])))" 2>/dev/null)
        if [ "$desired_sorted" = "${actual_sorted},"  ] || [ "$desired_sorted" = "$actual_sorted" ]; then
            print_status "Explorer sync exists: $sync_name (regions: $actual_sorted)"
            return 0
        fi
        print_warning "Explorer sync regions changed: $actual_sorted → $desired_sorted"
        print_warning "Recreating sync (SourceRegions are immutable after creation)..."
        aws ssm delete-resource-data-sync --sync-name "$sync_name" --sync-type SyncFromSource 2>/dev/null \
            || print_warning "Could not delete existing sync — recreate may fail"
        # Brief pause for the delete to settle
        sleep 3
    fi

    if [ "${MULTI_ACCOUNT_ENABLED:-false}" = "true" ]; then
        echo -e "${BLUE}Creating Explorer Resource Data Sync (cross-account, regions: $regions_csv)...${NC}"
        aws ssm create-resource-data-sync \
            --sync-name "$sync_name" \
            --sync-type SyncFromSource \
            --sync-source "{
                \"SourceType\": \"AwsOrganizations\",
                \"AwsOrganizationsSource\": {\"OrganizationSourceType\": \"EntireOrganization\"},
                \"SourceRegions\": ${regions_json},
                \"IncludeFutureRegions\": false,
                \"EnableAllOpsDataSources\": true
            }" 2>/dev/null && print_status "Explorer sync created: $sync_name" \
            || {
                echo ""
                echo -e "${RED}════════════════════════════════════════════════════════════════════${NC}"
                echo -e "${RED}  MULTI-ACCOUNT FLEET VISIBILITY UNAVAILABLE${NC}"
                echo -e "${RED}════════════════════════════════════════════════════════════════════${NC}"
                echo -e "  Explorer Resource Data Sync could not be created."
                echo -e "  Without it, the agent ${RED}cannot discover instances across spoke accounts${NC}."
                echo ""
                echo -e "  ${GREEN}Fix (run from management account):${NC}"
                echo -e "  aws ssm-quicksetup register-delegated-administrator \\"
                echo -e "      --delegated-admin-account-id ${account_id:-\$ACCOUNT_ID}"
                echo ""
                echo -e "  Then re-run: ${BLUE}./deploy.sh${NC}"
                echo -e "${RED}════════════════════════════════════════════════════════════════════${NC}"
                echo ""
            }
    else
        echo -e "${BLUE}Creating Explorer Resource Data Sync (single account, regions: $regions_csv)...${NC}"
        aws ssm create-resource-data-sync \
            --sync-name "$sync_name" \
            --sync-type SyncFromSource \
            --sync-source "{
                \"SourceType\": \"SingleAccountMultiRegions\",
                \"SourceRegions\": ${regions_json},
                \"EnableAllOpsDataSources\": true
            }" 2>/dev/null && print_status "Explorer sync created: $sync_name" \
            || print_warning "Could not create Explorer sync"
    fi
}

deploy_infra() {
    resolve_agentcore_role

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    ensure_cdk_bootstrap

    local stacks=""
    local stack_count=0

    # VPC: use existing or create new
    if [ -n "$EXISTING_VPC_ID" ]; then
        print_status "Using existing VPC: $EXISTING_VPC_ID"
        stacks="Patchy-VpcLookup"
        stack_count=1
    else
        stacks="Patchy-Network"
        stack_count=1
    fi

    # Solution stacks (always)
    stacks="$stacks Patchy-Core"
    stack_count=$((stack_count + 1))

    echo -e "${BLUE}Deploying infrastructure ($stack_count stacks)...${NC}"

    npx cdk deploy $stacks \
        --exclusively \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Infrastructure deployed ($stack_count stacks)"
    cd ..

    # Ensure Explorer Resource Data Sync exists (idempotent)
    ensure_explorer_sync
}

deploy_ui() {
    echo -e "${BLUE}Deploying Web UI (Fargate + Cognito + public ALB)...${NC}"

    # Pre-flight: agent config must exist (Docker build copies it into the image)
    if [ ! -f "agent/agentcore/agentcore.json" ]; then
        print_error "No agent config found (agent/agentcore/agentcore.json)"
        echo "  Deploy the agent first: ./deploy.sh agent"
        exit 1
    fi

    # Resolve MEMORY_PATCHMEMORYV2_ID from deployed state if not already
    # exported. Standalone './deploy.sh ui' doesn't run deploy_agent, so
    # the env var won't be set unless we look it up here. CDK reads this
    # var to wire the UI task env (used by /api/session/{id}/messages).
    if [ -z "$MEMORY_PATCHMEMORYV2_ID" ]; then
        local _ui_deployed_state="agent/agentcore/.cli/deployed-state.json"
        if [ -f "$_ui_deployed_state" ]; then
            local _ui_memory_id
            _ui_memory_id=$(python3 -c "
import json
with open('$_ui_deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
mems = res.get('memories') or {}
for m in mems.values():
    if 'memoryId' in m:
        print(m['memoryId']); break
" 2>/dev/null || true)
            if [ -n "$_ui_memory_id" ]; then
                export MEMORY_PATCHMEMORYV2_ID="$_ui_memory_id"
                print_status "Memory ID (from deployed state): $_ui_memory_id"
            fi
        fi
    fi

    # Pre-flight: frontend lockfile must exist (npm ci requires it)
    if [ ! -f "ui/frontend/package-lock.json" ]; then
        print_error "ui/frontend/package-lock.json not found"
        echo "  Run: cd ui/frontend && npm install"
        exit 1
    fi

    # ── Auto-setup TLS (required for Cognito) ──────────────────────
    # Cognito requires HTTPS. Auto-run setup-tls.sh if no cert is configured.
    # To disable Cognito and use internal ALB + bastion instead, set COGNITO_ENABLED=false in .env.
    local use_cognito=true
    if [ "${COGNITO_ENABLED:-true}" = "false" ]; then
        use_cognito=false
        print_status "Cognito disabled — deploying with internal ALB + bastion host"
    fi

    if [ "$use_cognito" = true ]; then
        if [ -z "$ACM_CERTIFICATE_ARN" ]; then
            echo -e "${BLUE}No ACM_CERTIFICATE_ARN set — generating self-signed certificate...${NC}"
            ./setup-tls.sh
            # Pick up the new ACM_CERTIFICATE_ARN without re-sourcing entire .env
            ACM_CERTIFICATE_ARN=$(grep '^ACM_CERTIFICATE_ARN=' .env 2>/dev/null | cut -d= -f2-)
        fi

        if [ -z "$ACM_CERTIFICATE_ARN" ]; then
            print_error "ACM_CERTIFICATE_ARN still not set after setup-tls.sh. Check for errors above."
            exit 1
        fi
        # Validate ACM cert belongs to the current account
        local current_account
        current_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
        local cert_account
        cert_account=$(echo "$ACM_CERTIFICATE_ARN" | cut -d: -f5)
        if [ -n "$cert_account" ] && [ "$cert_account" != "$current_account" ]; then
            print_warning "ACM_CERTIFICATE_ARN belongs to account $cert_account, not current account $current_account"
            print_warning "Clearing stale cert — setup-tls.sh will generate a new one."
            ACM_CERTIFICATE_ARN=""
            sed -i.bak 's|ACM_CERTIFICATE_ARN=.*|ACM_CERTIFICATE_ARN=|' .env 2>/dev/null; rm -f .env.bak
            ./setup-tls.sh
            ACM_CERTIFICATE_ARN=$(grep '^ACM_CERTIFICATE_ARN=' .env 2>/dev/null | cut -d= -f2-)
            if [ -z "$ACM_CERTIFICATE_ARN" ]; then
                print_error "Failed to generate new TLS certificate."
                exit 1
            fi
        fi

        export ACM_CERTIFICATE_ARN
        print_status "TLS certificate: $ACM_CERTIFICATE_ARN"

        # ── Auto-derive Cognito domain prefix ──────────────────────────
        # Domain prefix must be globally unique — use account ID as suffix.
        # Also fix stale domain prefix from a different account.
        if [ -n "$COGNITO_DOMAIN_PREFIX" ]; then
            # Check if domain prefix contains a different account ID
            local domain_account
            domain_account=$(echo "$COGNITO_DOMAIN_PREFIX" | grep -oE '[0-9]{12}' || true)
            if [ -n "$domain_account" ] && [ "$domain_account" != "$current_account" ]; then
                print_warning "COGNITO_DOMAIN_PREFIX contains stale account $domain_account — resetting"
                COGNITO_DOMAIN_PREFIX=""
                sed -i.bak "s|COGNITO_DOMAIN_PREFIX=.*|COGNITO_DOMAIN_PREFIX=patchy-${current_account}|" .env 2>/dev/null; rm -f .env.bak
            fi
        fi
        if [ -z "$COGNITO_DOMAIN_PREFIX" ]; then
            COGNITO_DOMAIN_PREFIX="patchy-${current_account}"
            echo -e "${BLUE}Auto-derived Cognito domain prefix: ${COGNITO_DOMAIN_PREFIX}${NC}"
            # Persist to .env for future deploys
            if [ -f ".env" ] && ! grep -q "COGNITO_DOMAIN_PREFIX" .env; then
                echo "" >> .env
                echo "# Cognito hosted UI domain (auto-generated)" >> .env
                echo "COGNITO_DOMAIN_PREFIX=${COGNITO_DOMAIN_PREFIX}" >> .env
            fi
        fi
        export COGNITO_DOMAIN_PREFIX
        print_status "Cognito auth enabled (domain: $COGNITO_DOMAIN_PREFIX)"
    fi

    resolve_agentcore_role

    # Build frontend on host (avoids esbuild crash under Finch/x86 emulation on Apple Silicon)
    echo -e "${BLUE}Building frontend...${NC}"
    (cd ui/frontend && npm ci --silent && npm run build)
    if [ ! -d "ui/frontend/dist" ]; then
        print_error "Frontend build failed — ui/frontend/dist not found"
        exit 1
    fi
    print_status "Frontend built ($(du -sh ui/frontend/dist | cut -f1))"

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    # Compile TypeScript before CDK deploy (CDK uses .js files, not .ts)
    npx tsc
    ensure_cdk_bootstrap

    # Clear CDK output cache to ensure fresh Docker image hash.
    # CDK's content-addressable caching can skip image rebuilds even when
    # source files change. The BUILD_TIMESTAMP build arg in ui-stack.ts
    # ensures a new hash, but stale cdk.out can still cause issues.
    rm -rf cdk.out 2>/dev/null

    # Guard: verify Cognito env vars are set before deploying.
    # Running cdk deploy without ACM_CERTIFICATE_ARN will flip cognitoEnabled=false
    # and tear down the public ALB + Cognito — a destructive, hard-to-recover mistake.
    if [ "$use_cognito" = true ] && [ -z "$ACM_CERTIFICATE_ARN" ]; then
        print_error "ACM_CERTIFICATE_ARN is not set but Cognito is enabled. This would destroy the public ALB."
        print_error "Run ./setup-tls.sh first, or set ACM_CERTIFICATE_ARN in .env"
        cd ..
        exit 1
    fi

    # Capture current task definition revision for post-deploy verification
    local pre_deploy_taskdef
    pre_deploy_taskdef=$(aws ecs describe-services \
        --cluster "$(aws ecs list-clusters --query 'clusterArns[0]' --output text 2>/dev/null | sed 's|.*/||')" \
        --services "$(aws ecs list-services --cluster "$(aws ecs list-clusters --query 'clusterArns[0]' --output text 2>/dev/null | sed 's|.*/||')" --query 'serviceArns[0]' --output text 2>/dev/null | sed 's|.*/||')" \
        --query 'services[0].taskDefinition' --output text 2>/dev/null || echo "none")

    # Ensure env vars are exported for CDK subprocess
    export ACM_CERTIFICATE_ARN COGNITO_DOMAIN_PREFIX COGNITO_ENABLED
    npx cdk deploy Patchy-UI \
        --exclusively \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"

    # Verify deployment actually updated the task definition
    local post_deploy_taskdef
    post_deploy_taskdef=$(aws ecs describe-services \
        --cluster "$(aws ecs list-clusters --query 'clusterArns[0]' --output text 2>/dev/null | sed 's|.*/||')" \
        --services "$(aws ecs list-services --cluster "$(aws ecs list-clusters --query 'clusterArns[0]' --output text 2>/dev/null | sed 's|.*/||')" --query 'serviceArns[0]' --output text 2>/dev/null | sed 's|.*/||')" \
        --query 'services[0].taskDefinition' --output text 2>/dev/null || echo "none")

    if [ "$pre_deploy_taskdef" = "$post_deploy_taskdef" ] && [ "$pre_deploy_taskdef" != "none" ]; then
        print_warning "Task definition unchanged ($post_deploy_taskdef) — CDK may have skipped the image rebuild."
        print_warning "If you changed server.py or frontend code, try: rm -rf infra/cdk.out && ./deploy.sh ui"
    else
        print_status "Web UI deployed (task definition: $post_deploy_taskdef)"
    fi

    # ── Fix Cognito callback URLs (lowercase ALB DNS) ──────────────
    # ALB DNS names are mixed-case but browsers lowercase them.
    # Cognito compares callback URLs case-sensitively, so we add the
    # lowercase variant post-deploy (CloudFormation has no lowercase intrinsic).
    if [ "$use_cognito" = true ]; then
        echo -e "${BLUE}Fixing Cognito callback URLs (lowercase ALB DNS)...${NC}"
        local pool_id client_id alb_dns alb_dns_lower
        pool_id=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text 2>/dev/null)
        client_id=$(aws cognito-idp list-user-pool-clients --user-pool-id "$pool_id" \
            --query 'UserPoolClients[0].ClientId' --output text 2>/dev/null)
        alb_dns=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text 2>/dev/null | sed 's|https://||')
        alb_dns_lower=$(echo "$alb_dns" | tr '[:upper:]' '[:lower:]')
        if [ -n "$pool_id" ] && [ -n "$client_id" ] && [ -n "$alb_dns" ]; then
            aws cognito-idp update-user-pool-client \
                --user-pool-id "$pool_id" \
                --client-id "$client_id" \
                --supported-identity-providers COGNITO \
                --allowed-o-auth-flows code \
                --allowed-o-auth-scopes openid email profile \
                --allowed-o-auth-flows-user-pool-client \
                --callback-urls "https://${alb_dns}/oauth2/idpresponse" "https://${alb_dns_lower}/oauth2/idpresponse" \
                --logout-urls "https://${alb_dns}" "https://${alb_dns_lower}" "https://${alb_dns}/signed-out" "https://${alb_dns_lower}/signed-out" \
                > /dev/null 2>&1
            print_status "Cognito callback URLs updated (added lowercase: ${alb_dns_lower})"

            # Switch domain to managed login v2 (modern branding).
            # CloudFormation rejects this property on existing domains, so we set it via CLI.
            # Idempotent — safe to run on every deploy.
            local domain_prefix
            domain_prefix=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
                --query 'Stacks[0].Outputs[?OutputKey==`CognitoDomainPrefix`].OutputValue' --output text 2>/dev/null)
            if [ -n "$domain_prefix" ]; then
                aws cognito-idp update-user-pool-domain \
                    --user-pool-id "$pool_id" \
                    --domain "$domain_prefix" \
                    --managed-login-version 2 \
                    > /dev/null 2>&1
                print_status "Managed login v2 enabled"
            fi

            # Create default managed login branding style if none exists.
            # Required for managed login v2 (ManagedLoginVersion=2 set by CDK).
            # Idempotent — silently skips if branding already exists.
            aws cognito-idp create-managed-login-branding \
                --user-pool-id "$pool_id" \
                --client-id "$client_id" \
                --use-cognito-provided-values \
                > /dev/null 2>&1 || true
            print_status "Managed login branding configured"
        else
            print_warning "Could not fix Cognito callbacks — update manually if login fails"
        fi
    fi

    echo ""
    if [ "$use_cognito" = true ]; then
        echo -e "${GREEN}UI is publicly accessible with Cognito authentication.${NC}"
        local ui_url
        ui_url=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text 2>/dev/null)
        echo -e "URL: ${BLUE}${ui_url}${NC}"
    else
        echo -e "${GREEN}UI is on an internal ALB (not publicly accessible).${NC}"
        echo -e "Connect via SSM port forwarding: ${BLUE}./connect-ui.sh${NC}"
        echo ""
        local ui_url
        ui_url=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text 2>/dev/null)
        echo -e "Internal ALB DNS: ${ui_url}"
    fi
    cd ..
}

deploy_spoke() {
    echo -e "${BLUE}Deploying spoke IAM role via StackSet...${NC}"
    # Hub is included in the target list. SSM Automation TargetLocations
    # requires PatchySpokeRole to exist in every account it fans out to —
    # including the hub itself, even though the hub uses local credentials
    # for everything else (the agent's get_credentials short-circuits the
    # hub case via cross_account.py).
    _deploy_stackset \
        --stack-set "Patchy-SpokeIam" \
        --cdk-stack "Patchy-SpokeIam" \
        --regions "$AWS_REGION" \
        --filter-hub false \
        --description "Patchy spoke IAM role for cross-account patch operations"
}

deploy_docs() {
    echo -e "${BLUE}Deploying SSM Automation documents via StackSet...${NC}"
    # Docs go to every (account, region) the agent fans out into — including the hub.
    # Hub is NOT filtered out so its hub-region docs come from this StackSet too.
    local docs_regions="${SPOKE_REGIONS:-$AWS_REGION}"
    _deploy_stackset \
        --stack-set "Patchy-SsmDocs" \
        --cdk-stack "Patchy-SsmDocs" \
        --regions "$docs_regions" \
        --filter-hub false \
        --description "Patchy SSM Automation documents for cross-account patching"
}

# ── _deploy_stackset: shared StackSet deploy logic ─────────────────
# Synthesises a CDK stack and creates/updates a CloudFormation StackSet.
# Used by both deploy_spoke (IAM role) and deploy_docs (SSM documents).
#
# Required flags:
#   --stack-set <name>      CloudFormation StackSet name
#   --cdk-stack <id>        CDK stack ID to synthesise
#   --regions <csv>         Comma-separated region list (single region = primary only)
#   --filter-hub true|false Whether to drop the hub account from target list
#   --description <text>    StackSet description
_deploy_stackset() {
    local stack_set_name="" cdk_stack_id="" regions_csv=""
    local filter_hub="false" stackset_description=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --stack-set)   stack_set_name="$2"; shift 2 ;;
            --cdk-stack)   cdk_stack_id="$2"; shift 2 ;;
            --regions)     regions_csv="$2"; shift 2 ;;
            --filter-hub)  filter_hub="$2"; shift 2 ;;
            --description) stackset_description="$2"; shift 2 ;;
            *) print_error "_deploy_stackset: unknown flag: $1"; return 1 ;;
        esac
    done

    if [ -z "$stack_set_name" ] || [ -z "$cdk_stack_id" ] || [ -z "$regions_csv" ]; then
        print_error "_deploy_stackset: missing required flag (--stack-set, --cdk-stack, --regions)"
        return 1
    fi

    local spoke_role="${SPOKE_EXECUTION_ROLE:-PatchySpokeRole}"
    local account_id
    account_id=$(aws sts get-caller-identity --query Account --output text)
    local synth_dir="/tmp/patchy-${cdk_stack_id}-cdk.out"
    local template_path="${synth_dir}/${cdk_stack_id}.template.json"

    local regions_json
    regions_json=$(echo "$regions_csv" | tr -d ' ' | tr ',' '\n' | awk '{printf "\"%s\",", $0}' | sed 's/,$//')
    regions_json="[${regions_json}]"

    # Verify caller is management account or delegated administrator.
    # DA for Quick Setup auto-grants DA for CloudFormation StackSets + Explorer.
    local org_mgmt_id call_as=""
    org_mgmt_id=$(aws organizations describe-organization \
        --query 'Organization.MasterAccountId' --output text 2>/dev/null || true)
    if [ -n "$org_mgmt_id" ] && [ "$account_id" != "$org_mgmt_id" ]; then
        local is_da
        is_da=$(aws organizations list-delegated-administrators \
            --service-principal ssm.amazonaws.com \
            --query "DelegatedAdministrators[?Id=='${account_id}'].Id" --output text 2>/dev/null || true)
        if [ -z "$is_da" ]; then
            print_error "Account $account_id is not the management account ($org_mgmt_id) or a delegated administrator for SSM."
            echo "  Register as DA: aws ssm-quicksetup register-delegated-administrator --delegated-admin-account-id $account_id"
            exit 1
        fi
        call_as="--call-as DELEGATED_ADMIN"
        print_status "Running as delegated administrator ($account_id)"
    fi

    # ── Resolve target accounts/OUs ────────────────────────────────────
    # Priority: env vars > interactive prompt
    local target_accounts="" target_ous="" target_mode=""

    if [ -n "${SPOKE_OU_IDS:-}" ]; then
        target_ous="$SPOKE_OU_IDS"
        target_mode="ou"
        echo "Using SPOKE_OU_IDS from config: $target_ous"
    elif [ -n "${SPOKE_ACCOUNT_IDS:-}" ]; then
        target_accounts="$SPOKE_ACCOUNT_IDS"
        target_mode="account"
        echo "Using SPOKE_ACCOUNT_IDS from config: $target_accounts"
    else
        echo ""
        echo -e "${YELLOW}Choose deployment target:${NC}"
        echo "  1) Specific account IDs"
        echo "  2) Organization OU IDs (auto-deploys to new accounts joining the OU)"
        echo "  3) All non-hub accounts in organization (auto-discover)"
        echo ""
        read -p "Choice (1, 2, or 3): " target_choice

        case "$target_choice" in
            1)
                read -p "Account IDs (comma-separated): " target_accounts
                target_accounts=$(echo "$target_accounts" | tr -d ' ')
                if [ -z "$target_accounts" ]; then
                    print_error "No account IDs provided"
                    exit 1
                fi
                target_mode="account"
                ;;
            2)
                read -p "OU IDs (comma-separated, e.g. ou-abc123,ou-def456): " target_ous
                target_ous=$(echo "$target_ous" | tr -d ' ')
                if [ -z "$target_ous" ]; then
                    print_error "No OU IDs provided"
                    exit 1
                fi
                target_mode="ou"
                ;;
            3)
                echo "Discovering spoke accounts from AWS Organizations..."
                target_accounts=$(aws organizations list-accounts --no-paginate \
                    --query "Accounts[?Id!='${account_id}' && Status=='ACTIVE'].Id" \
                    --output text 2>/dev/null | tr '\t' ',')
                if [ -z "$target_accounts" ]; then
                    print_error "No spoke accounts found in organization."
                    exit 1
                fi
                local acct_count
                acct_count=$(echo "$target_accounts" | tr ',' '\n' | wc -l | xargs)
                echo "Found $acct_count spoke account(s): $target_accounts"
                read -p "Deploy to all of these? (yes/no): " confirm
                if [ "$confirm" != "yes" ]; then
                    print_warning "Cancelled"
                    exit 0
                fi
                target_mode="account"
                ;;
            *)
                print_error "Invalid choice"
                exit 1
                ;;
        esac
    fi

    # ── Filter hub account out of target_accounts (when requested) ────
    # The IAM StackSet (Patchy-SpokeIam) filters the hub because the hub uses
    # its own AgentCore role, not PatchySpokeRole. The Docs StackSet
    # (Patchy-SsmDocs) does NOT filter the hub — the hub needs the SSM docs
    # locally to start the parent Automation execution.
    if [ "$filter_hub" = "true" ] && [ "$target_mode" = "account" ] && [ -n "$target_accounts" ]; then
        local filtered_accounts=""
        local hub_in_targets=false
        for acct in $(echo "$target_accounts" | tr ',' '\n'); do
            [ -z "$acct" ] && continue
            if [ "$acct" = "$account_id" ]; then
                hub_in_targets=true
                continue
            fi
            filtered_accounts="${filtered_accounts:+${filtered_accounts},}${acct}"
        done
        if [ "$hub_in_targets" = true ]; then
            print_warning "Hub account ($account_id) was in SPOKE_ACCOUNT_IDS — skipping (the hub does not need PatchySpokeRole)."
        fi
        if [ -z "$filtered_accounts" ]; then
            print_error "No spoke accounts left after filtering hub. Set SPOKE_ACCOUNT_IDS to non-hub accounts only."
            exit 1
        fi
        target_accounts="$filtered_accounts"
    fi

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi

    echo "Synthesizing template for $cdk_stack_id (hub account: $account_id)..."
    export SPOKE_EXECUTION_ROLE="$spoke_role"
    export HUB_ACCOUNT_ID="$account_id"
    rm -rf "$synth_dir"
    npx cdk synth "$cdk_stack_id" \
        -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}" \
        -c hubAccountId="$account_id" \
        --quiet \
        -o "$synth_dir"

    if [ ! -s "$template_path" ]; then
        print_error "Failed to synthesize template for $cdk_stack_id"
        cd ..
        exit 1
    fi
    print_status "Template synthesized: $template_path"

    # ── Wait for any in-progress StackSet operation (limit: 1 concurrent) ──
    _wait_stackset_idle() {
        local ss_name=$1 max_wait=120 waited=0
        while [ $waited -lt $max_wait ]; do
            local running
            running=$(aws cloudformation list-stack-set-operations \
                --stack-set-name "$ss_name" $call_as \
                --query "Summaries[?Status=='RUNNING'].OperationId" \
                --output text 2>/dev/null || true)
            [ -z "$running" ] && return 0
            echo "  Waiting for in-progress StackSet operation to complete..."
            sleep 10
            waited=$((waited + 10))
        done
        print_warning "StackSet operation still running after ${max_wait}s — proceeding anyway"
    }

    # ── Verify a StackSet operation reached a terminal SUCCEEDED state ──
    # Polls describe-stack-set-operation every 15 s. On FAILED / STOPPED, dumps
    # per-instance failure reasons and returns non-zero. Set STACKSET_WAIT=false
    # to skip the wait (dev iteration); default behaviour is to verify.
    _verify_stackset_operation() {
        local ss_name="$1" op_id="$2" label="${3:-operation}"
        if [ -z "$op_id" ] || [ "$op_id" = "None" ]; then
            print_warning "$ss_name: no operation id returned for $label — cannot verify"
            return 0
        fi
        if [ "${STACKSET_WAIT:-true}" = "false" ]; then
            print_warning "$ss_name: skipping verification of $label op $op_id (STACKSET_WAIT=false)"
            return 0
        fi
        local max_wait=1800 waited=0   # 30 min cap for large fleets
        echo -n "  Verifying $ss_name $label (op $op_id)"
        while [ $waited -lt $max_wait ]; do
            local status
            status=$(aws cloudformation describe-stack-set-operation \
                --stack-set-name "$ss_name" $call_as \
                --operation-id "$op_id" \
                --query 'StackSetOperation.Status' --output text 2>/dev/null || echo "UNKNOWN")
            case "$status" in
                SUCCEEDED)
                    echo ""
                    print_status "$ss_name $label SUCCEEDED"
                    return 0
                    ;;
                FAILED|STOPPED)
                    echo ""
                    print_error "$ss_name $label $status (op $op_id)"
                    # Dump per-instance failure reasons (max 20 to keep output sane)
                    aws cloudformation list-stack-set-operation-results \
                        --stack-set-name "$ss_name" $call_as \
                        --operation-id "$op_id" --max-items 20 \
                        --query 'Summaries[?Status!=`SUCCEEDED`].[Account,Region,Status,StatusReason]' \
                        --output table 2>/dev/null || true
                    return 1
                    ;;
                RUNNING|QUEUED|PENDING|STOPPING)
                    printf '.'
                    ;;
            esac
            sleep 15
            waited=$((waited + 15))
        done
        echo ""
        print_warning "$ss_name $label still running after ${max_wait}s — check console"
        return 1
    }

    # ── Helper: format account list as JSON array ──
    _accounts_to_json() {
        echo "$1" | tr ',' '\n' | awk '{printf "\"%s\",", $0}' | sed 's/,$//'
    }

    # ── Helper: resolve parent OUs for a list of account IDs ──
    # SERVICE_MANAGED StackSets require OrganizationalUnitIds even when
    # targeting specific accounts (AccountFilterType=INTERSECTION).
    _resolve_parent_ous() {
        local accounts="$1"
        local ous=""
        for acct in $(echo "$accounts" | tr ',' '\n'); do
            local parent_ou
            parent_ou=$(aws organizations list-parents --child-id "$acct" \
                --query 'Parents[0].Id' --output text 2>/dev/null || true)
            if [ -n "$parent_ou" ] && [ "$parent_ou" != "None" ]; then
                ous="${ous:+${ous},}${parent_ou}"
            fi
        done
        echo "$ous" | tr ',' '\n' | sort -u | tr '\n' ',' | sed 's/,$//'
    }

    # ── Deploy or update StackSet ──────────────────────────────────────
    local stackset_exists=false
    if aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" $call_as &>/dev/null; then
        stackset_exists=true
    elif [ -n "$call_as" ] && aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" &>/dev/null; then
        print_error "StackSet '$stack_set_name' exists as SELF_MANAGED (created outside DA)."
        echo "  DA cannot manage SELF_MANAGED StackSets. Delete it first:"
        echo "  aws cloudformation delete-stack-set --stack-set-name $stack_set_name"
        echo "  (You may need to delete stack instances first)"
        exit 1
    fi

    if [ "$stackset_exists" = true ]; then
        echo "Updating existing StackSet: $stack_set_name"
        _wait_stackset_idle "$stack_set_name"
        local update_op_id
        update_op_id=$(aws cloudformation update-stack-set \
            --stack-set-name "$stack_set_name" $call_as \
            --template-body "file://${template_path}" \
            --capabilities CAPABILITY_NAMED_IAM \
            --operation-preferences "$_STACKSET_OP_PREFS" \
            --query 'OperationId' --output text 2>/dev/null || true)
        print_status "StackSet template update started: $stack_set_name (op $update_op_id)"
        _verify_stackset_operation "$stack_set_name" "$update_op_id" "template update" || { cd ..; exit 1; }

        if [ "$target_mode" = "account" ]; then
            local existing_accounts new_accounts=""
            existing_accounts=$(aws cloudformation list-stack-instances \
                --stack-set-name "$stack_set_name" $call_as --no-paginate \
                --query 'Summaries[].Account' --output text 2>/dev/null | tr '\t' '\n' | sort -u)
            for acct in $(echo "$target_accounts" | tr ',' '\n'); do
                if ! echo "$existing_accounts" | grep -q "^${acct}$"; then
                    new_accounts="${new_accounts:+${new_accounts},}${acct}"
                fi
            done
            if [ -n "$new_accounts" ]; then
                _wait_stackset_idle "$stack_set_name"
                local add_op_id
                if [ -n "$call_as" ]; then
                    local new_parent_ous
                    new_parent_ous=$(_resolve_parent_ous "$new_accounts")
                    local new_parent_ou_json
                    new_parent_ou_json=$(echo "$new_parent_ous" | tr ',' '\n' | awk '{printf "\"%s\",", $0}' | sed 's/,$//')
                    add_op_id=$(aws cloudformation create-stack-instances \
                        --stack-set-name "$stack_set_name" $call_as \
                        --deployment-targets "OrganizationalUnitIds=[${new_parent_ou_json}],Accounts=[$(_accounts_to_json "$new_accounts")],AccountFilterType=INTERSECTION" \
                        --regions "$regions_json" \
                        --operation-preferences "$_STACKSET_OP_PREFS" \
                        --query 'OperationId' --output text 2>/dev/null || true)
                else
                    add_op_id=$(aws cloudformation create-stack-instances \
                        --stack-set-name "$stack_set_name" \
                        --accounts "[$(_accounts_to_json "$new_accounts")]" \
                        --regions "$regions_json" \
                        --operation-preferences "$_STACKSET_OP_PREFS" \
                        --query 'OperationId' --output text 2>/dev/null || true)
                fi
                print_status "New stack instances deploying to: $new_accounts (op $add_op_id)"
                _verify_stackset_operation "$stack_set_name" "$add_op_id" "new instances" || { cd ..; exit 1; }
            else
                print_status "All target accounts already have stack instances"
            fi
        fi
    else
        echo "Creating StackSet: $stack_set_name"
        if [ "$target_mode" = "ou" ]; then
            local ou_json
            ou_json=$(echo "$target_ous" | tr ',' '\n' | awk '{printf "\"%s\",", $0}' | sed 's/,$//')

            aws cloudformation create-stack-set \
                --stack-set-name "$stack_set_name" $call_as \
                --template-body "file://${template_path}" \
                --capabilities CAPABILITY_NAMED_IAM \
                --permission-model SERVICE_MANAGED \
                --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
                --description "$stackset_description"
            print_status "StackSet created: $stack_set_name (service-managed, auto-deploys to new accounts)"

            local create_op_id
            create_op_id=$(aws cloudformation create-stack-instances \
                --stack-set-name "$stack_set_name" $call_as \
                --deployment-targets "OrganizationalUnitIds=[${ou_json}]" \
                --regions "$regions_json" \
                --operation-preferences "$_STACKSET_OP_PREFS" \
                --query 'OperationId' --output text 2>/dev/null || true)
            print_status "Stack instances deploying to OUs: $target_ous (op $create_op_id)"
            _verify_stackset_operation "$stack_set_name" "$create_op_id" "initial deploy" || { cd ..; exit 1; }
        else
            if [ -n "$call_as" ]; then
                # DA must use SERVICE_MANAGED — target accounts via OU+Account intersection
                local parent_ous
                parent_ous=$(_resolve_parent_ous "$target_accounts")
                if [ -z "$parent_ous" ]; then
                    print_error "Could not resolve parent OUs for accounts: $target_accounts"
                    echo "  SERVICE_MANAGED StackSets require OrganizationalUnitIds."
                    echo "  Use SPOKE_OU_IDS instead, or ensure accounts exist in the organization."
                    cd ..; exit 1
                fi
                local parent_ou_json
                parent_ou_json=$(echo "$parent_ous" | tr ',' '\n' | awk '{printf "\"%s\",", $0}' | sed 's/,$//')

                aws cloudformation create-stack-set \
                    --stack-set-name "$stack_set_name" $call_as \
                    --template-body "file://${template_path}" \
                    --capabilities CAPABILITY_NAMED_IAM \
                    --permission-model SERVICE_MANAGED \
                    --auto-deployment Enabled=false \
                    --description "$stackset_description"
                print_status "StackSet created: $stack_set_name (service-managed, DA)"

                local create_op_id
                create_op_id=$(aws cloudformation create-stack-instances \
                    --stack-set-name "$stack_set_name" $call_as \
                    --deployment-targets "OrganizationalUnitIds=[${parent_ou_json}],Accounts=[$(_accounts_to_json "$target_accounts")],AccountFilterType=INTERSECTION" \
                    --regions "$regions_json" \
                    --operation-preferences "$_STACKSET_OP_PREFS" \
                    --query 'OperationId' --output text 2>/dev/null || true)
                print_status "Stack instances deploying (op $create_op_id)"
                _verify_stackset_operation "$stack_set_name" "$create_op_id" "initial deploy" || { cd ..; exit 1; }
            else
                # Management account — use SELF_MANAGED with direct account targeting
                aws cloudformation create-stack-set \
                    --stack-set-name "$stack_set_name" \
                    --template-body "file://${template_path}" \
                    --capabilities CAPABILITY_NAMED_IAM \
                    --description "$stackset_description"
                print_status "StackSet created: $stack_set_name"

                local create_op_id
                create_op_id=$(aws cloudformation create-stack-instances \
                    --stack-set-name "$stack_set_name" \
                    --accounts "[$(_accounts_to_json "$target_accounts")]" \
                    --regions "$regions_json" \
                    --operation-preferences "$_STACKSET_OP_PREFS" \
                    --query 'OperationId' --output text 2>/dev/null || true)
                print_status "Stack instances deploying to: $target_accounts (op $create_op_id)"
                _verify_stackset_operation "$stack_set_name" "$create_op_id" "initial deploy" || { cd ..; exit 1; }
            fi
        fi
    fi

    rm -rf "$synth_dir"
    cd ..

    local scope_key="${SSM_SCOPE_TAG_KEY:-PatchAutomation}"
    local scope_val="${SSM_SCOPE_TAG_VALUE:-enabled}"

    echo ""
    echo -e "${GREEN}$stack_set_name deployed.${NC}"
    echo "  Regions: ${regions_csv}"
    echo "  Inspect: aws cloudformation list-stack-instances --stack-set-name $stack_set_name"

    # Tagging guidance is only relevant for the IAM StackSet — that's what
    # gates instance discovery. The docs StackSet has no tagging implications.
    if [ "$stack_set_name" = "Patchy-SpokeIam" ]; then
        echo ""
        echo -e "${YELLOW}Required: tag spoke account instances for the agent to discover them:${NC}"
        echo "  Tag: ${scope_key}=${scope_val}    (scope — instances without this tag are invisible)"
        echo "  Tag: Environment=dev|staging|prod  (routing — determines which environment)"
        echo "  Tag: PatchGroup=<group-name>       (baseline association)"
    fi
}

destroy_spoke() {
    # Tear down the Patchy-SpokeIam StackSet plus the legacy Patchy-SpokeRole
    # StackSet (from before the IAM/docs split, if it still exists).
    _destroy_stackset "Patchy-SpokeIam"
    _destroy_stackset "Patchy-SpokeRole"
}

destroy_docs() {
    # Tear down the Patchy-SsmDocs StackSet.
    _destroy_stackset "Patchy-SsmDocs"
}

_destroy_stackset() {
    # Tear down a single StackSet across all target accounts/OUs.
    # Two-step: delete stack instances first (they don't auto-delete), then
    # delete the empty StackSet. Honours both DA (DELEGATED_ADMIN) and
    # management-account paths.
    local stack_set_name="$1"
    if [ -z "$stack_set_name" ]; then
        print_error "_destroy_stackset: missing stack set name"
        return 1
    fi

    local account_id
    account_id=$(aws sts get-caller-identity --query Account --output text)

    # Detect DA vs management-account context
    local org_mgmt_id call_as=""
    org_mgmt_id=$(aws organizations describe-organization \
        --query 'Organization.MasterAccountId' --output text 2>/dev/null || true)
    if [ -n "$org_mgmt_id" ] && [ "$account_id" != "$org_mgmt_id" ]; then
        call_as="--call-as DELEGATED_ADMIN"
    fi

    # Probe — does the StackSet exist?
    local stackset_exists=false
    if aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" $call_as &>/dev/null; then
        stackset_exists=true
    elif [ -n "$call_as" ] && aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" &>/dev/null; then
        # Created as SELF_MANAGED before — try without DA flag
        call_as=""
        stackset_exists=true
    fi

    if [ "$stackset_exists" != true ]; then
        print_status "StackSet $stack_set_name not found — nothing to destroy"
        return 0
    fi

    echo -e "${RED}Tearing down StackSet: $stack_set_name${NC}"

    # Wait until any in-flight operation finishes
    _wait_stackset_idle_for_destroy() {
        local ss_name=$1 max_wait=300 waited=0
        while [ $waited -lt $max_wait ]; do
            local running
            running=$(aws cloudformation list-stack-set-operations \
                --stack-set-name "$ss_name" $call_as \
                --query "Summaries[?Status=='RUNNING'].OperationId" \
                --output text 2>/dev/null || true)
            [ -z "$running" ] && return 0
            echo "  Waiting for in-progress StackSet operation ($running)..."
            sleep 15
            waited=$((waited + 15))
        done
        print_warning "StackSet operation still running after ${max_wait}s — proceeding anyway"
    }
    _wait_stackset_idle_for_destroy "$stack_set_name"

    # Enumerate stack instances grouped by (account, region) and (deployment_target, region)
    # SERVICE_MANAGED instances live in OUs and require DeploymentTargets to delete.
    local instance_rows
    instance_rows=$(aws cloudformation list-stack-instances \
        --stack-set-name "$stack_set_name" $call_as --no-paginate \
        --query 'Summaries[].[Account,Region,OrganizationalUnitId]' --output text 2>/dev/null || true)

    if [ -z "$instance_rows" ]; then
        echo "  No stack instances to delete"
    else
        local count
        count=$(echo "$instance_rows" | wc -l | tr -d ' ')
        echo "  Found $count stack instance(s) — deleting (retain stacks: false)..."

        # Permission model determines the delete payload
        local perm_model
        perm_model=$(aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" $call_as \
            --query 'StackSet.PermissionModel' --output text 2>/dev/null || echo "")

        # Group by region for one delete-stack-instances call per region
        local regions
        regions=$(echo "$instance_rows" | awk '{print $2}' | sort -u)
        for rgn in $regions; do
            local accounts_in_region ous_in_region
            accounts_in_region=$(echo "$instance_rows" | awk -v r="$rgn" '$2==r {print $1}' | sort -u | tr '\n' ',' | sed 's/,$//')
            ous_in_region=$(echo "$instance_rows" | awk -v r="$rgn" '$2==r && $3!="None" && $3!="" {print $3}' | sort -u | tr '\n' ',' | sed 's/,$//')

            if [ "$perm_model" = "SERVICE_MANAGED" ]; then
                # Service-managed: must delete via DeploymentTargets
                if [ -n "$ous_in_region" ]; then
                    local ous_json accounts_json
                    ous_json=$(echo "$ous_in_region" | tr ',' '\n' | awk 'NF{printf "\"%s\",", $0}' | sed 's/,$//')
                    accounts_json=$(echo "$accounts_in_region" | tr ',' '\n' | awk 'NF{printf "\"%s\",", $0}' | sed 's/,$//')
                    echo "  Deleting instances in $rgn (OUs: $ous_in_region; accounts: $accounts_in_region)"
                    aws cloudformation delete-stack-instances \
                        --stack-set-name "$stack_set_name" $call_as \
                        --deployment-targets "OrganizationalUnitIds=[${ous_json}],Accounts=[${accounts_json}],AccountFilterType=INTERSECTION" \
                        --regions "[\"$rgn\"]" \
                        --no-retain-stacks \
                        --operation-preferences "$_STACKSET_OP_PREFS" \
                        > /dev/null
                fi
            else
                # Self-managed: delete by account list
                local accounts_json
                accounts_json=$(echo "$accounts_in_region" | tr ',' '\n' | awk 'NF{printf "\"%s\",", $0}' | sed 's/,$//')
                echo "  Deleting instances in $rgn (accounts: $accounts_in_region)"
                aws cloudformation delete-stack-instances \
                    --stack-set-name "$stack_set_name" \
                    --accounts "[${accounts_json}]" \
                    --regions "[\"$rgn\"]" \
                    --no-retain-stacks \
                    --operation-preferences "$_STACKSET_OP_PREFS" \
                    > /dev/null
            fi

            # Wait for this delete to complete before queuing the next (StackSet allows 1 op at a time)
            _wait_stackset_idle_for_destroy "$stack_set_name"
        done

        print_status "Stack instances deleted"
    fi

    # Now delete the empty StackSet
    echo "  Deleting empty StackSet..."
    aws cloudformation delete-stack-set --stack-set-name "$stack_set_name" $call_as 2>&1 | grep -v "^$" || true

    # Verify it's gone
    if aws cloudformation describe-stack-set --stack-set-name "$stack_set_name" $call_as &>/dev/null; then
        print_warning "StackSet still exists — may have residual instances. Inspect: aws cloudformation describe-stack-set --stack-set-name $stack_set_name $call_as"
        return 1
    fi
    print_status "StackSet $stack_set_name destroyed"
    return 0
}

destroy() {
    local mode=${1:-all}     # 'all' (default) | 'spoke' | 'docs'

    if [ "$mode" = "spoke" ]; then
        echo -e "${RED}Destroying spoke IAM StackSet only...${NC}"
        read -p "This removes Patchy-SpokeIam (and the legacy Patchy-SpokeRole if present) from every spoke account. Confirm (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            print_warning "Cancelled"
            exit 0
        fi
        destroy_spoke
        return 0
    fi

    if [ "$mode" = "docs" ]; then
        echo -e "${RED}Destroying SSM documents StackSet only...${NC}"
        read -p "This removes Patchy-SsmDocs (and the SSM Automation documents) from every (account, region) target. Confirm (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            print_warning "Cancelled"
            exit 0
        fi
        destroy_docs
        return 0
    fi

    # Pre-check: Patchy-SampleEnv imports private-subnet refs from Patchy-Network.
    # Tearing down Network while SampleEnv is still up fails mid-destroy with a
    # cryptic CFN "export in use" error after Patchy-Core is already gone.
    # Fail fast so the operator can run ./sample-env.sh destroy first.
    if aws cloudformation describe-stacks --stack-name "Patchy-SampleEnv" >/dev/null 2>&1; then
        print_error "Patchy-SampleEnv is still deployed and depends on Patchy-Network exports."
        echo "  Tear down the sample environment first, then re-run destroy:"
        echo "    ${BLUE}./sample-env.sh destroy${NC}"
        echo "    ${BLUE}./deploy.sh destroy${NC}"
        exit 1
    fi

    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi

    echo -e "${RED}Destroying ALL infrastructure...${NC}"
    echo -e "${RED}This includes:${NC}"
        echo "  - All CDK stacks (Network, Core, UI, AgentCore-patchy-default)"
        echo "  - AgentCore runtime, memory, ECR repo"
        echo "  - S3 compliance bucket and ALL reports"
        echo "  - Cognito user pool and ALL users"

        # Spoke + Docs StackSets — opt-in
        if [ "${DESTROY_SPOKE_STACKSET}" = "true" ]; then
            echo "  - Patchy-SpokeIam StackSet and instances in spoke accounts"
            echo "  - Patchy-SsmDocs StackSet and instances in (hub + spoke) accounts/regions"
            echo "  - Legacy Patchy-SpokeRole StackSet (if present from older deploys)"
        else
            echo -e "  ${YELLOW}- Patchy-SpokeIam + Patchy-SsmDocs StackSets will be PRESERVED${NC} (org-wide blast radius)"
            echo "    To remove: DESTROY_SPOKE_STACKSET=true ./deploy.sh destroy"
            echo "    Or:       ./deploy.sh destroy --spoke-only   (IAM StackSet only)"
            echo "              ./deploy.sh destroy --docs-only    (SSM docs StackSet only)"
        fi

        # Resource Data Sync — opt-in
        if [ "${DESTROY_FLEET_SYNC}" = "true" ]; then
            echo "  - SSM Resource Data Sync 'patchy-fleet-sync'"
        else
            echo -e "  ${YELLOW}- SSM Resource Data Sync 'patchy-fleet-sync' will be PRESERVED${NC}"
            echo "    Re-creation triggers a multi-hour ingestion window."
            echo "    To remove: DESTROY_FLEET_SYNC=true ./deploy.sh destroy"
        fi

        echo ""
        read -p "Are you sure? (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            print_warning "Cancelled"
            cd ..
            exit 0
        fi

        # Step 1: Destroy AgentCore runtime (agent, memory, IAM roles)
        # Per AWS docs (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html#stop-session-or-clean-up):
        #   1. `agentcore remove all` resets local schemas only
        #   2. `agentcore deploy` synthesizes the now-empty CDK app and tears down AWS resources
        # The CLI must be run from `agent/` (the project root containing the agentcore/ config folder).
        # If either step fails, fall back to direct CloudFormation delete.
        local agent_stack_name="AgentCore-${PROJECT_NAME:-patchy}-${AGENTCORE_TARGET:-default}"
        local agentcore_destroyed=false
        cd ../agent
        local _ac_cli
        _ac_cli="$(npm prefix -g)/bin/agentcore"
        if [ -x "$_ac_cli" ] && [ -f "agentcore/agentcore.json" ]; then
            # Back up agentcore.json before `remove all` empties it — needed for
            # the next deploy. The file ships with the solution and contains the
            # runtime/memory/env-var config that deploy_agent relies on.
            cp agentcore/agentcore.json agentcore/agentcore.json.bak

            echo "Resetting AgentCore local config (step 1/2)..."
            if "$_ac_cli" remove all -y; then
                echo "Pushing empty config to tear down AWS resources (step 2/2)..."
                if "$_ac_cli" deploy -y; then
                    agentcore_destroyed=true
                    print_status "AgentCore runtime destroyed via CLI"
                else
                    print_warning "agentcore deploy (teardown) failed — will fall back to CFN delete"
                fi
            else
                print_warning "agentcore remove all failed — will fall back to CFN delete"
            fi

            # Restore agentcore.json so the next `./deploy.sh` works without manual steps.
            # `remove all` empties the file (runtimes=[], memories=[]), which would cause
            # deploy_agent to skip runtime creation (file exists but has no config).
            mv agentcore/agentcore.json.bak agentcore/agentcore.json
            print_status "Restored agentcore.json (ready for next deploy)"
        fi
        cd ../infra

        # Step 1b: Fallback — if the AgentCore stack still exists, delete it directly
        if [ "$agentcore_destroyed" = false ] || \
           aws cloudformation describe-stacks --stack-name "$agent_stack_name" >/dev/null 2>&1; then
            if aws cloudformation describe-stacks --stack-name "$agent_stack_name" >/dev/null 2>&1; then
                echo "Deleting CloudFormation stack directly: $agent_stack_name"
                if aws cloudformation delete-stack --stack-name "$agent_stack_name"; then
                    aws cloudformation wait stack-delete-complete --stack-name "$agent_stack_name" 2>/dev/null \
                        && print_status "AgentCore stack destroyed: $agent_stack_name" \
                        || print_warning "AgentCore stack delete did not complete cleanly — check console"
                else
                    print_warning "Could not initiate delete on $agent_stack_name — delete manually"
                fi
            fi
        fi

        # Step 2: Destroy CDK stacks (solution only — sample env preserved).
        # Listing stacks explicitly avoids `--all` sweeping up Patchy-SampleEnv,
        # which is managed by ./sample-env.sh and intentionally outlives the
        # solution lifecycle.
        local solution_stacks=("Patchy-UI" "Patchy-Core" "Patchy-Network" "Patchy-VpcLookup")
        for s in "${solution_stacks[@]}"; do
            if aws cloudformation describe-stacks --stack-name "$s" >/dev/null 2>&1; then
                npx cdk destroy "$s" --exclusively --force \
                    -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
            fi
        done
        print_status "CDK stacks destroyed"
        cd ..

        # Step 3: Destroy spoke + docs StackSets (not CDK resources — managed directly)
        # Default: PRESERVE. Set DESTROY_SPOKE_STACKSET=true to opt in.
        if [ "${DESTROY_SPOKE_STACKSET}" != "true" ]; then
            print_warning "Skipping Patchy-SpokeIam + Patchy-SsmDocs StackSets (DESTROY_SPOKE_STACKSET=false)"
            print_warning "  StackSets retained. To remove later:"
            print_warning "    ./deploy.sh destroy --spoke-only   (IAM)"
            print_warning "    ./deploy.sh destroy --docs-only    (SSM docs)"
        elif [ "${MULTI_ACCOUNT_ENABLED:-false}" = "true" ]; then
            destroy_spoke || print_warning "Spoke IAM StackSet destroy had issues — inspect manually"
            destroy_docs  || print_warning "SSM docs StackSet destroy had issues — inspect manually"
        else
            print_status "Multi-account disabled — skipping spoke + docs StackSet destroy"
        fi

        # Step 4: Delete the SSM Resource Data Sync (also not a CDK resource)
        # Default: PRESERVE. Set DESTROY_FLEET_SYNC=true to opt in.
        if [ "${DESTROY_FLEET_SYNC}" != "true" ]; then
            print_warning "Skipping SSM Resource Data Sync 'patchy-fleet-sync' (DESTROY_FLEET_SYNC=false)"
            print_warning "  Sync retained. To remove later: DESTROY_FLEET_SYNC=true ./deploy.sh destroy"
        else
            local sync_name="patchy-fleet-sync"
            if aws ssm list-resource-data-sync --sync-type SyncFromSource \
                --query "ResourceDataSyncItems[?SyncName=='${sync_name}']" --output text 2>/dev/null | grep -q "$sync_name"; then
                aws ssm delete-resource-data-sync --sync-name "$sync_name" --sync-type SyncFromSource 2>/dev/null \
                    && print_status "Resource Data Sync deleted: $sync_name" \
                    || print_warning "Could not delete Resource Data Sync — delete manually if needed"
            fi
        fi

        print_status "All infrastructure destroyed"
        return 0
    cd ..
}

# ── Status (read-only) ─────────────────────────────────────────────

show_status() {
    echo -e "${BLUE}Intelligent Patch Automation — Deployment Status${NC}"
    echo "=================================================="

    local hub_account region
    hub_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "?")
    region="${AWS_REGION:-us-east-1}"

    # ── Detect deployment footprint ────────────────────────────────
    # A "Patchy deployment" in this account means the hub CFN stacks exist here.
    # The StackSets (Patchy-SpokeIam, Patchy-SsmDocs), AgentCore memory, S3 bucket,
    # or Inspector being enabled are NOT sufficient on their own — those can exist
    # for other reasons (e.g. this account is the org's StackSet management/DA but
    # the hub lives elsewhere; Inspector enabled for general security; leftover
    # memory from a destroyed deploy).
    local deployed_stacks
    deployed_stacks=$(aws cloudformation describe-stacks \
        --query "Stacks[?starts_with(StackName,'Patchy-') || starts_with(StackName,'AgentCore-patchy')].StackName" \
        --output text 2>/dev/null || true)

    if [ -z "$deployed_stacks" ]; then
        echo ""
        echo "  No deployment detected in account ${hub_account} / region ${region}"
        echo "  (no Patchy-* or AgentCore-patchy-* CloudFormation stacks)"
        echo ""
        echo "  Run ./deploy.sh to deploy."
        echo ""
        return 0
    fi

    # Hub is present — now probe the supplementary resources we may want to show.
    # We treat either Patchy-SpokeIam OR the legacy Patchy-SpokeRole as evidence
    # of a multi-account deployment.
    local stackset_exists
    stackset_exists=""
    for ss_probe in Patchy-SpokeIam Patchy-SpokeRole; do
        stackset_exists=$(aws cloudformation describe-stack-set --stack-set-name "$ss_probe" --call-as DELEGATED_ADMIN \
            --query 'StackSet.StackSetId' --output text 2>/dev/null || true)
        if [ -z "$stackset_exists" ] || [ "$stackset_exists" = "None" ]; then
            stackset_exists=$(aws cloudformation describe-stack-set --stack-set-name "$ss_probe" \
                --query 'StackSet.StackSetId' --output text 2>/dev/null || true)
        fi
        if [ -n "$stackset_exists" ] && [ "$stackset_exists" != "None" ]; then
            break
        fi
    done

    # ── Account / region ──────────────────────────────────────────
    echo ""
    echo -e "${BLUE}Account & region${NC}"
    echo "  Hub account:        ${hub_account}"
    echo "  AWS profile:        ${AWS_PROFILE:-default}"
    echo "  Region:             ${region}"

    # ── Multi-account ─────────────────────────────────────────────
    # Only meaningful when the StackSet (which carries the cross-account spoke roles)
    # actually exists. Without it, MULTI_ACCOUNT_ENABLED is just .env intent, not state.
    if [ -n "$stackset_exists" ] && [ "$stackset_exists" != "None" ]; then
        echo ""
        echo -e "${BLUE}Multi-account${NC}"
        echo "  Mode:               enabled"
        echo "  Org ID:             ${AWS_ORG_ID:-not set}"
        echo "  Spoke role:         ${SPOKE_EXECUTION_ROLE:-PatchySpokeRole}"
        echo "  Spoke accounts:     ${SPOKE_ACCOUNT_IDS:-(discover from org)}"
        echo "  Spoke OUs:          ${SPOKE_OU_IDS:-not set}"
        echo "  Spoke regions:      ${SPOKE_REGIONS:-${region} (default)}"
    fi

    # ── AgentCore ─────────────────────────────────────────────────
    # Only print this section when there's something to report. The agent name from
    # agentcore.json alone isn't a deployment — only ARNs / memory IDs are.
    local agent_arn role_arn memory_id agent_name memory_arn
    local deployed_state="agent/agentcore/.cli/deployed-state.json"
    if [ -f "$deployed_state" ]; then
        agent_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'runtimeArn' in a:
        print(a['runtimeArn']); break
" 2>/dev/null || true)
        role_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
agents = res.get('runtimes') or res.get('agents') or {}
for a in agents.values():
    if 'roleArn' in a:
        print(a['roleArn']); break
" 2>/dev/null || true)
        memory_id=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
mems = res.get('memories') or {}
for m in mems.values():
    if 'memoryId' in m:
        print(m['memoryId']); break
" 2>/dev/null || true)
        memory_arn=$(python3 -c "
import json
with open('$deployed_state') as f:
    state = json.load(f)
res = state.get('targets',{}).get('default',{}).get('resources',{})
mems = res.get('memories') or {}
for m in mems.values():
    if 'memoryArn' in m:
        print(m['memoryArn']); break
" 2>/dev/null || true)
    fi

    # Verify the runtime/memory in deployed-state actually exist in AWS — stale local
    # state from a deleted stack should not show up as deployed.
    if [ -n "$agent_arn" ]; then
        if ! aws bedrock-agentcore-control get-agent-runtime \
                --agent-runtime-id "$(echo "$agent_arn" | sed 's|.*/||' | cut -d: -f1)" \
                --region "$region" &>/dev/null; then
            agent_arn=""
            role_arn=""
        fi
    fi
    if [ -n "$memory_id" ]; then
        if ! aws bedrock-agentcore-control get-memory --memory-id "$memory_id" \
                --region "$region" &>/dev/null; then
            memory_id=""
            memory_arn=""
        fi
    fi

    # Fallback: read CloudFormation stack outputs when deployed-state is missing fields.
    # The agentcore CLI sometimes only writes memories + stackName to deployed-state.json,
    # leaving the runtime ARN + role ARN to be fetched from the stack outputs.
    local agentcore_stack_exists
    agentcore_stack_exists=$(echo "$deployed_stacks" | tr -s '[:space:]' '\n' | grep -x 'AgentCore-patchy-default' || true)
    if [ -n "$agentcore_stack_exists" ] && { [ -z "$agent_arn" ] || [ -z "$role_arn" ]; }; then
        local cfn_outputs
        cfn_outputs=$(aws cloudformation describe-stacks --stack-name AgentCore-patchy-default \
            --query 'Stacks[0].Outputs' --output json 2>/dev/null || echo "[]")
        if [ -z "$agent_arn" ]; then
            agent_arn=$(echo "$cfn_outputs" | python3 -c "
import json, sys
for o in json.load(sys.stdin):
    if 'RuntimeArn' in o.get('OutputKey',''):
        print(o['OutputValue']); break
" 2>/dev/null || true)
        fi
        if [ -z "$role_arn" ]; then
            role_arn=$(echo "$cfn_outputs" | python3 -c "
import json, sys
for o in json.load(sys.stdin):
    if 'RoleArn' in o.get('OutputKey',''):
        print(o['OutputValue']); break
" 2>/dev/null || true)
        fi
    fi

    if [ -n "$agent_arn" ] || [ -n "$role_arn" ] || [ -n "$memory_id" ] || [ -n "$agentcore_stack_exists" ]; then
        if [ -f "agent/agentcore/agentcore.json" ]; then
            agent_name=$(python3 -c "
import json
c = json.load(open('agent/agentcore/agentcore.json'))
print(c.get('name','(unknown)'))
" 2>/dev/null || echo "?")
        fi
        echo ""
        echo -e "${BLUE}AgentCore runtime${NC}"
        echo "  Name:               ${agent_name:-not configured}"
        echo "  Agent ARN:          ${agent_arn:-not deployed}"
        echo "  Runtime role:       ${role_arn:-not deployed}"
        echo "  Memory ID:          ${memory_id:-not deployed}"
        [ -n "$memory_arn" ] && echo "  Memory ARN:         ${memory_arn}"
        if [ -n "$agent_arn" ]; then
            echo "  Bedrock model:      ${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6 (default)}"
        fi
    fi

    # ── CloudFormation stacks ─────────────────────────────────────
    # Note: Patchy-Network and Patchy-VpcLookup are mutually exclusive (one or the other,
    # depending on EXISTING_VPC_ID). Patchy-SampleEnv is managed by ./sample-env.sh.
    # We only print stacks that actually exist — absence is normal.
    if [ -n "$deployed_stacks" ]; then
        echo ""
        echo -e "${BLUE}CloudFormation stacks${NC}"
        local all_stacks=("Patchy-Network" "Patchy-VpcLookup" "Patchy-Core" "Patchy-UI" "Patchy-SampleEnv" "AgentCore-patchy-default")
        for s in "${all_stacks[@]}"; do
            local row
            row=$(aws cloudformation describe-stacks --stack-name "$s" \
                --query 'Stacks[0].[StackStatus,LastUpdatedTime]' --output text 2>/dev/null || true)
            if [ -n "$row" ]; then
                local status_val updated
                status_val=$(echo "$row" | awk '{print $1}')
                updated=$(echo "$row" | awk '{print $2}' | cut -d'T' -f1)
                printf "  %-30s  %s  (%s)\n" "$s" "$status_val" "${updated:-?}"
            fi
        done
    fi

    # ── StackSets (spoke IAM + SSM docs) ──────────────────────────
    _print_stackset_status() {
        local ss_name="$1"
        local ss_id ss_summary
        # Try DA mode first, then self-managed
        ss_id=""
        for call_as_flag in "--call-as DELEGATED_ADMIN" ""; do
            ss_id=$(aws cloudformation describe-stack-set --stack-set-name "$ss_name" $call_as_flag \
                --query 'StackSet.StackSetId' --output text 2>/dev/null || true)
            if [ -n "$ss_id" ] && [ "$ss_id" != "None" ]; then
                ss_summary=$(aws cloudformation describe-stack-set --stack-set-name "$ss_name" $call_as_flag \
                    --query 'StackSet.[Status,PermissionModel]' --output text 2>/dev/null || true)
                local ss_status ss_perm
                ss_status=$(echo "$ss_summary" | awk '{print $1}')
                ss_perm=$(echo "$ss_summary" | awk '{print $2}')
                echo ""
                echo -e "${BLUE}StackSet — ${ss_name}${NC}"
                echo "  Status:             ${ss_status}"
                echo "  Permission model:   ${ss_perm}"
                local instances
                instances=$(aws cloudformation list-stack-instances --stack-set-name "$ss_name" $call_as_flag \
                    --query 'Summaries[].[Account,Region,Status]' --output text 2>/dev/null || true)
                if [ -n "$instances" ]; then
                    local count
                    count=$(echo "$instances" | wc -l | tr -d ' ')
                    echo "  Stack instances:    ${count}"
                    echo "$instances" | head -10 | while read -r acct rgn st; do
                        printf "                      %s  %s  %s\n" "$acct" "$rgn" "$st"
                    done
                else
                    echo "  Stack instances:    none"
                fi
                return 0
            fi
        done
        return 1
    }

    _print_stackset_status "Patchy-SpokeIam" || true
    _print_stackset_status "Patchy-SsmDocs"  || true
    # Surface the legacy StackSet too, so operators upgrading from the older
    # combined topology can see it still exists and clean it up.
    _print_stackset_status "Patchy-SpokeRole" || true

    # ── UI ────────────────────────────────────────────────────────
    local ui_stack_exists
    ui_stack_exists=$(echo "$deployed_stacks" | tr -s '[:space:]' '\n' | grep -x 'Patchy-UI' || true)
    if [ -n "$ui_stack_exists" ]; then
        echo ""
        echo -e "${BLUE}Web UI${NC}"
        local ui_url
        ui_url=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text 2>/dev/null || true)
        if [ -n "$ui_url" ] && [ "$ui_url" != "None" ]; then
            # Highlight the URL in bold green so operators can copy it out of
            # a long status block without hunting for it.
            echo -e "  URL:                \033[1;32m${ui_url}\033[0m"
            local cog_pool
            cog_pool=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
                --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text 2>/dev/null || true)
            if [ -n "$cog_pool" ] && [ "$cog_pool" != "None" ]; then
                echo "  Auth:               Cognito (User Pool: ${cog_pool})"
                local cog_domain
                cog_domain=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
                    --query 'Stacks[0].Outputs[?OutputKey==`CognitoDomainPrefix`].OutputValue' --output text 2>/dev/null || true)
                [ -n "$cog_domain" ] && [ "$cog_domain" != "None" ] && echo "  Cognito domain:     ${cog_domain}"
            else
                echo "  Auth:               internal ALB (no Cognito)"
                local bastion
                bastion=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
                    --query 'Stacks[0].Outputs[?OutputKey==`BastionInstanceId`].OutputValue' --output text 2>/dev/null || true)
                [ -n "$bastion" ] && [ "$bastion" != "None" ] && echo "  Bastion instance:   ${bastion}"
            fi
        fi
        [ -n "$ACM_CERTIFICATE_ARN" ] && echo "  TLS certificate:    ${ACM_CERTIFICATE_ARN}"
    fi

    # ── S3 bucket ─────────────────────────────────────────────────
    local bucket="patch-compliance-reports-${hub_account}"
    if aws s3api head-bucket --bucket "$bucket" &>/dev/null; then
        echo ""
        echo -e "${BLUE}Compliance reports${NC}"
        echo "  Bucket:             s3://${bucket}"
        local report_count
        report_count=$(aws s3api list-objects-v2 --bucket "$bucket" --prefix "$(date +%Y/%m)" \
            --query 'KeyCount' --output text 2>/dev/null || echo "?")
        echo "  Reports this month: ${report_count}"
        local override_count
        override_count=$(aws s3api list-objects-v2 --bucket "$bucket" --prefix "baseline-overrides/" \
            --query 'KeyCount' --output text 2>/dev/null || echo "?")
        echo "  Baseline overrides: ${override_count}"
    fi

    # ── Explorer sync ─────────────────────────────────────────────
    local sync_info
    sync_info=$(aws ssm list-resource-data-sync --sync-type SyncFromSource --region "$region" \
        --query "ResourceDataSyncItems[?SyncName=='patchy-fleet-sync'].[SyncSource.SourceType,SyncSource.State,join(',',SyncSource.SourceRegions)]" \
        --output text 2>/dev/null || true)
    if [ -n "$sync_info" ]; then
        echo ""
        echo -e "${BLUE}SSM Explorer Resource Data Sync${NC}"
        local src_type src_state src_regions
        src_type=$(echo "$sync_info" | awk '{print $1}')
        src_state=$(echo "$sync_info" | awk '{print $2}')
        src_regions=$(echo "$sync_info" | awk '{for(i=3;i<=NF;i++) printf "%s%s", $i, (i<NF?" ":"")}')
        echo "  Sync name:          patchy-fleet-sync"
        echo "  Source type:        ${src_type}"
        echo "  State:              ${src_state}"
        echo "  Source regions:     ${src_regions}"
    fi

    # ── Inspector ─────────────────────────────────────────────────
    # Inspector is the source of vulnerability findings — without it the dashboard's
    # CVE count and the chat agent's vulnerability discovery return empty even when
    # there are vulnerable instances. Always show the section so operators know to
    # check, and report per-region state across SPOKE_REGIONS.
    echo ""
    echo -e "${BLUE}Inspector${NC}"
    echo "  Resource types:     ${INSPECTOR_RESOURCE_TYPES:-EC2 (default)}"

    # Org-level configuration (only DA can call describe-organization-configuration)
    local org_cfg_json
    org_cfg_json=$(aws inspector2 describe-organization-configuration --region "$region" \
        --output json 2>/dev/null || true)
    if [ -n "$org_cfg_json" ] && [ "$org_cfg_json" != "null" ]; then
        local auto_ec2 auto_ecr auto_lambda
        auto_ec2=$(echo "$org_cfg_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('autoEnable',{}).get('ec2',False))" 2>/dev/null)
        auto_ecr=$(echo "$org_cfg_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('autoEnable',{}).get('ecr',False))" 2>/dev/null)
        auto_lambda=$(echo "$org_cfg_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('autoEnable',{}).get('lambda',False))" 2>/dev/null)
        echo "  Org admin (DA):     ${hub_account} (this account)"
        echo "  Auto-enable:        EC2=${auto_ec2}  ECR=${auto_ecr}  Lambda=${auto_lambda}  (in this region)"

        # Member-account count in this region
        local member_count
        member_count=$(aws inspector2 list-members --region "$region" \
            --query 'length(members)' --output text 2>/dev/null || echo "?")
        echo "  Member accounts:    ${member_count} (in this region)"
    else
        # describe-organization-configuration failed — either not the DA, no permission,
        # or Inspector not enabled in this region for the caller
        echo "  Org admin (DA):     not the org admin (or Inspector not enabled here)"
        echo "                      Recommend: register a DA via mgmt account and enable"
        echo "                      via Inspector Org policy (auto-enables all regions)"
    fi

    # Per-region account state — only check regions the solution actually queries.
    # We check just the hub here; spoke account checks would need assume-role per region
    # which is more cost than this status command warrants. Spoke coverage is implied
    # by the "Member accounts" count and Auto-enable above.
    local spoke_regions_list="${SPOKE_REGIONS:-${region}}"
    echo "  Per-region status:"
    local rgn ec2_status overall_status
    for rgn in $(echo "$spoke_regions_list" | tr ',' ' '); do
        rgn=$(echo "$rgn" | xargs)  # trim
        [ -z "$rgn" ] && continue
        overall_status=$(aws inspector2 batch-get-account-status \
            --account-ids "$hub_account" --region "$rgn" \
            --query 'accounts[0].state.status' --output text 2>/dev/null || echo "ERROR")
        ec2_status=$(aws inspector2 batch-get-account-status \
            --account-ids "$hub_account" --region "$rgn" \
            --query 'accounts[0].resourceState.ec2.status' --output text 2>/dev/null || echo "ERROR")
        if [ "$overall_status" = "ENABLED" ] && [ "$ec2_status" = "ENABLED" ]; then
            printf "    %-20s %s\n" "$rgn" "✅ EC2 scanning enabled"
        elif [ "$overall_status" = "ENABLED" ]; then
            printf "    %-20s %s\n" "$rgn" "⚠️  account enabled, EC2 ${ec2_status}"
        elif [ "$overall_status" = "DISABLED" ] || [ "$overall_status" = "None" ]; then
            printf "    %-20s %s\n" "$rgn" "❌ disabled (no findings will appear)"
        else
            printf "    %-20s %s\n" "$rgn" "${overall_status} / EC2 ${ec2_status}"
        fi
    done

    echo ""
}

# ── Main ────────────────────────────────────────────────────────────

COMMAND=${1:-}
FLAG=${2:-}

case "$COMMAND" in
    "")
        # Full deploy: env → agent → infra → UI
        check_prereqs true true true
        echo -e "${BLUE}Intelligent Patch Automation — Full Deploy${NC}"
        echo "=================================================="

        setup_environment
        ensure_cdk_bootstrap
        ensure_observability
        deploy_agent
        ensure_runtime_observability

        # Deploy hub infrastructure (Network + Core)
        deploy_infra

        # Upload baseline overrides AFTER infra (S3 bucket must exist first)
        echo -e "${BLUE}Uploading severity-scoped baseline overrides to S3...${NC}"
        cd agent
        python setup_baseline_overrides.py || print_warning "Baseline overrides upload failed — run manually later: python agent/setup_baseline_overrides.py"
        cd ..

        # Auto-deploy spoke roles + SSM documents when multi-account is configured
        if [ "${MULTI_ACCOUNT_ENABLED:-false}" = "true" ]; then
            if [ -n "${SPOKE_ACCOUNT_IDS:-}" ] || [ -n "${SPOKE_OU_IDS:-}" ]; then
                deploy_spoke
                deploy_docs
            else
                print_warning "MULTI_ACCOUNT_ENABLED=true but no SPOKE_ACCOUNT_IDS or SPOKE_OU_IDS set"
                echo "  Skipping spoke + docs deployment. Set targets in .env or run:"
                echo "    ./deploy.sh spoke   (cross-account IAM role)"
                echo "    ./deploy.sh docs    (SSM Automation documents)"
            fi
        fi

        deploy_ui

        echo ""
        echo -e "${GREEN}Deployment complete.${NC}"
        echo "=================================================="
        echo ""
        echo "Next steps:"
        echo "  1. Create a user:           ./deploy.sh create-user"
        echo "  2. Deploy sample EC2 env:   ./sample-env.sh deploy   (optional, demo data)"
        echo "  3. Open the UI:             Check the Patchy-UI stack outputs for the URL"
        echo ""
        ;;

    "agent")
        check_prereqs true true true
        echo -e "${BLUE}Intelligent Patch Automation — Redeploy Agent${NC}"
        echo "=================================================="
        run_eval_gate
        setup_environment
        ensure_cdk_bootstrap
        ensure_observability
        deploy_agent
        ensure_runtime_observability
        deploy_ui
        ;;

    "ui")
        check_prereqs false true true
        echo -e "${BLUE}Intelligent Patch Automation — Redeploy UI${NC}"
        echo "=================================================="
        deploy_ui
        ;;

    "spoke")
        check_prereqs false true
        echo -e "${BLUE}Intelligent Patch Automation — Deploy Spoke IAM Role${NC}"
        echo "=================================================="
        deploy_spoke
        ;;

    "docs")
        check_prereqs false true
        echo -e "${BLUE}Intelligent Patch Automation — Deploy SSM Documents${NC}"
        echo "=================================================="
        deploy_docs
        ;;

    "destroy")
        check_prereqs false true
        if [ "$FLAG" = "--sample-only" ]; then
            print_error "Sample environment is now managed by ./sample-env.sh"
            echo "  Run: ./sample-env.sh destroy"
            exit 1
        elif [ "$FLAG" = "--spoke-only" ]; then
            destroy spoke
        elif [ "$FLAG" = "--docs-only" ]; then
            destroy docs
        else
            destroy all
        fi
        ;;

    "status")
        check_prereqs false false false
        show_status
        exit 0
        ;;

    "create-user")
        check_prereqs false false false
        echo -e "${BLUE}Intelligent Patch Automation — Create Cognito User${NC}"
        echo "=================================================="

        POOL_ID=$(aws cloudformation describe-stacks --stack-name Patchy-UI \
            --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' \
            --output text --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null)

        if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
            print_error "Could not find Cognito User Pool. Is Patchy-UI deployed?"
            exit 1
        fi

        read -p "Email address: " USER_EMAIL
        if [ -z "$USER_EMAIL" ]; then
            print_error "Email is required"
            exit 1
        fi

        read -sp "Temporary password (min 8 chars, uppercase, lowercase, number): " USER_PASSWORD
        echo ""
        if [ -z "$USER_PASSWORD" ]; then
            print_error "Password is required"
            exit 1
        fi

        read -p "Role (operator/viewer) [operator]: " USER_ROLE
        USER_ROLE="${USER_ROLE:-operator}"
        if [ "$USER_ROLE" != "operator" ] && [ "$USER_ROLE" != "viewer" ]; then
            print_error "Role must be 'operator' or 'viewer'"
            exit 1
        fi
        GROUP_NAME="${USER_ROLE}s"

        echo -e "${BLUE}Creating user: $USER_EMAIL (role: $USER_ROLE)${NC}"

        set +e  # Disable exit-on-error for user creation (user may already exist)
        aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" \
            --username "$USER_EMAIL" \
            --user-attributes Name=email,Value="$USER_EMAIL" Name=email_verified,Value=true \
            --temporary-password "$USER_PASSWORD" --message-action SUPPRESS \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" > /dev/null 2>&1
        create_exit=$?
        
        aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
            --username "$USER_EMAIL" --group-name "$GROUP_NAME" \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" > /dev/null 2>&1
        group_exit=$?
        set -e

        if [ $group_exit -ne 0 ]; then
            print_error "Failed to add user to group '$GROUP_NAME'"
            exit 1
        fi

        if [ $create_exit -ne 0 ]; then
            print_warning "User may already exist — added to group '$GROUP_NAME'"
        else
            print_status "User created: $USER_EMAIL (group: $GROUP_NAME)"
        fi
        echo ""
        echo "Login with this email and temporary password. Cognito will prompt to set a new password on first login."
        ;;

    "help"|"--help"|"-h")
        show_help
        ;;

    *)
        print_error "Unknown command: $COMMAND"
        echo ""
        show_help
        exit 1
        ;;
esac

echo ""
echo -e "AWS Profile: ${BLUE}$AWS_PROFILE${NC} | Region: ${BLUE}$AWS_REGION${NC}"
