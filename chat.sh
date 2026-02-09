#!/bin/bash
# Patch Automation Chat Interface
# Wrapper for the Python chat client

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# Get project root BEFORE changing directories
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Source .env if it exists
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Check virtual environment
if [ ! -d "$PROJECT_ROOT/venv" ]; then
    echo -e "${RED}❌ Virtual environment not found${NC}"
    echo "Run: ./deploy.sh env"
    exit 1
fi

# Activate virtual environment
source "$PROJECT_ROOT/venv/bin/activate"

# Change to agent directory (where .bedrock_agentcore.yaml exists)
cd "$PROJECT_ROOT/agent"

# Check agentcore CLI
if ! command -v agentcore &> /dev/null; then
    echo -e "${RED}❌ AgentCore CLI not found${NC}"
    echo "Installing..."
    pip install bedrock-agentcore -q
    echo -e "${GREEN}✅ Installed${NC}"
fi

# Parse arguments
if [ "$1" = "-d" ] || [ "$1" = "--debug" ]; then
    # Debug mode
    python ../chatbot-client/src/patch_client.py --debug
elif [ "$1" = "-m" ] || [ "$1" = "--message" ]; then
    # Single message mode
    shift
    python ../chatbot-client/src/patch_client.py --message "$*"
elif [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    # Show help
    python ../chatbot-client/src/patch_client.py --help
else
    # Interactive mode (default)
    python ../chatbot-client/src/patch_client.py "$@"
fi

