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

# Severity-scoped baseline overrides. AWS-RunPatchBaseline picks the entry
# whose OperatingSystem matches the instance and fails the run if none does,
# so each file carries one entry per OS family that supports severity filters.
# Ubuntu and Debian filter on PRIORITY, not severity, so they have no entry and
# a severity-filtered run against them fails rather than over-installing.
OVERRIDES = {
    "critical-only": {
        "description": "Critical severity patches only",
        "levels": ["Critical"],
    },
    "high-and-above": {
        "description": "Critical + Important (High) severity patches",
        "levels": ["Critical", "Important"],
    },
    "medium-and-above": {
        "description": "Critical + Important + Medium severity patches",
        "levels": ["Critical", "Important", "Medium"],
    },
    "all-severities": {
        "description": "All severity levels",
        "levels": ["Critical", "Important", "Medium", "Low"],
    },
}

# Per-OS filter vocabulary, from ssm:DescribePatchProperties. "Medium" is
# called "Moderate" outside Amazon Linux.
OS_FILTERS = {
    "AMAZON_LINUX_2":          ("SEVERITY", "Medium", ["Security", "Bugfix"]),
    "AMAZON_LINUX_2023":       ("SEVERITY", "Medium", ["Security", "Bugfix"]),
    "REDHAT_ENTERPRISE_LINUX": ("SEVERITY", "Moderate", ["Security", "Bugfix"]),
    "CENTOS":                  ("SEVERITY", "Moderate", ["Security", "Bugfix"]),
    "ROCKY_LINUX":             ("SEVERITY", "Moderate", ["Security", "Bugfix"]),
    "ALMA_LINUX":              ("SEVERITY", "Moderate", ["Security", "Bugfix"]),
    "ORACLE_LINUX":            ("SEVERITY", "Moderate", ["Security", "Bugfix"]),
    "SUSE":                    ("SEVERITY", "Moderate", ["Security", "Recommended"]),
    "WINDOWS":                 ("MSRC_SEVERITY", "Moderate", ["SecurityUpdates", "CriticalUpdates"]),
}


def build_override_json(levels: list) -> list:
    """Build a BaselineOverride list with one baseline per supported OS family.

    Args:
        levels: Severity levels using Amazon Linux names (Critical, Important, Medium, Low).

    Returns:
        List of baseline objects, as AWS-RunPatchBaseline expects.
    """
    baselines = []
    for os_name, (key, medium_name, classes) in OS_FILTERS.items():
        values = [medium_name if lvl == "Medium" else lvl for lvl in levels]
        baselines.append({
            "OperatingSystem": os_name,
            "ApprovalRules": {
                "PatchRules": [{
                    "PatchFilterGroup": {
                        "PatchFilters": [
                            {"Key": key, "Values": values},
                            {"Key": "CLASSIFICATION", "Values": classes},
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
        })
    return baselines


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
        override = build_override_json(config["levels"])
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
