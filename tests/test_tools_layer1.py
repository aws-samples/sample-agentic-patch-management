#!/usr/bin/env python3
"""Layer 1: Direct tool tests — no LLM, just verify tool functions work correctly.

Usage:
    AWS_PROFILE=patchy python tests/test_tools_layer1.py [test_name]
    
    test_name: maintenance_windows | patch_policy | baseline_override | all
"""

import os
import sys
import json
import time

# Set up environment
os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

# Add agent dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: {detail}")


def test_maintenance_windows():
    """Test get_maintenance_windows returns patch-filtered windows with hours_until."""
    print("\n=== TEST: get_maintenance_windows ===")
    from helper.tools import get_maintenance_windows
    
    start = time.time()
    result = get_maintenance_windows.__wrapped__("dev")
    elapsed = time.time() - start
    print(f"  Time: {elapsed:.1f}s")
    
    check("returns dict", isinstance(result, dict))
    check("has 'windows' key", 'windows' in result)
    check("has 'count' key", 'count' in result)
    check("no error", 'error' not in result, result.get('error', ''))
    
    windows = result.get('windows', [])
    print(f"  Found {len(windows)} windows for dev")
    
    for w in windows:
        name = w.get('name', '?')
        check(f"window '{name}' has window_id", 'window_id' in w)
        check(f"window '{name}' has next_execution", w.get('next_execution') is not None,
              f"next_execution={w.get('next_execution')}")
        check(f"window '{name}' has hours_until_window", w.get('hours_until_window') is not None,
              f"hours_until_window={w.get('hours_until_window')}")
        check(f"window '{name}' has has_patch_task", 'has_patch_task' in w)
        check(f"window '{name}' environment is dev", w.get('environment') == 'dev',
              f"environment={w.get('environment')}")
        print(f"  Window: {name} | next={w.get('next_execution')} | hours={w.get('hours_until_window')} | patch_task={w.get('has_patch_task')}")


def test_patch_policy():
    """Test get_patch_policy returns per-instance association info."""
    print("\n=== TEST: get_patch_policy ===")
    import boto3
    ec2 = boto3.client('ec2', region_name='us-east-1')
    
    # Get 2 dev instance IDs
    resp = ec2.describe_instances(
        Filters=[{'Name': 'tag:Environment', 'Values': ['dev']}],
        MaxResults=5
    )
    dev_ids = [i['InstanceId'] for r in resp['Reservations'] for i in r['Instances']][:2]
    print(f"  Testing with instances: {dev_ids}")
    
    if not dev_ids:
        print("  ⚠️ No dev instances found, skipping")
        return
    
    from helper.tools import get_patch_policy
    
    start = time.time()
    result = get_patch_policy.__wrapped__(dev_ids)
    elapsed = time.time() - start
    print(f"  Time: {elapsed:.1f}s")
    
    check("returns dict", isinstance(result, dict))
    check("no error", 'error' not in result, result.get('error', ''))
    check("has instance_policies", 'instance_policies' in result)
    check("has summary", 'summary' in result)
    check("has instances_without_policy", 'instances_without_policy' in result)
    check("has instances_with_install_policy", 'instances_with_install_policy' in result)
    check("has instances_with_scan_only", 'instances_with_scan_only' in result)
    
    policies = result.get('instance_policies', {})
    for iid, pols in policies.items():
        print(f"  {iid}: {len(pols)} associations")
        for p in pols:
            print(f"    - {p.get('name')} | op={p.get('operation')} | schedule={p.get('schedule')}")
    
    print(f"  Summary: {result.get('summary')}")


def test_baseline_override():
    """Test _get_baseline_override_url returns correct S3 URLs."""
    print("\n=== TEST: _get_baseline_override_url ===")
    from helper.tools import _get_baseline_override_url
    
    # Test each severity
    for sev, expected_key in [
        ("CRITICAL", "critical-only"),
        ("HIGH", "high-and-above"),
        ("IMPORTANT", "high-and-above"),
        ("MEDIUM", "medium-and-above"),
        ("LOW", "all-severities"),
    ]:
        url = _get_baseline_override_url(sev)
        check(f"{sev} returns URL", url is not None, f"got None")
        if url:
            check(f"{sev} contains '{expected_key}'", expected_key in url, f"url={url}")
    
    # Test None returns None
    url = _get_baseline_override_url(None)
    check("None severity returns None", url is None, f"got {url}")
    
    # Test invalid returns None
    url = _get_baseline_override_url("BOGUS")
    check("invalid severity returns None", url is None, f"got {url}")
    
    # Verify S3 files exist
    print("\n  Checking S3 files exist...")
    import boto3
    s3 = boto3.client('s3', region_name='us-east-1')
    sts = boto3.client('sts', region_name='us-east-1')
    account = sts.get_caller_identity()['Account']
    bucket = f'patch-compliance-reports-{account}'
    
    for name in ['critical-only', 'high-and-above', 'medium-and-above', 'all-severities']:
        key = f'baseline-overrides/{name}.json'
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            body = json.loads(resp['Body'].read())
            check(f"S3 {name}.json exists", True)
            check(f"S3 {name}.json is array", isinstance(body, list), f"type={type(body)}")
        except Exception as e:
            check(f"S3 {name}.json exists", False, str(e))


if __name__ == '__main__':
    test_name = sys.argv[1] if len(sys.argv) > 1 else 'all'
    
    tests = {
        'maintenance_windows': test_maintenance_windows,
        'patch_policy': test_patch_policy,
        'baseline_override': test_baseline_override,
    }
    
    if test_name == 'all':
        for t in tests.values():
            t()
    elif test_name in tests:
        tests[test_name]()
    else:
        print(f"Unknown test: {test_name}. Options: {', '.join(tests.keys())}, all")
        sys.exit(1)
    
    print(f"\n{'='*50}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL > 0 else 0)
