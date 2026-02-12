#!/bin/bash

# Intelligent Patch Automation — Deployment Script
# Usage: ./deploy.sh [--with-demo] [agent|ui|destroy|destroy --demo-only]

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
    echo "  ${GREEN}--with-demo${NC}       Deploy full solution + sample EC2 environment"
    echo "  ${GREEN}agent${NC}             Redeploy agent only (after code changes)"
    echo "  ${GREEN}ui${NC}               Redeploy UI only (after code changes)"
    echo ""
    echo "Cleanup:"
    echo "  ${RED}destroy${NC}           Destroy ALL infrastructure"
    echo "  ${RED}destroy --demo-only${NC}  Destroy sample environment only"
    echo ""
    echo "First-time setup:"
    echo "  1. cp .env.example .env"
    echo "  2. Edit .env — set AWS_PROFILE and AWS_REGION"
    echo "  3. ./deploy.sh"
    echo ""
    echo "That's it. The script handles agent deployment, role detection,"
    echo "CDK bootstrap, infrastructure, and UI automatically."
    echo ""
}

# ── Prereq checks ──────────────────────────────────────────────────

check_prereqs() {
    local need_python=${1:-false}
    local need_node=${2:-false}

    if [ "$need_python" = true ] && ! command -v python3 &> /dev/null; then
        print_error "Python 3 required"
        exit 1
    fi
    if [ "$need_node" = true ] && ! command -v node &> /dev/null; then
        print_error "Node.js 18+ required (for CDK)"
        exit 1
    fi
    if ! command -v aws &> /dev/null; then
        print_error "AWS CLI v2 required"
        exit 1
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
    echo "Checking CDK bootstrap..."
    if ! npx cdk bootstrap --show-template &> /dev/null 2>&1; then
        echo "Bootstrapping CDK..."
        npx cdk bootstrap "aws://${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}/${AWS_REGION}"
        print_status "CDK bootstrapped"
    else
        print_status "CDK already bootstrapped"
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

# ── Deploy functions ────────────────────────────────────────────────

deploy_agent() {
    echo -e "${BLUE}Deploying agent...${NC}"
    cd agent
    source ../venv/bin/activate
    if ! command -v agentcore &> /dev/null; then
        pip install bedrock-agentcore -q
        print_status "AgentCore CLI installed"
    fi
    agentcore launch --auto-update-on-conflict
    print_status "Agent deployed"
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

    local stacks="PatchAutomationNetworking PatchAutomationSecurity PatchAutomationAgentCorePermissions"
    local stack_count=3

    if [ "$include_demo" = true ]; then
        stacks="$stacks PatchAutomationVulnerabilityDriven"
        stack_count=4
        echo -e "${BLUE}Deploying infrastructure ($stack_count stacks, including sample environment)...${NC}"
    else
        echo -e "${BLUE}Deploying infrastructure ($stack_count stacks)...${NC}"
    fi

    npx cdk deploy $stacks \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Infrastructure deployed ($stack_count stacks)"
    cd ..
}

deploy_ui() {
    echo -e "${BLUE}Deploying Web UI (Fargate + ALB)...${NC}"

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

    resolve_agentcore_role

    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    ensure_cdk_bootstrap
    npx cdk deploy PatchAutomationUI \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Web UI deployed"

    echo ""
    echo -e "${GREEN}UI URL:${NC}"
    aws cloudformation describe-stacks \
        --stack-name PatchAutomationUI \
        --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' \
        --output text 2>/dev/null || echo "  (check CloudFormation outputs)"
    cd ..
}

destroy() {
    local demo_only=${1:-false}

    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi

    if [ "$demo_only" = true ]; then
        echo -e "${RED}Destroying sample environment...${NC}"
        npx cdk destroy PatchAutomationVulnerabilityDriven --force \
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
    ""|"--with-demo")
        # Full deploy: env → agent → infra → UI
        [ "$COMMAND" = "--with-demo" ] && INCLUDE_DEMO=true || INCLUDE_DEMO=false

        check_prereqs true true
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
        check_prereqs false true
        echo -e "${BLUE}Intelligent Patch Automation — Redeploy UI${NC}"
        echo "=================================================="
        deploy_ui
        ;;

    "destroy")
        check_prereqs false true
        if [ "$FLAG" = "--demo-only" ]; then
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
