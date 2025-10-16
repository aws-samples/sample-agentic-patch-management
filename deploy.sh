#!/bin/bash

# COP407 Intelligent Patch Automation - Fast Deployment Script
# Usage: ./deploy.sh [all|infra|agent|frontend|env]

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
AWS_PROFILE="ri25-demo"
AWS_REGION="us-east-1"
PYTHON_VERSION="3.9"

# Parse deployment target
DEPLOY_TARGET=${1:-all}

echo -e "${BLUE}🚀 COP407 Fast Deployment - Target: ${DEPLOY_TARGET}${NC}"
echo "=================================================="

# Function to print status
print_status() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_skip() {
    echo -e "${YELLOW}⏭️  Skipping $1${NC}"
}

# Fast prerequisite check (only what's needed)
check_prereqs() {
    case $DEPLOY_TARGET in
        "env"|"all")
            if ! command -v python3 &> /dev/null; then
                print_error "Python 3 required for environment setup"
                exit 1
            fi
            ;;
        "infra")
            if ! command -v node &> /dev/null; then
                print_error "Node.js required for infrastructure deployment"
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

# Environment setup (fast)
setup_environment() {
    echo -e "${BLUE}🐍 Environment setup...${NC}"
    
    # Quick venv check/create
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        print_status "Virtual environment created"
    fi
    
    source venv/bin/activate
    
    # Fast dependency install (skip if already installed)
    if [ ! -f "venv/.deps_installed" ]; then
        pip install --upgrade pip -q
        pip install -r agent/requirements.txt -q
        touch venv/.deps_installed
        print_status "Dependencies installed"
    else
        print_status "Dependencies already installed"
    fi
    
    # AWS profile setup (fast check)
    export AWS_PROFILE=$AWS_PROFILE
    export AWS_DEFAULT_REGION=$AWS_REGION
    export AWS_DEFAULT_PROFILE=$AWS_PROFILE
    
    if ! aws sts get-caller-identity &> /dev/null; then
        print_warning "AWS credentials not configured. Run: aws configure --profile $AWS_PROFILE"
        exit 1
    fi
    print_status "AWS environment ready"
}

# Infrastructure deployment (optimized)
deploy_infrastructure() {
    echo -e "${BLUE}🏗️  Infrastructure deployment...${NC}"
    
    cd infra
    
    # Fast npm install (skip if node_modules exists and package.json unchanged)
    if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
        npm ci --silent
        print_status "CDK dependencies ready"
    else
        print_status "CDK dependencies up to date"
    fi
    
    # Skip bootstrap check for speed (assume it's done)
    echo "Deploying CDK stacks..."
    npx cdk deploy --all --require-approval never --concurrency 10
    print_status "Infrastructure deployed"
    
    cd ..
}

# Agent deployment (fast)
deploy_agent() {
    echo -e "${BLUE}🤖 Agent deployment...${NC}"
    
    cd agent
    source ../venv/bin/activate
    
    # Fast agentcore install check
    if ! command -v agentcore &> /dev/null; then
        pip install bedrock-agentcore -q
        print_status "AgentCore CLI installed"
    fi
    
    # Deploy with auto-update (fastest option)
    agentcore launch --auto-update-on-conflict
    print_status "Agent deployed"
    
    cd ..
}

# Frontend deployment (placeholder)
deploy_frontend() {
    echo -e "${BLUE}🌐 Frontend deployment...${NC}"
    print_warning "Frontend deployment not implemented yet"
}

# Fast testing (minimal)
run_tests() {
    print_skip "tests (disabled for speed)"
}

# Main execution logic
main() {
    check_prereqs
    
    case $DEPLOY_TARGET in
        "env")
            setup_environment
            ;;
        "infra")
            deploy_infrastructure
            ;;
        "agent")
            setup_environment
            deploy_agent
            ;;
        "frontend")
            deploy_frontend
            ;;
        "all")
            setup_environment
            deploy_infrastructure
            deploy_agent
            ;;
        *)
            print_error "Invalid target: $DEPLOY_TARGET"
            echo "Usage: $0 [all|infra|agent|frontend|env]"
            exit 1
            ;;
    esac
}

# Run main function
main

# Display summary
echo -e "\n${GREEN}🎉 Deployment Complete: ${DEPLOY_TARGET}${NC}"
echo "=================================================="
echo -e "Target: ${BLUE}$DEPLOY_TARGET${NC}"
echo -e "AWS Profile: ${BLUE}$AWS_PROFILE${NC}"
echo -e "AWS Region: ${BLUE}$AWS_REGION${NC}"
echo ""
echo -e "${YELLOW}Quick Commands:${NC}"
echo "• Status: cd agent && agentcore status"
echo "• Test: agentcore invoke '{\"prompt\": \"Hello\"}'"
echo "• Logs: aws logs tail /aws/bedrock-agentcore/runtimes/intelligent_patch_automation-TzHdGwD6sO-DEFAULT --follow"
echo ""
echo -e "${BLUE}Fast Deployments:${NC}"
echo "• Environment only: ./deploy.sh env"
echo "• Infrastructure only: ./deploy.sh infra"  
echo "• Agent only: ./deploy.sh agent"
echo "• Everything: ./deploy.sh all"
