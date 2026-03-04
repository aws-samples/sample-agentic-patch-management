#!/bin/bash

# Intelligent Patch Automation — Deployment Script
# Usage: ./deploy.sh [--with-sample-env] [agent|ui|destroy|destroy --sample-only]

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Source .env if it exists
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AGENTCORE_ROLE_ARN="${AGENTCORE_ROLE_ARN:-}"

print_status() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

# ── Help ────────────────────────────────────────────────────────────

show_help() {
    echo -e "${BLUE}Intelligent Patch Automation — Deployment${NC}"
    echo "=================================================="
    echo ""
    echo "Usage: $0 [command]"
    echo ""
    echo "Deploy:"
    echo "  ${GREEN}(no args)${NC}         Deploy full solution (infra + agent + UI)"
    echo "  ${GREEN}--with-sample-env${NC}  Deploy full solution + sample EC2 environment"
    echo "  ${GREEN}agent${NC}             Redeploy agent only (after code changes)"
    echo "  ${GREEN}ui${NC}               Redeploy UI only (after code changes)"
    echo ""
    echo "Cleanup:"
    echo "  ${RED}destroy${NC}           Destroy ALL infrastructure"
    echo "  ${RED}destroy --sample-only${NC}  Destroy sample environment only"
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
        if ! command -v docker &> /dev/null; then
            print_error "Docker required (CDK builds the UI container image locally)"
            exit 1
        fi
        if ! docker info &> /dev/null; then
            print_error "Docker daemon not running. Start Docker Desktop and try again."
            exit 1
        fi
        print_status "Docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
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
    if ! aws sts get-caller-identity &> /dev/null; then
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

# ── Auto-detect AgentCore role ARN ──────────────────────────────────

resolve_agentcore_role() {
    # If already set in .env or environment, use it
    if [ -n "$AGENTCORE_ROLE_ARN" ]; then
        print_status "Using AgentCore role from config: $AGENTCORE_ROLE_ARN"
        return 0
    fi

    # Auto-detect from IAM
    echo "Detecting AgentCore role ARN..."
    local role_arn
    role_arn=$(aws iam list-roles \
        --query 'Roles[?starts_with(RoleName,`AmazonBedrockAgentCoreSDKRuntime`)].Arn' \
        --output text 2>/dev/null | head -1)

    if [ -z "$role_arn" ] || [ "$role_arn" = "None" ]; then
        print_error "Could not find AgentCore runtime role"
        echo "  Deploy the agent first, or set AGENTCORE_ROLE_ARN in .env"
        exit 1
    fi

    AGENTCORE_ROLE_ARN="$role_arn"
    print_status "Auto-detected AgentCore role: $AGENTCORE_ROLE_ARN"
}

# ── Validate and fix memory IAM permissions ────────────────────────

_fix_memory_iam_if_needed() {
    local account_id="$1"

    # Extract memory_id from the yaml
    local memory_id
    memory_id=$(grep "memory_id:" .bedrock_agentcore.yaml 2>/dev/null | head -1 | awk '{print $2}' | tr -d "'\"")
    if [ -z "$memory_id" ] || [ "$memory_id" = "null" ]; then
        print_warning "No memory_id in yaml — skipping IAM validation"
        return 0
    fi

    # Find the runtime role name
    local role_name
    role_name=$(aws iam list-roles \
        --query 'Roles[?starts_with(RoleName,`AmazonBedrockAgentCoreSDKRuntime`)].RoleName' \
        --output text 2>/dev/null | head -1)
    if [ -z "$role_name" ]; then
        print_warning "Could not find AgentCore runtime role — skipping IAM validation"
        return 0
    fi

    # Check if the IAM policy references the correct memory_id
    local policy_name="BedrockAgentCoreRuntimeExecutionPolicy-intelligent_patch_automation"
    local policy_doc
    policy_doc=$(aws iam get-role-policy --role-name "$role_name" --policy-name "$policy_name" \
        --query 'PolicyDocument' --output json 2>/dev/null)

    if [ -z "$policy_doc" ]; then
        print_warning "Could not read IAM policy $policy_name — skipping validation"
        return 0
    fi

    local memory_arn="arn:aws:bedrock-agentcore:${AWS_REGION}:${account_id}:memory/${memory_id}"

    if echo "$policy_doc" | grep -q "$memory_id"; then
        print_status "IAM policy references correct memory: $memory_id"
        return 0
    fi

    # Policy references a different (stale) memory — fix it
    print_warning "IAM policy references stale memory — updating to $memory_id"

    # Replace any memory ARN in the policy with the correct one
    local fixed_doc
    fixed_doc=$(echo "$policy_doc" | python3 -c "
import json, sys, re
doc = json.load(sys.stdin)
doc_str = json.dumps(doc)
# Replace any memory ARN with the correct one
doc_str = re.sub(
    r'arn:aws:bedrock-agentcore:[^:]+:[^:]+:memory/[a-zA-Z0-9_-]+',
    '${memory_arn}',
    doc_str
)
print(doc_str)
")

    if [ -z "$fixed_doc" ]; then
        print_warning "Could not fix IAM policy — update manually"
        return 0
    fi

    echo "$fixed_doc" > /tmp/patchy_fixed_policy.json
    aws iam put-role-policy \
        --role-name "$role_name" \
        --policy-name "$policy_name" \
        --policy-document "file:///tmp/patchy_fixed_policy.json" 2>/dev/null

    if [ $? -eq 0 ]; then
        print_status "IAM policy updated with correct memory ARN"
        rm -f /tmp/patchy_fixed_policy.json
    else
        print_warning "Failed to update IAM policy — update manually"
        rm -f /tmp/patchy_fixed_policy.json
    fi
}

# ── Deploy functions ────────────────────────────────────────────────

deploy_agent() {
    echo -e "${BLUE}Deploying agent...${NC}"
    cd agent
    source ../venv/bin/activate
    if ! command -v agentcore &> /dev/null; then
        pip install bedrock-agentcore -q
        print_status "AgentCore CLI installed"
    fi

    # Detect current account and check for stale config from a different account
    local current_account
    current_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)

    if [ -f ".bedrock_agentcore.yaml" ]; then
        local config_account
        config_account=$(grep -A1 "account:" .bedrock_agentcore.yaml | tail -1 | tr -d " '\"" | sed 's/account://')
        if [ -n "$config_account" ] && [ "$config_account" != "$current_account" ]; then
            print_warning "Stale .bedrock_agentcore.yaml detected (account: $config_account, current: $current_account)"
            print_warning "Removing stale config — agentcore configure will create a fresh one for this account."
            rm -f .bedrock_agentcore.yaml
        fi
    fi

    # First-time setup: generate .bedrock_agentcore.yaml + Dockerfile if they don't exist.
    # agentcore configure creates the memory resource, IAM role, ECR repo, and writes
    # the config yaml + generated Dockerfile. On subsequent deploys, agentcore launch
    # reads the existing yaml — no need to re-configure.
    if [ ! -f ".bedrock_agentcore.yaml" ]; then
        print_warning "No .bedrock_agentcore.yaml found — running first-time configuration"
        echo "  This creates the AgentCore runtime, memory resource, IAM role, and ECR repo."
        agentcore configure \
            --entrypoint supervisor.py \
            --name intelligent_patch_automation \
            --non-interactive
        print_status "AgentCore configuration created"
        FIRST_DEPLOY=true
    else
        FIRST_DEPLOY=false
    fi

    agentcore launch --auto-update-on-conflict
    print_status "Agent deployed"

    # ── Post-deploy: ensure memory is fully configured ──────────────
    # agentcore configure defaults to STM_ONLY. We need STM_AND_LTM for
    # cross-session recall. This block handles it automatically so the
    # customer doesn't need to manually edit yaml or run extra scripts.

    # 1. Upgrade memory mode in yaml if it's STM_ONLY
    if grep -q "mode: STM_ONLY" .bedrock_agentcore.yaml 2>/dev/null; then
        print_warning "Memory mode is STM_ONLY — upgrading to STM_AND_LTM"
        sed -i.bak 's/mode: STM_ONLY/mode: STM_AND_LTM/' .bedrock_agentcore.yaml
        rm -f .bedrock_agentcore.yaml.bak
        print_status "Memory mode upgraded to STM_AND_LTM"
    fi

    # 2. Set up LTM strategies on first deploy (idempotent — skips if already configured)
    if [ "$FIRST_DEPLOY" = true ]; then
        echo -e "${BLUE}Setting up long-term memory strategies...${NC}"
        python setup_memory_strategies.py || print_warning "Memory strategies setup failed — run manually later: python agent/setup_memory_strategies.py"
    fi

    # 3. Upload baseline overrides (idempotent — always runs, just 4 small S3 puts)
    echo -e "${BLUE}Uploading severity-scoped baseline overrides to S3...${NC}"
    python setup_baseline_overrides.py || print_warning "Baseline overrides upload failed — run manually later: python agent/setup_baseline_overrides.py"

    # 3. Validate IAM policy grants memory access for the correct memory_id
    _fix_memory_iam_if_needed "$current_account"

    cd ..
}

deploy_infra() {
    local include_demo=${1:-false}

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

    if [ "$include_demo" = true ]; then
        stacks="$stacks Patchy-SampleEnv"
        stack_count=$((stack_count + 1))
        echo -e "${BLUE}Deploying infrastructure ($stack_count stacks, including sample environment)...${NC}"
    else
        echo -e "${BLUE}Deploying infrastructure ($stack_count stacks)...${NC}"
    fi

    npx cdk deploy $stacks \
        --exclusively \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Infrastructure deployed ($stack_count stacks)"
    cd ..
}

deploy_ui() {
    echo -e "${BLUE}Deploying Web UI (Fargate + Cognito + public ALB)...${NC}"

    # Pre-flight: agent config must exist (Docker build bakes it into the image)
    if [ ! -f "agent/.bedrock_agentcore.yaml" ]; then
        print_error "agent/.bedrock_agentcore.yaml not found"
        echo "  The UI container needs the agent config at build time."
        echo "  Deploy the agent first: ./deploy.sh agent"
        exit 1
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
            # Reload .env to pick up the new ACM_CERTIFICATE_ARN
            if [ -f ".env" ]; then
                set -a; source .env; set +a
            fi
        fi

        if [ -z "$ACM_CERTIFICATE_ARN" ]; then
            print_error "ACM_CERTIFICATE_ARN still not set after setup-tls.sh. Check for errors above."
            exit 1
        fi
        export ACM_CERTIFICATE_ARN
        print_status "TLS certificate: $ACM_CERTIFICATE_ARN"

        # ── Auto-derive Cognito domain prefix ──────────────────────────
        # Domain prefix must be globally unique — use account ID as suffix.
        if [ -z "$COGNITO_DOMAIN_PREFIX" ]; then
            local account_id
            account_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
            COGNITO_DOMAIN_PREFIX="patchy-${account_id}"
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

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    ensure_cdk_bootstrap

    npx cdk deploy Patchy-UI \
        --exclusively \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Web UI deployed"

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
                --logout-urls "https://${alb_dns}" "https://${alb_dns_lower}" \
                > /dev/null 2>&1
            print_status "Cognito callback URLs updated (added lowercase: ${alb_dns_lower})"
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
        echo ""
        echo -e "Next steps:"
        echo -e "  1. Open the URL above in your browser"
        echo -e "  2. Create users in Cognito: ${BLUE}aws cognito-idp admin-create-user --user-pool-id \$(aws cloudformation describe-stacks --stack-name Patchy-UI --query 'Stacks[0].Outputs[?OutputKey==\`CognitoUserPoolId\`].OutputValue' --output text) --username <email> --user-attributes Name=email,Value=<email> --temporary-password <TempPass1!>${NC}"
        echo -e "  3. Add users to groups: operators (full access) or viewers (read-only)"
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

destroy() {
    local demo_only=${1:-false}

    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi

    if [ "$demo_only" = true ]; then
        echo -e "${RED}Destroying sample environment...${NC}"
        npx cdk destroy Patchy-SampleEnv --exclusively --force \
            -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
        print_status "Sample environment destroyed (solution infrastructure preserved)"
    else
        echo -e "${RED}Destroying ALL infrastructure...${NC}"
        read -p "Are you sure? This removes everything including VPC, IAM, S3. (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            print_warning "Cancelled"
            cd ..
            exit 0
        fi
        npx cdk destroy --all --force \
            -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
        print_status "All infrastructure destroyed"
    fi
    cd ..
}

# ── Main ────────────────────────────────────────────────────────────

COMMAND=${1:-}
FLAG=${2:-}

case "$COMMAND" in
    ""|"--with-sample-env")
        # Full deploy: env → agent → infra → UI
        [ "$COMMAND" = "--with-sample-env" ] && INCLUDE_DEMO=true || INCLUDE_DEMO=false

        check_prereqs true true true
        echo -e "${BLUE}Intelligent Patch Automation — Full Deploy${NC}"
        echo "=================================================="

        setup_environment
        deploy_agent
        deploy_infra $INCLUDE_DEMO
        deploy_ui

        echo ""
        echo -e "${GREEN}Deployment complete.${NC}"
        echo "=================================================="
        ;;

    "agent")
        check_prereqs true false
        echo -e "${BLUE}Intelligent Patch Automation — Redeploy Agent${NC}"
        echo "=================================================="
        setup_environment
        deploy_agent
        ;;

    "ui")
        check_prereqs false true true
        echo -e "${BLUE}Intelligent Patch Automation — Redeploy UI${NC}"
        echo "=================================================="
        deploy_ui
        ;;

    "destroy")
        check_prereqs false true
        if [ "$FLAG" = "--sample-only" ]; then
            destroy true
        else
            destroy false
        fi
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
