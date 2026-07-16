#!/bin/bash

# Connect to the internal Patch Automation UI via SSM port forwarding.
# Opens https://localhost:8443 (or http://localhost:8080 if no TLS cert).
#
# Prerequisites:
#   - AWS CLI v2 with Session Manager plugin
#   - At least one SSM-managed EC2 instance in the VPC
#   - UI stack deployed: ./deploy.sh ui
#
# Usage: ./connect-ui.sh
#   Then open https://localhost:8443 in your browser.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
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
export AWS_PROFILE AWS_DEFAULT_REGION="$AWS_REGION"

# Check prerequisites
if ! command -v session-manager-plugin &> /dev/null; then
    echo -e "${RED}Session Manager plugin not found.${NC}"
    echo "Install: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
    exit 1
fi

# Get ALB DNS from CloudFormation
echo "Looking up internal ALB..."
ALB_DNS=$(aws cloudformation describe-stacks \
    --stack-name Patchy-UI \
    --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' \
    --output text 2>/dev/null)

if [ -z "$ALB_DNS" ] || [ "$ALB_DNS" = "None" ]; then
    echo -e "${RED}UI stack not found. Deploy first: ./deploy.sh ui${NC}"
    exit 1
fi

# Determine port based on protocol
if [[ "$ALB_DNS" == https://* ]]; then
    REMOTE_PORT=443
    LOCAL_PORT=8443
    LOCAL_URL="https://localhost:${LOCAL_PORT}"
else
    REMOTE_PORT=80
    LOCAL_PORT=8080
    LOCAL_URL="http://localhost:${LOCAL_PORT}"
fi

# Strip protocol prefix to get bare hostname
ALB_HOST="${ALB_DNS#https://}"
ALB_HOST="${ALB_HOST#http://}"

# Find the bastion instance (deployed by Patchy-UI stack)
echo "Finding bastion instance..."
INSTANCE_ID=$(aws cloudformation describe-stacks \
    --stack-name Patchy-UI \
    --query 'Stacks[0].Outputs[?OutputKey==`BastionInstanceId`].OutputValue' \
    --output text 2>/dev/null)

# Fallback: find any SSM-managed instance in the ALB's VPC
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
    echo "No bastion found. Finding SSM instance in ALB VPC..."
    ALB_VPC=$(aws elbv2 describe-load-balancers \
        --query "LoadBalancers[?DNSName=='${ALB_HOST}'].VpcId" \
        --output text 2>/dev/null)

    if [ -n "$ALB_VPC" ] && [ "$ALB_VPC" != "None" ]; then
        VPC_INSTANCES=$(aws ec2 describe-instances \
            --filters "Name=vpc-id,Values=${ALB_VPC}" "Name=instance-state-name,Values=running" \
            --query 'Reservations[*].Instances[*].InstanceId' \
            --output text 2>/dev/null)
        for iid in $VPC_INSTANCES; do
            STATUS=$(aws ssm describe-instance-information \
                --filters "Key=InstanceIds,Values=${iid}" \
                --query 'InstanceInformationList[0].PingStatus' \
                --output text 2>/dev/null)
            if [ "$STATUS" = "Online" ]; then
                INSTANCE_ID="$iid"
                break
            fi
        done
    fi
fi

if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
    echo -e "${RED}No online SSM-managed instances found.${NC}"
    echo "Deploy the sample environment: ./sample-env.sh deploy"
    exit 1
fi

echo -e "${GREEN}ALB:${NC}      ${ALB_HOST}:${REMOTE_PORT}"
echo -e "${GREEN}Instance:${NC} ${INSTANCE_ID}"
echo -e "${GREEN}Local:${NC}    ${LOCAL_URL}"
echo ""
echo -e "${BLUE}Starting SSM port forward... (Ctrl+C to stop)${NC}"
echo ""

aws ssm start-session \
    --target "$INSTANCE_ID" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"${ALB_HOST}\"],\"portNumber\":[\"${REMOTE_PORT}\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"