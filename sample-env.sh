#!/bin/bash

# Sample Environment Lifecycle — separate from the solution.
#
# The sample env is demo data: a single-VPC stack with 5 EC2 instances
# (dev/staging/prod), maintenance windows, ALBs, and a patch baseline.
# It is NOT required for the solution to work — most customers point the
# agent at their own existing fleet.
#
# Lives in its own script so:
#   - ./deploy.sh destroy never accidentally tears down demo data
#   - the StackSet teardown's eventual-consistency dance is in one place
#   - operators can deploy/destroy demo instances independently
#
# Usage:
#   ./sample-env.sh                Deploy hub + spoke (when multi-account)
#   ./sample-env.sh deploy         Same as above
#   ./sample-env.sh destroy        Tear down hub + spoke StackSet
#   ./sample-env.sh status         Show hub stack + StackSet instance counts
#   ./sample-env.sh help

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

AWS_PROFILE="${_saved_profile:-${AWS_PROFILE:-default}}"
AWS_REGION="${_saved_region:-${AWS_REGION:-us-east-1}}"
AGENTCORE_ROLE_ARN="${AGENTCORE_ROLE_ARN:-}"
export AWS_PROFILE AWS_REGION AGENTCORE_ROLE_ARN
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text 2>/dev/null)
export CDK_DEFAULT_REGION=$AWS_REGION
unset _saved_profile _saved_region

