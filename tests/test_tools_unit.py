#!/usr/bin/env python3
"""Unit tests for agent tool helper functions — no live AWS required.

Uses unittest.mock to patch boto3 clients. Run with:
    python -m pytest tests/test_tools_unit.py -v
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

# Set up environment before importing agent code
os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))


# ---------------------------------------------------------------------------
# _validate_instance_ids
# ---------------------------------------------------------------------------

class TestValidateInstanceIds:
    def setup_method(self):
        from helper.tools import _validate_instance_ids
        self.validate = _validate_instance_ids

    def test_valid_ids(self):
        assert self.validate(['i-0abcdef1234567890']) is None

    def test_valid_short_id(self):
        assert self.validate(['i-abcd1234']) is None

    def test_empty_list(self):
        result = self.validate([])
        assert result is not None
        assert result['category'] == 'ABORT'
        assert 'empty' in result['error']

    def test_invalid_format_no_prefix(self):
        result = self.validate(['abcdef1234567890'])
        assert result is not None
        assert result['error_code'] == 'InvalidInstanceId'

    def test_invalid_format_wrong_chars(self):
        result = self.validate(['i-ZZZZ1234'])
        assert result is not None
        assert result['error_code'] == 'InvalidInstanceId'

    def test_mixed_valid_invalid(self):
        result = self.validate(['i-abcd1234', 'bad-id'])
        assert result is not None
        assert 'bad-id' in result['error']

    def test_multiple_valid(self):
        ids = [f'i-{i:017x}' for i in range(5)]
        assert self.validate(ids) is None


# ---------------------------------------------------------------------------
# _calculate_sla_requirement
# ---------------------------------------------------------------------------

class TestCalculateSlaRequirement:
    def setup_method(self):
        from helper.tools import _calculate_sla_requirement
        self.calc = _calculate_sla_requirement

    def test_from_instance_tags(self):
        tags = {'SLA-CRITICAL': '6', 'SLA-HIGH': '24'}
        result = self.calc(['PCI-DSS'], 'CRITICAL', instance_tags=tags)
        assert result is not None
        assert result['sla_hours'] == 6
        assert result['source'] == 'tag:SLA-CRITICAL'

    def test_fallback_to_default(self):
        result = self.calc(['SOC2'], 'HIGH')
        assert result is not None
        assert result['source'] == 'default'
        assert result['sla_hours'] == 72  # default from _DEFAULT_SLA

    def test_invalid_tag_value_falls_to_default(self):
        tags = {'SLA-CRITICAL': 'not-a-number'}
        result = self.calc(['SOC2'], 'CRITICAL', instance_tags=tags)
        assert result is not None
        assert result['source'] == 'default'

    def test_case_insensitive_severity(self):
        result = self.calc([], 'critical')
        assert result is not None
        assert result['severity'] == 'CRITICAL'

    def test_unknown_severity_uses_24h_default(self):
        result = self.calc([], 'UNKNOWN_SEV')
        assert result is not None
        assert result['sla_hours'] == 24  # fallback


# ---------------------------------------------------------------------------
# _normalize_environment
# ---------------------------------------------------------------------------

class TestNormalizeEnvironment:
    def setup_method(self):
        from helper.tools import _normalize_environment
        self.normalize = _normalize_environment

    def test_canonical_names(self):
        assert self.normalize('dev') == 'dev'
        assert self.normalize('staging') == 'staging'
        assert self.normalize('prod') == 'prod'

    def test_aliases(self):
        assert self.normalize('production') == 'prod'
        assert self.normalize('development') == 'dev'
        assert self.normalize('uat') == 'staging'

    def test_case_insensitive(self):
        assert self.normalize('PROD') == 'prod'
        assert self.normalize('Production') == 'prod'

    def test_unknown_returns_as_is(self):
        assert self.normalize('custom-env') == 'custom-env'


# ---------------------------------------------------------------------------
# _format_utc_as_local
# ---------------------------------------------------------------------------

class TestFormatUtcAsLocal:
    def setup_method(self):
        from helper.tools import _format_utc_as_local, set_timezone
        self.format = _format_utc_as_local
        self.set_tz = set_timezone

    def test_utc_format(self):
        self.set_tz('UTC')
        result = self.format('2025-01-15T10:30:00+00:00')
        assert '15 Jan 2025' in result

    def test_invalid_input_returns_as_is(self):
        self.set_tz('UTC')
        result = self.format('not-a-date')
        assert result == 'not-a-date'

    def test_bad_timezone_returns_string(self):
        self.set_tz('Invalid/Timezone')
        result = self.format('2025-01-15T10:30:00+00:00')
        # Should fall back gracefully
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _DEFAULT_SLA (module-level dict with fallback SLA hours)
# ---------------------------------------------------------------------------

class TestDefaultSla:
    def test_has_all_severities(self):
        from helper.tools import _DEFAULT_SLA
        assert isinstance(_DEFAULT_SLA, dict)
        assert 'CRITICAL' in _DEFAULT_SLA
        assert 'HIGH' in _DEFAULT_SLA
        assert 'MEDIUM' in _DEFAULT_SLA
        assert 'LOW' in _DEFAULT_SLA

    def test_default_values(self):
        from helper.tools import _DEFAULT_SLA
        assert _DEFAULT_SLA['CRITICAL'] == 24
        assert _DEFAULT_SLA['HIGH'] == 72
        assert _DEFAULT_SLA['MEDIUM'] == 168
        assert _DEFAULT_SLA['LOW'] == 720


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------

class TestClassifyError:
    def setup_method(self):
        from helper.error_handling import classify_error
        self.classify = classify_error

    def test_throttling_is_retryable(self):
        err = ClientError(
            {'Error': {'Code': 'ThrottlingException', 'Message': 'Rate exceeded'}},
            'DescribeInstances'
        )
        result = self.classify(err)
        assert result['category'] == 'RETRYABLE'
        assert result['retryable'] is True

    def test_access_denied_is_abort(self):
        err = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'Not authorized'}},
            'SendCommand'
        )
        result = self.classify(err)
        assert result['category'] == 'ABORT'
        assert result['retryable'] is False

    def test_not_found_is_skip(self):
        err = ClientError(
            {'Error': {'Code': 'InvalidInstanceId', 'Message': 'Not found'}},
            'DescribeInstances'
        )
        result = self.classify(err)
        assert result['category'] == 'SKIP'

    def test_unknown_aws_error(self):
        err = ClientError(
            {'Error': {'Code': 'SomeNewError', 'Message': 'Unexpected'}},
            'SomeOp'
        )
        result = self.classify(err)
        assert result['category'] == 'UNKNOWN'

    def test_non_aws_error(self):
        err = ValueError("bad value")
        result = self.classify(err)
        assert result['category'] == 'UNKNOWN'
        assert result['error_code'] == 'ValueError'


# ---------------------------------------------------------------------------
# classify_error — additional edge cases
# ---------------------------------------------------------------------------

class TestClassifyErrorEdgeCases:
    def setup_method(self):
        from helper.error_handling import classify_error
        self.classify = classify_error

    def test_endpoint_connection_error(self):
        from botocore.exceptions import EndpointConnectionError
        err = EndpointConnectionError(endpoint_url='https://ssm.us-east-1.amazonaws.com')
        result = self.classify(err)
        assert result['category'] == 'RETRYABLE'
        assert result['retryable'] is True

    def test_read_timeout_is_retryable(self):
        from botocore.exceptions import ReadTimeoutError
        err = ReadTimeoutError(endpoint_url='https://ssm.us-east-1.amazonaws.com')
        result = self.classify(err)
        assert result['category'] == 'RETRYABLE'
        assert result['retryable'] is True
