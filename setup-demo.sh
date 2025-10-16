#!/bin/bash

# Simple demo setup script
set -e

echo "🚀 Setting up demo environment..."

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✅ Virtual environment activated"
else
    echo "❌ Virtual environment not found. Run ./deploy.sh env first"
    exit 1
fi

# Set AWS profile
export AWS_PROFILE=ri25-demo
echo "✅ AWS profile set to: $AWS_PROFILE"

# Navigate to agent directory
cd agent

echo "🎯 Ready for demo! Use:"
echo "   agentcore invoke '{\"prompt\": \"Your query here\"}'"