print_status()  { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error()   { echo -e "${RED}❌ $1${NC}"; }

STACK_NAME="Patchy-SampleEnv"
SOLUTION_STACK_FOR_BUCKET="Patchy-Core"

# ── Help ────────────────────────────────────────────────────────────

show_help() {
    echo -e "${BLUE}Patchy — Sample Environment${NC}"
    echo "=================================================="
    echo ""
    echo "Manages the sample EC2 environment used for demos and testing."
    echo "Independent of the solution lifecycle — ./deploy.sh destroy never"
    echo "touches the sample env."
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "  ${GREEN}deploy${NC}        Deploy hub stack + (multi-account) spoke StackSet"
    echo "  ${GREEN}status${NC}        Show what is currently deployed"
    echo "  ${RED}destroy${NC}        Tear down hub stack + spoke StackSet"
    echo "  ${BLUE}help${NC}          This message"
    echo ""
    echo "Multi-account targets are resolved from .env in this order:"
    echo "  SAMPLE_ENV_ACCOUNTS (comma-separated, explicit)"
    echo "    or"
    echo "  first account in SPOKE_ACCOUNT_IDS"
    echo ""
    echo "Prerequisites:"
    echo "  - $SOLUTION_STACK_FOR_BUCKET must be deployed (compliance bucket lookup)"
    echo "  - For multi-account: Patchy-SpokeRole StackSet present in spoke accounts"
    echo ""
}

# ── Prereqs ─────────────────────────────────────────────────────────

check_prereqs() {
    if ! command -v aws &>/dev/null; then
        print_error "AWS CLI v2 required"
        exit 1
    fi
    if ! command -v node &>/dev/null; then
        print_error "Node.js 18+ required"
        exit 1
    fi
    if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null; then
        print_error "AWS credentials not valid (profile: $AWS_PROFILE)"
        exit 1
    fi
}

require_solution_deployed() {
    # The sample stack's IAM grant references the compliance bucket created
    # by Patchy-Core. The bucket lookup is by name, so deploying without it
    # technically succeeds — but the demo behaviour is incomplete because
    # the instances can't fetch baseline overrides.
    if ! aws cloudformation describe-stacks --stack-name "$SOLUTION_STACK_FOR_BUCKET" \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" &>/dev/null; then
        print_warning "$SOLUTION_STACK_FOR_BUCKET not found — deploy the solution first: ./deploy.sh"
        print_warning "Continuing anyway; sample instances will lack baseline-override read access."
    fi
}

# ── StackSet detection helpers ──────────────────────────────────────

# Detect the call mode for the StackSet:
#   SERVICE_MANAGED + DA      → "--call-as DELEGATED_ADMIN"
#   SERVICE_MANAGED (mgmt)    → ""
#   SELF_MANAGED              → ""
#   StackSet does not exist   → "MISSING"
detect_call_as() {
    local ss_name=$1
    if aws cloudformation describe-stack-set --stack-set-name "$ss_name" \
            --call-as DELEGATED_ADMIN \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" &>/dev/null; then
        echo "--call-as DELEGATED_ADMIN"
        return 0
    fi
    if aws cloudformation describe-stack-set --stack-set-name "$ss_name" \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" &>/dev/null; then
        echo ""
        return 0
    fi
    echo "MISSING"
}

# ── Deploy ──────────────────────────────────────────────────────────

deploy_hub_stack() {
    echo -e "${BLUE}Deploying hub sample stack ($STACK_NAME)...${NC}"
    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    npx cdk deploy "$STACK_NAME" --exclusively --require-approval never \
        -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
    print_status "Hub sample stack deployed"
    cd ..
}

deploy_spoke_stackset() {
    if [ "${MULTI_ACCOUNT_ENABLED:-false}" != "true" ]; then
        print_status "Single-account mode — spoke StackSet skipped"
        return 0
    fi

    local hub_account_id="${CDK_DEFAULT_ACCOUNT}"
    local region="${AWS_REGION:-us-east-1}"

    # Resolve target spoke accounts:
    #   SAMPLE_ENV_ACCOUNTS overrides; otherwise take the first SPOKE_ACCOUNT_IDS entry.
    local target_accounts=""
    if [ -n "${SAMPLE_ENV_ACCOUNTS:-}" ]; then
        target_accounts="$SAMPLE_ENV_ACCOUNTS"
    elif [ -n "${SPOKE_ACCOUNT_IDS:-}" ]; then
        target_accounts=$(echo "$SPOKE_ACCOUNT_IDS" | tr ',' '\n' | head -1)
    fi
    target_accounts=$(echo "$target_accounts" | tr ',' '\n' | grep -v "^${hub_account_id}$" | grep -v '^$' | tr '\n' ',' | sed 's/,$//')

    if [ -z "$target_accounts" ]; then
        print_warning "No spoke account configured — skipping StackSet"
        return 0
    fi

    echo -e "${BLUE}Deploying $STACK_NAME to spoke accounts: $target_accounts (region: $region)...${NC}"

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi

    echo "Synthesizing $STACK_NAME template (self-contained, no VPC dependency)..."
    npx cdk synth "$STACK_NAME" --exclusively \
        -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}" \
        -c hubAccountId="${hub_account_id}" \
        -c synthForStackSet=true \
        --quiet > /dev/null 2>&1

    local template_path="cdk.out/${STACK_NAME}.template.json"
    if [ ! -f "$template_path" ]; then
        print_error "Failed to synthesize $STACK_NAME template"
        cd ..
        return 1
    fi

    # Create or update the StackSet (always SERVICE_MANAGED via DA).
    if aws cloudformation describe-stack-set --stack-set-name "$STACK_NAME" \
            --call-as DELEGATED_ADMIN \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" &>/dev/null; then
        echo "Updating existing StackSet: $STACK_NAME"
        aws cloudformation update-stack-set \
            --stack-set-name "$STACK_NAME" \
            --template-body "file://${template_path}" \
            --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
            --permission-model SERVICE_MANAGED \
            --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
            --call-as DELEGATED_ADMIN \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
    else
        echo "Creating StackSet: $STACK_NAME"
        aws cloudformation create-stack-set \
            --stack-set-name "$STACK_NAME" \
            --template-body "file://${template_path}" \
            --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
            --permission-model SERVICE_MANAGED \
            --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
            --call-as DELEGATED_ADMIN \
            --profile "$AWS_PROFILE" --region "$AWS_REGION"
    fi

    # Deploy stack instances per target account using INTERSECTION targeting.
    for acct in $(echo "$target_accounts" | tr ',' '\n'); do
        local acct_ou
        acct_ou=$(aws organizations list-parents --child-id "$acct" \
            --query 'Parents[0].Id' --output text 2>/dev/null || true)
        if [ -z "$acct_ou" ] || [ "$acct_ou" = "None" ]; then
            print_warning "Could not determine OU for account $acct — skipping"
            continue
        fi

        local existing
        existing=$(aws cloudformation list-stack-instances \
            --stack-set-name "$STACK_NAME" \
            --call-as DELEGATED_ADMIN \
            --query "Summaries[?Account=='${acct}' && Region=='${region}'].Status" \
            --output text \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true)
        if [ -n "$existing" ] && [ "$existing" != "None" ]; then
            print_status "Stack instance already exists in $acct/$region (status: $existing)"
            continue
        fi

        echo "Creating stack instance in $acct/$region (OU: $acct_ou)..."
        aws cloudformation create-stack-instances \
            --stack-set-name "$STACK_NAME" \
            --deployment-targets "OrganizationalUnitIds=[\"${acct_ou}\"],Accounts=[\"${acct}\"],AccountFilterType=INTERSECTION" \
            --regions "$region" \
            --operation-preferences MaxConcurrentCount=1,FailureToleranceCount=0 \
            --call-as DELEGATED_ADMIN \
            --profile "$AWS_PROFILE" --region "$AWS_REGION"
    done

    # Wait for instances to settle (10 min timeout).
    echo "Waiting for stack instance deployments (this may take a few minutes)..."
    local waited=0
    while [ $waited -lt 600 ]; do
        local pending
        pending=$(aws cloudformation list-stack-instances \
            --stack-set-name "$STACK_NAME" \
            --call-as DELEGATED_ADMIN \
            --query "Summaries[?StackInstanceStatus.DetailedStatus!='SUCCEEDED' && StackInstanceStatus.DetailedStatus!='CURRENT' && Status!='CURRENT'].Account" \
            --output text \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true)
        [ -z "$pending" ] || [ "$pending" = "None" ] && break

        local failed
        failed=$(aws cloudformation list-stack-instances \
            --stack-set-name "$STACK_NAME" \
            --call-as DELEGATED_ADMIN \
            --query "Summaries[?Status=='OUTDATED' && StackInstanceStatus.DetailedStatus=='FAILED'].Account" \
            --output text \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true)
        if [ -n "$failed" ] && [ "$failed" != "None" ]; then
            print_error "Stack instance deployment failed in: $failed"
            print_warning "Check CloudFormation StackSet operations in the console"
            cd ..
            return 1
        fi
        sleep 15
        waited=$((waited + 15))
        echo "  Waiting... (${waited}s elapsed)"
    done

    print_status "Sample environment deployed to: $target_accounts"
    cd ..
}

