#!/bin/bash

# Intelligent Patch Automation — Deployment Script
# Usage: ./deploy.sh [all|infra|solution|agent|frontend|env|destroy-demo|destroy-all]

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

DEPLOY_TARGET=${1:-}

if [ -z "$DEPLOY_TARGET" ]; then
    echo -e "${BLUE}Intelligent Patch Automation — Deployment${NC}"
    echo "=================================================="
    echo ""
    echo "Usage: $0 <target>"
    echo ""
    echo "Solution:"
    echo "  ${GREEN}all${NC}           Deploy everything (env + infra + sample env + agent)"
    echo "  ${GREEN}solution${NC}      Deploy solution only (env + core infra + agent, no sample instances)"
    echo "  ${GREEN}env${NC}           Setup Python virtual environment"
    echo "  ${GREEN}infra${NC}         Deploy all infrastructure (solution + sample environment)"
    echo "  ${GREEN}agent${NC}         Deploy AgentCore agent"
    echo ""
    echo "Cleanup:"
    echo "  ${RED}destroy-demo${NC}  Destroy sample environment only (keeps solution infra)"
    echo "  ${RED}destroy-all${NC}   Destroy ALL infrastructure"
    echo ""
    echo "Examples:"
    echo "  $0 solution       # Deploy for production (no sample instances)"
    echo "  $0 all            # Deploy with sample instances for testing"
    echo "  $0 agent          # Redeploy agent only"
    echo "  $0 destroy-demo   # Remove sample instances (quick cleanup)"
    echo ""
    exit 0
fi

echo -e "${BLUE}Intelligent Patch Automation — Target: ${DEPLOY_TARGET}${NC}"
echo "=================================================="

print_status() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

check_prereqs() {
    case $DEPLOY_TARGET in
        "env"|"all"|"solution")
            if ! command -v python3 &> /dev/null; then
                print_error "Python 3 required"
                exit 1
            fi
            ;;
        "infra")
            if ! command -v node &> /dev/null; then
                print_error "Node.js required for CDK"
                exit 1
            fi
            ;;
        "agent")
            if [ ! -d "venv" ]; then
                print_error "Virtual environment required. Run: ./deploy.sh env"
                exit 1
            fi
            ;;
    esac
}

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
        print_warning "AWS credentials not configured. Run: aws configure --profile $AWS_PROFILE"
        exit 1
    fi
    print_status "AWS environment ready"
}

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

deploy_infra_solution() {
    echo -e "${BLUE}Deploying solution infrastructure (3 stacks)...${NC}"
    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    if [ -z "$AGENTCORE_ROLE_ARN" ]; then
        print_error "AGENTCORE_ROLE_ARN is required for infrastructure deployment"
        echo "  Set it in .env or: export AGENTCORE_ROLE_ARN=arn:aws:iam::<account>:role/<role>"
        echo "  Find it: aws iam list-roles --query 'Roles[?starts_with(RoleName,\`AmazonBedrockAgentCoreSDKRuntime\`)].Arn' --output text"
        exit 1
    fi
    ensure_cdk_bootstrap
    npx cdk deploy \
        PatchAutomationNetworking \
        PatchAutomationSecurity \
        PatchAutomationAgentCorePermissions \
        --require-approval never \
        -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "Solution infrastructure deployed (3 stacks)"
    cd ..
}

deploy_infra_all() {
    echo -e "${BLUE}Deploying all infrastructure (solution + sample environment)...${NC}"
    cd infra
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
    fi
    if [ -z "$AGENTCORE_ROLE_ARN" ]; then
        print_error "AGENTCORE_ROLE_ARN is required for infrastructure deployment"
        echo "  Set it in .env or: export AGENTCORE_ROLE_ARN=arn:aws:iam::<account>:role/<role>"
        echo "  Find it: aws iam list-roles --query 'Roles[?starts_with(RoleName,\`AmazonBedrockAgentCoreSDKRuntime\`)].Arn' --output text"
        exit 1
    fi
    ensure_cdk_bootstrap
    npx cdk deploy --all --require-approval never -c agentCoreRoleArn="$AGENTCORE_ROLE_ARN"
    print_status "All infrastructure deployed (4 stacks)"
    cd ..
}

destroy_demo() {
    echo -e "${RED}Destroying sample environment...${NC}"
    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi
    npx cdk destroy PatchAutomationVulnerabilityDriven --force -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
    print_status "Sample environment destroyed (solution infrastructure preserved)"
    cd ..
}

destroy_all() {
    echo -e "${RED}Destroying ALL infrastructure...${NC}"
    read -p "Are you sure? This removes everything including VPC, IAM, S3. (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        print_warning "Cancelled"
        exit 0
    fi
    cd infra
    if [ ! -d "node_modules" ]; then npm ci --silent; fi
    npx cdk destroy --all --force -c agentCoreRoleArn="${AGENTCORE_ROLE_ARN:-placeholder}"
    print_status "All infrastructure destroyed"
    cd ..
}

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

main() {
    check_prereqs
    case $DEPLOY_TARGET in
        "env")
            setup_environment
            ;;
        "solution")
            setup_environment
            deploy_infra_solution
            deploy_agent
            ;;
        "infra")
            deploy_infra_all
            ;;
        "agent")
            setup_environment
            deploy_agent
            ;;
        "destroy-demo")
            destroy_demo
            ;;
        "destroy-all")
            destroy_all
            ;;
        "all")
            setup_environment
            deploy_infra_all
            deploy_agent
            ;;
        *)
            print_error "Invalid target: $DEPLOY_TARGET"
            echo "Usage: $0 [all|solution|infra|agent|env|destroy-demo|destroy-all]"
            exit 1
            ;;
    esac
}

main

echo -e "\n${GREEN}Done: ${DEPLOY_TARGET}${NC}"
echo "=================================================="
echo -e "AWS Profile: ${BLUE}$AWS_PROFILE${NC} | Region: ${BLUE}$AWS_REGION${NC}"
echo ""
echo "Quick commands:"
echo "  agentcore status                    # Check agent"
echo "  agentcore invoke '{\"prompt\": \"...\"}' # Test agent"
echo "  cd ui/api && python3 server.py      # Start web UI backend"
echo "  cd ui/frontend && npm run dev       # Start web UI frontend"
