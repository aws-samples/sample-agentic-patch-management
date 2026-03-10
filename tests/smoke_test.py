#!/usr/bin/env python3
"""Smoke tests — verify modules load, decorators work, config parses.

No AWS credentials required. Run with:
    python -m pytest tests/smoke_test.py -v
"""

import os
import sys
import json

os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))


def test_tools_module_imports():
    """Verify helper.tools can be imported without errors."""
    from helper import tools
    assert hasattr(tools, 'get_vulnerability_findings')
    assert hasattr(tools, 'get_patch_compliance')
    assert hasattr(tools, 'execute_patch_operation')
    assert hasattr(tools, 'patch_dry_run')


def test_error_handling_imports():
    """Verify helper.error_handling loads and has expected exports."""
    from helper.error_handling import classify_error, retry_on_transient, RETRYABLE_ERRORS
    assert callable(classify_error)
    assert callable(retry_on_transient)
    assert 'ThrottlingException' in RETRYABLE_ERRORS


def test_tool_decorator_applied():
    """Verify @tool decorator is applied (functions have __wrapped__)."""
    from helper.tools import get_vulnerability_findings, patch_dry_run
    # Strands @tool decorator wraps functions; __wrapped__ gives the original
    assert hasattr(get_vulnerability_findings, '__wrapped__')
    assert hasattr(patch_dry_run, '__wrapped__')


def test_config_json_parses():
    """Verify config/config.json is valid JSON with expected structure."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'agent', 'config', 'config.json')
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)

    assert 'frameworks' in config
    assert 'default' in config
    for framework in ['PCI-DSS', 'SOC2', 'HIPAA']:
        assert framework in config['frameworks']
        for sev in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']:
            assert sev in config['frameworks'][framework]

    for sev in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']:
        assert sev in config['default']
        assert isinstance(config['default'][sev], int)


def test_response_templates_parse():
    """Verify config/response_templates.yaml is valid YAML."""
    import yaml
    templates_path = os.path.join(os.path.dirname(__file__), '..', 'agent', 'config', 'response_templates.yaml')
    with open(templates_path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)


def test_instance_id_validation_pattern():
    """Verify INSTANCE_ID_PATTERN regex works correctly."""
    from helper.tools import INSTANCE_ID_PATTERN
    assert INSTANCE_ID_PATTERN.match('i-0abcdef1234567890')
    assert INSTANCE_ID_PATTERN.match('i-abcd1234')
    assert not INSTANCE_ID_PATTERN.match('bad-id')
    assert not INSTANCE_ID_PATTERN.match('')
    assert not INSTANCE_ID_PATTERN.match('i-ZZZZ')


def test_environment_normalization():
    """Verify environment alias resolution."""
    from helper.tools import _normalize_environment
    assert _normalize_environment('production') == 'prod'
    assert _normalize_environment('development') == 'dev'
    assert _normalize_environment('uat') == 'staging'
    assert _normalize_environment('qa') == 'test'


def test_severity_override_map():
    """Verify severity-to-S3-key mapping is complete."""
    from helper.tools import _SEVERITY_OVERRIDE_MAP
    assert 'CRITICAL' in _SEVERITY_OVERRIDE_MAP
    assert 'HIGH' in _SEVERITY_OVERRIDE_MAP
    assert 'IMPORTANT' in _SEVERITY_OVERRIDE_MAP
    assert 'MEDIUM' in _SEVERITY_OVERRIDE_MAP
    assert 'LOW' in _SEVERITY_OVERRIDE_MAP