deploy_all() {
    check_prereqs
    require_solution_deployed
    deploy_hub_stack
    deploy_spoke_stackset

    echo ""
    echo -e "${GREEN}Sample environment deployment complete.${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Verify instances:    ./sample-env.sh status"
    echo "  2. Try the agent:       open the UI and ask 'show fleet'"
    echo ""
}

# ── Destroy ─────────────────────────────────────────────────────────

destroy_spoke_stackset() {
    if [ "${MULTI_ACCOUNT_ENABLED:-false}" != "true" ]; then
        return 0
    fi

    local call_as
    call_as=$(detect_call_as "$STACK_NAME")
    if [ "$call_as" = "MISSING" ]; then
        print_status "$STACK_NAME StackSet not found — nothing to destroy"
        return 0
    fi

    echo -e "${BLUE}Destroying $STACK_NAME StackSet...${NC}"

    local permission_model
    permission_model=$(aws cloudformation describe-stack-set --stack-set-name "$STACK_NAME" $call_as \
        --query 'StackSet.PermissionModel' --output text \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "")

    local instances
    instances=$(aws cloudformation list-stack-instances --stack-set-name "$STACK_NAME" $call_as \
        --query 'Summaries[].{Account:Account,Region:Region,OU:OrganizationalUnitId}' \
        --output json \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "[]")

    if [ "$instances" != "[]" ] && [ -n "$instances" ]; then
        if [ "$permission_model" = "SERVICE_MANAGED" ]; then
            local ous_json accts_json regions_json
            ous_json=$(echo "$instances" | python3 -c "import sys,json; ous=sorted(set(i['OU'] for i in json.load(sys.stdin) if i.get('OU'))); print(','.join('\"%s\"' % o for o in ous))" 2>/dev/null || echo "")
            accts_json=$(echo "$instances" | python3 -c "import sys,json; a=sorted(set(i['Account'] for i in json.load(sys.stdin))); print(','.join('\"%s\"' % x for x in a))" 2>/dev/null || echo "")
            regions_json=$(echo "$instances" | python3 -c "import sys,json; r=sorted(set(i['Region'] for i in json.load(sys.stdin))); print(','.join('\"%s\"' % x for x in r))" 2>/dev/null || echo "")

            if [ -n "$ous_json" ] && [ -n "$accts_json" ] && [ -n "$regions_json" ]; then
                local op_id
                op_id=$(aws cloudformation delete-stack-instances \
                    --stack-set-name "$STACK_NAME" $call_as \
                    --deployment-targets "OrganizationalUnitIds=[${ous_json}],Accounts=[${accts_json}],AccountFilterType=INTERSECTION" \
                    --regions "[${regions_json}]" \
                    --no-retain-stacks \
                    --query 'OperationId' --output text \
                    --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "")

                if [ -n "$op_id" ] && [ "$op_id" != "None" ]; then
                    echo "Waiting for stack instances to be deleted (operation: $op_id)..."
                    aws cloudformation wait stack-set-operation-complete \
                        --stack-set-name "$STACK_NAME" $call_as \
                        --operation-id "$op_id" \
                        --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
                fi
            fi
        else
            local accts regions
            accts=$(echo "$instances" | python3 -c "import sys,json; print(' '.join(sorted(set(i['Account'] for i in json.load(sys.stdin)))))" 2>/dev/null || echo "")
            regions=$(echo "$instances" | python3 -c "import sys,json; print(' '.join(sorted(set(i['Region'] for i in json.load(sys.stdin)))))" 2>/dev/null || echo "")

            if [ -n "$accts" ] && [ -n "$regions" ]; then
                local op_id
                op_id=$(aws cloudformation delete-stack-instances \
                    --stack-set-name "$STACK_NAME" $call_as \
                    --accounts $accts \
                    --regions $regions \
                    --no-retain-stacks \
                    --query 'OperationId' --output text \
                    --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "")

                if [ -n "$op_id" ] && [ "$op_id" != "None" ]; then
                    echo "Waiting for stack instances to be deleted (operation: $op_id)..."
                    aws cloudformation wait stack-set-operation-complete \
                        --stack-set-name "$STACK_NAME" $call_as \
                        --operation-id "$op_id" \
                        --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || true
                fi
            fi
        fi
    fi

    # CloudFormation's list-stack-instances is eventually consistent —
    # poll for up to 5 minutes before attempting the StackSet delete.
    # The wait-stack-set-operation-complete above returns when the operation
    # finishes, but list-stack-instances can lag by several minutes.
    local remaining=""
    for _ in $(seq 1 60); do
        remaining=$(aws cloudformation list-stack-instances --stack-set-name "$STACK_NAME" $call_as \
            --query 'length(Summaries)' --output text \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "0")
        [ "$remaining" = "0" ] && break
        sleep 5
    done

    if [ "$remaining" = "0" ]; then
        if aws cloudformation delete-stack-set --stack-set-name "$STACK_NAME" $call_as \
                --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>&1; then
            print_status "$STACK_NAME StackSet destroyed"
        else
            print_warning "Could not delete $STACK_NAME StackSet (see error above)"
        fi
    else
        # Final attempt: CFN sometimes accepts delete-stack-set even when
        # list-stack-instances still shows lingering entries. Try it; if the
        # operation truly hasn't drained, this returns OperationInProgress.
        print_warning "$remaining stack instance(s) still listed after 5 minutes — attempting delete anyway"
        if aws cloudformation delete-stack-set --stack-set-name "$STACK_NAME" $call_as \
                --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>&1; then
            print_status "$STACK_NAME StackSet destroyed"
        else
            print_warning "Could not delete $STACK_NAME StackSet — instances still draining."
            print_warning "  Re-run when drained: ${BLUE}./sample-env.sh destroy${NC}"
            print_warning "  Or manually:        aws cloudformation delete-stack-set --stack-set-name $STACK_NAME $call_as"
        fi
    fi
}

