"""
Error Handling - Structured error responses for intelligent agent recovery

Instead of generic {"error": str(e)} responses, tools return structured errors
that tell the agent exactly what happened and what it can do about it.

Design Philosophy:
- Tools classify their own errors (they know best what went wrong)
- Agent decides recovery strategy (it has the workflow context)
- Decision logger captures error recovery decisions (audit trail)
- No separate "error recovery framework" - that's over-engineering

Error Categories:
- RETRYABLE: Transient failures (throttling, timeouts) - agent should retry
- SKIP: Resource-specific failures (instance not found) - skip and continue
- ABORT: Critical failures (permissions, service down) - stop workflow
- DEGRADED: Partial success (3 of 5 instances patched) - agent decides
"""

import logging
import time
import functools
from typing import Optional, Dict, Any, Callable
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

logger = logging.getLogger(__name__)


# ============================================================================
# ERROR CLASSIFICATION
# ============================================================================

# AWS error codes → recovery category
RETRYABLE_ERRORS = {
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "Throttling",
    "ProvisionedThroughputExceededException",
    "ServiceUnavailable",
    "InternalServerError",
    "InternalError",
    "RequestTimeout",
    "RequestTimeoutException",
    "IDPCommunicationError",
}

SKIP_ERRORS = {
    "InvalidInstanceId",
    "InvalidInstanceId.NotFound",
    "InstanceNotFound",
    "InvalidTarget",
    "ResourceNotFoundException",
    "InvalidResourceId",
}

ABORT_ERRORS = {
    "AccessDeniedException",
    "UnauthorizedAccess",
    "AuthFailure",
    "InvalidParameterValue",
    "ValidationException",
    "InvalidDocument",
    "InvalidPermissionType",
    "UnsupportedOperatingSystem",
}


def classify_error(error: Exception) -> Dict[str, Any]:
    """
    Classify an exception into a structured error response.
    
    Returns a dict the agent can reason about:
    {
        "error": "Human-readable message",
        "error_code": "AWS error code or exception type",
        "category": "RETRYABLE | SKIP | ABORT | UNKNOWN",
        "suggestion": "What the agent should do",
        "retryable": bool,
        "details": {}  # Additional context
    }
    """
    if isinstance(error, ClientError):
        error_code = error.response["Error"]["Code"]
        error_message = error.response["Error"]["Message"]
        
        if error_code in RETRYABLE_ERRORS:
            return {
                "error": error_message,
                "error_code": error_code,
                "category": "RETRYABLE",
                "suggestion": "Transient AWS error. Retry the operation.",
                "retryable": True,
            }
        
        if error_code in SKIP_ERRORS:
            return {
                "error": error_message,
                "error_code": error_code,
                "category": "SKIP",
                "suggestion": "Resource not found or invalid. Skip this resource and continue with others.",
                "retryable": False,
            }
        
        if error_code in ABORT_ERRORS:
            return {
                "error": error_message,
                "error_code": error_code,
                "category": "ABORT",
                "suggestion": "Permission or configuration error. Stop and report to user.",
                "retryable": False,
            }
        
        # Unknown AWS error
        return {
            "error": error_message,
            "error_code": error_code,
            "category": "UNKNOWN",
            "suggestion": "Unexpected AWS error. Report to user with error details.",
            "retryable": False,
        }
    
    if isinstance(error, EndpointConnectionError):
        return {
            "error": str(error),
            "error_code": "EndpointConnectionError",
            "category": "RETRYABLE",
            "suggestion": "Network connectivity issue. Retry the operation.",
            "retryable": True,
        }
    
    if isinstance(error, ReadTimeoutError):
        return {
            "error": str(error),
            "error_code": "ReadTimeoutError",
            "category": "RETRYABLE",
            "suggestion": "Request timed out. Retry with a longer timeout or smaller batch.",
            "retryable": True,
        }
    
    # Non-AWS errors
    return {
        "error": str(error),
        "error_code": type(error).__name__,
        "category": "UNKNOWN",
        "suggestion": "Unexpected error. Report to user with error details.",
        "retryable": False,
    }


def retry_on_transient(max_retries: int = 2, base_delay: float = 1.0):
    """Decorator that retries a function on transient AWS errors.

    Only retries when classify_error() returns category=RETRYABLE.
    Uses exponential backoff with jitter. Intended for @tool functions
    that make AWS API calls — boto3's built-in retry handles most
    throttling, but this catches cases where the built-in retry is exhausted.

    Usage:
        @tool
        @retry_on_transient(max_retries=2)
        def my_tool(...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ClientError, EndpointConnectionError, ReadTimeoutError) as e:
                    last_error = e
                    error_info = classify_error(e)
                    if error_info["category"] != "RETRYABLE" or attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** attempt) + (time.monotonic() % 0.5)
                    logger.warning(f"[RETRY] {func.__name__} attempt {attempt + 1}/{max_retries} "
                                   f"after {error_info['error_code']}, waiting {delay:.1f}s")
                    time.sleep(delay)
            raise last_error  # unreachable but satisfies type checker
        return wrapper
    return decorator
