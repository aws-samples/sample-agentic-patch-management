"""Shared tools for intelligent patch automation agents"""

import logging
from strands import tool
from strands_tools.use_aws import use_aws
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

def handle_aws_error(error: Exception, operation: str, context: dict) -> dict:
    """Handle AWS service errors with appropriate fallbacks"""
    if isinstance(error, ClientError):
        error_code = error.response['Error']['Code']
        logger.warning(f"AWS {operation} failed with {error_code}: {str(error)}")
        
        if error_code == 'AccessDenied':
            return {
                "status": "error",
                "error_type": "access_denied",
                "message": f"Access denied for {operation}. Check IAM permissions.",
                "fallback_available": True,
                "recommendation": "Use read-only operations or request additional permissions"
            }
        elif error_code == 'ThrottlingException':
            return {
                "status": "error", 
                "error_type": "throttling",
                "message": f"API throttling for {operation}. Retry recommended.",
                "fallback_available": True,
                "recommendation": "Implement exponential backoff and retry"
            }
        elif error_code == 'ResourceNotFound':
            return {
                "status": "error",
                "error_type": "resource_not_found", 
                "message": f"Resource not found for {operation}",
                "fallback_available": False,
                "recommendation": "Verify resource exists and region is correct"
            }
        elif error_code == 'InspectorNotEnabled':
            return {
                "status": "error",
                "error_type": "service_not_enabled",
                "message": "AWS Inspector is not enabled in this account/region",
                "fallback_available": True,
                "recommendation": "Enable Inspector or use external vulnerability databases"
            }
    
    logger.error(f"Unexpected error in {operation}: {str(error)}")
    return {
        "status": "error",
        "error_type": "unexpected",
        "message": str(error),
        "fallback_available": False,
        "recommendation": "Review operation parameters and try alternative approach"
    }

@tool
def aws_query(service: str, operation: str, parameters: dict = None) -> dict:
    """Flexible AWS service interaction with comprehensive error handling."""
    logger.info(f"Executing AWS {service} {operation}")
    try:
        result = use_aws(
            tool=None,  # Add required tool parameter
            service_name=service,
            operation_name=operation,
            parameters=parameters or {},
            region="us-east-1",
            label=f"Query {service} {operation}"
        )
        logger.info(f"AWS {service} {operation} completed successfully")
        return {"status": "success", "data": result}
    except Exception as e:
        error_response = handle_aws_error(e, f"{service} {operation}", parameters or {})
        logger.error(f"AWS {service} {operation} failed: {error_response['message']}")
        return error_response