destroy_hub_stack() {
    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
            --profile "$AWS_PROFILE" --region "$AWS_REGION" &>/dev/null; then
        echo -e "${BLUE}Destroying hub sample stack ($STACK_NAME)...${NC}"
        npx cdk destroy "$STACK_NAME" --exclusively --force \
            -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
        print_status "Hub sample stack destroyed"
    else
        print_status "Hub sample stack not found — nothing to destroy"
    fi
    cd ..
}

destroy_all() {
    check_prereqs
    echo -e "${RED}Destroying sample environment...${NC}"
    echo "  - Hub stack: $STACK_NAME"
    if [ "${MULTI_ACCOUNT_ENABLED:-false}" = "true" ]; then
        echo "  - Spoke StackSet: $STACK_NAME (and all instances)"
    fi
    echo ""
    read -p "Confirm (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        print_warning "Cancelled"
        exit 0
    fi

    destroy_spoke_stackset
    destroy_hub_stack

    echo ""
    print_status "Sample environment destroyed (solution infrastructure preserved)"
}

# ── Status ──────────────────────────────────────────────────────────

show_status() {
    check_prereqs
    echo -e "${BLUE}Sample environment status${NC}"
    echo "=================================================="
    echo ""

    # Hub stack
    local hub_status
    hub_status=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query 'Stacks[0].StackStatus' --output text \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "NOT_DEPLOYED")
    printf "  %-30s %s\n" "Hub stack ($STACK_NAME):" "$hub_status"

    # StackSet
    if [ "${MULTI_ACCOUNT_ENABLED:-false}" = "true" ]; then
        local call_as
        call_as=$(detect_call_as "$STACK_NAME")
        if [ "$call_as" = "MISSING" ]; then
            printf "  %-30s %s\n" "Spoke StackSet:" "NOT_DEPLOYED"
        else
            local model count
            model=$(aws cloudformation describe-stack-set --stack-set-name "$STACK_NAME" $call_as \
                --query 'StackSet.PermissionModel' --output text \
                --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "?")
            count=$(aws cloudformation list-stack-instances --stack-set-name "$STACK_NAME" $call_as \
                --query 'length(Summaries)' --output text \
                --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null || echo "?")
            printf "  %-30s %s (%s instance(s), permission model: %s)\n" "Spoke StackSet:" "DEPLOYED" "$count" "$model"
        fi
    else
        printf "  %-30s %s\n" "Spoke StackSet:" "DISABLED (MULTI_ACCOUNT_ENABLED=false)"
    fi

    echo ""
}

# ── Main ────────────────────────────────────────────────────────────

COMMAND=${1:-deploy}

case "$COMMAND" in
    ""|deploy)
        deploy_all
        ;;
    destroy)
        destroy_all
        ;;
    status)
        show_status
        ;;
    help|--help|-h)
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
