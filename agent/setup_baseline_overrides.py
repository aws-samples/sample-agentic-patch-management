#!/usr/bin/env python3
"""One-time setup: Upload severity-scoped baseline override JSON files to S3.

These files are used by patch_dry_run and execute_patch_operation when a
severity filter is requested. AWS-RunPatchBaseline accepts a BaselineOverride
parameter pointing to an S3 URL — this overrides the default baseline rules
at runtime without changing the instance's patch group mapping.

Usage:
    python setup_baseline_overrides.py

Requires:
    - AWS credentials configured (same profile as agent deployment)
    - The compliance reports S3 bucket must exist (created by Patchy-Core stack)
"""

import boto3
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

# Severity-scoped baseline overrides for Amazon Linux 2
# Each override approves patches at the specified severity levels with 0-day delay.
OVERRIDES = {
    "critical-only": {
        "description": "Critical severity patches only",
        "severities": ["Critical"]
    },
    "high-and-above": {
        "description": "Critical + Important (High) severity patches",
        "severities": ["Critical", "Important"]
    },
    "medium-and-above": {
        "description": "Critical + Important + Medium severity patches",
        "severities": ["Critical", "Important", "Medium"]
    },
    "all-severities": {
        "description": "All severity levels",
        "severities": ["Critical", "Important", "Medium", "Low"]
    },
}


def build_override_json(severities: list) -> list:
    """Build a BaselineOverride JSON structure for the given severities.
    
    Returns a LIST of baseline objects (AWS requires an array, not a single object).
    All fields must be explicitly typed — SSM rejects null values for booleans/strings.
    """
    return [{
        "OperatingSystem": "AMAZON_LINUX_2",
        "ApprovalRules": {
            "PatchRules": [{
                "PatchFilterGroup": {
                    "PatchFilters": [
                        {"Key": "SEVERITY", "Values": severities},
                        {"Key": "CLASSIFICATION", "Values": ["Security", "Bugfix"]},
                    ]
                },
                "ApproveAfterDays": 0,
                "ComplianceLevel": "CRITICAL",
                "EnableNonSecurity": False,
            }]
        },
        "ApprovedPatches": [],
        "ApprovedPatchesComplianceLevel": "CRITICAL",
        "ApprovedPatchesEnableNonSecurity": False,
        "RejectedPatches": [],
        "RejectedPatchesAction": "BLOCK",
        "GlobalFilters": {"PatchFilters": []},
        "Sources": [],
    }]


def main():
    sts = boto3.client('sts', region_name=AWS_REGION)
    account_id = sts.get_caller_identity()['Account']
    bucket = f'patch-compliance-reports-{account_id}'

    s3 = boto3.client('s3', region_name=AWS_REGION)

    # Verify bucket exists
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception as e:
        logger.error(f"Bucket {bucket} not found. Deploy Patchy-Core first: ./deploy.sh")
        logger.error(f"Error: {e}")
        return

    for name, config in OVERRIDES.items():
        override = build_override_json(config["severities"])
        key = f"baseline-overrides/{name}.json"
        body = json.dumps(override, indent=2)

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType='application/json',
            Metadata={'description': config['description']},
        )
        logger.info(f"Uploaded s3://{bucket}/{key} ({config['description']})")

    logger.info(f"Done. {len(OVERRIDES)} baseline overrides uploaded to s3://{bucket}/baseline-overrides/")
    logger.info("These are used by patch_dry_run and execute_patch_operation when severity_filter is set.")


if __name__ == '__main__':
    main()
