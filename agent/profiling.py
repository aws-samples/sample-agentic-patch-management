"""Centralized profiling utility for performance monitoring"""

import os
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

# Single source of truth for profiling flag
ENABLE_PROFILING = os.environ.get("ENABLE_PROFILING", "false").lower() == "true"


def profile(operation_name: str):
    """
    Decorator for profiling function execution time.
    
    Usage:
        @profile("Tool: get_instance_inventory")
        def get_instance_inventory(...):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not ENABLE_PROFILING:
                return func(*args, **kwargs)
            
            start_time = time.time()
            logger.info(f"[PROFILE] {operation_name}: START")
            
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time
                logger.info(f"[PROFILE] {operation_name}: COMPLETE in {elapsed:.2f}s")
                return result
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[PROFILE] {operation_name}: ERROR in {elapsed:.2f}s: {e}")
                raise
        
        return wrapper
    return decorator


def log_profile(message: str):
    """Simple profiling log (for non-decorated code)."""
    if ENABLE_PROFILING:
        logger.info(f"[PROFILE] {message}")
