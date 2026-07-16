#!/usr/bin/env python3
"""One-time setup: Create a sample Bedrock Guardrail for the patch automation agent.

Creates a minimal guardrail with:
  1. Denied topic: non-patching queries (general knowledge, personal advice, etc.)
  2. Content filter: blocks harmful/violent/sexual content at HIGH threshold
  3. Sensitive info filter: masks AWS account IDs and IP addresses in logs

This is a SAMPLE implementation. Enterprise customers should extend with:
  - Additional denied topics for their compliance requirements
  - PII redaction policies (SSN, credit card, etc.)
  - Custom word filters for organization-specific terms
  - Stricter content filter thresholds

Usage:
    python setup_guardrail.py

Outputs the guardrail ID and version to add to config.
"""

import os
import sys
import json
import logging
import boto3

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

GUARDRAIL_NAME = "patch-automation-guardrail"
REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))


def main():
    client = boto3.client('bedrock', region_name=REGION)

    # Check if guardrail already exists
    try:
        existing = client.list_guardrails(maxResults=100)
        for g in existing.get('guardrails', []):
            if g['name'] == GUARDRAIL_NAME:
                gid = g['id']
                ver = g['version']
                logger.info(f"Guardrail already exists: id={gid} version={ver}")
                logger.info(f"Add to agent config: GUARDRAIL_ID={gid} GUARDRAIL_VERSION={ver}")
                return gid, ver
    except Exception as e:
        logger.warning(f"Could not check existing guardrails: {e}")

    logger.info("Creating guardrail...")

    response = client.create_guardrail(
        name=GUARDRAIL_NAME,
        description=(
            "Sample guardrail for patch automation agent. "
            "Blocks off-topic queries and filters harmful content. "
            "Extend with additional policies for your compliance requirements."
        ),
        topicPolicyConfig={
            'topicsConfig': [
                {
                    'name': 'off-topic-queries',
                    'definition': (
                        'Queries unrelated to patch management, vulnerability scanning, '
                        'compliance reporting, or EC2 fleet operations. This includes '
                        'general knowledge questions, personal advice, creative writing, '
                        'coding help unrelated to patching, and questions about other '
                        'AWS services not involved in the patching workflow.'
                    ),
                    'examples': [
                        'What is the capital of France?',
                        'Write me a poem about clouds',
                        'How do I set up a DynamoDB table?',
                        'What are the best practices for S3 bucket policies?',
                        'Tell me a joke',
                    ],
                    'type': 'DENY'
                }
            ]
        },
        contentPolicyConfig={
            'filtersConfig': [
                {
                    'type': 'SEXUAL',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'HIGH'
                },
                {
                    'type': 'VIOLENCE',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'HIGH'
                },
                {
                    'type': 'HATE',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'HIGH'
                },
                {
                    'type': 'INSULTS',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'HIGH'
                },
                {
                    'type': 'MISCONDUCT',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'HIGH'
                },
                {
                    'type': 'PROMPT_ATTACK',
                    'inputStrength': 'HIGH',
                    'outputStrength': 'NONE'
                }
            ]
        },
        sensitiveInformationPolicyConfig={
            'regexesConfig': [
                {
                    'name': 'AWSAccountId',
                    'description': 'AWS Account ID (12 digits)',
                    'pattern': r'\b\d{12}\b',
                    'action': 'ANONYMIZE'
                },
                {
                    'name': 'IPv4Address',
                    'description': 'IPv4 addresses',
                    'pattern': r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
                    'action': 'ANONYMIZE'
                }
            ]
        },
        blockedInputMessaging=(
            "I can only help with patch management, vulnerability scanning, "
            "and compliance reporting for your EC2 fleet. "
            "Please ask a question related to these topics."
        ),
        blockedOutputMessaging=(
            "I'm unable to provide that response. "
            "Please rephrase your question about patch management or fleet operations."
        )
    )

    guardrail_id = response['guardrailId']
    logger.info(f"Created guardrail: {guardrail_id}")

    # Create a version (required for use)
    ver_response = client.create_guardrail_version(
        guardrailIdentifier=guardrail_id,
        description='Initial version — sample policies for patch automation pilot'
    )
    version = ver_response['version']

    logger.info(f"Created version: {version}")
    logger.info(f"")
    logger.info(f"Add these environment variables to enable the guardrail:")
    logger.info(f"  GUARDRAIL_ID={guardrail_id}")
    logger.info(f"  GUARDRAIL_VERSION={version}")
    logger.info(f"")
    logger.info(f"Or set in agentcore/agentcore.json under the agent config.")
    logger.info(f"The agent will automatically pick these up on next deployment.")

    return guardrail_id, version


if __name__ == '__main__':
    main()
