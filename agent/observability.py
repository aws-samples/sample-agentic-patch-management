"""Centralized observability for patch automation agents."""

import logging
import time
import json
from functools import wraps
from typing import Any, Callable, Dict, Optional
from datetime import datetime

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Track performance metrics across the application."""
    
    def __init__(self):
        self.metrics = {
            'cache_hits': 0,
            'cache_misses': 0,
            'aws_api_calls': 0,
            'agent_invocations': 0,
            'tool_calls': {},
            'query_times': []
        }
    
    def record_cache_hit(self, key: str):
        """Record a cache hit."""
        self.metrics['cache_hits'] += 1
        logger.info("Cache HIT", extra={
            'metric_type': 'cache',
            'result': 'hit',
            'key': key[:50]  # Truncate long keys
        })
    
    def record_cache_miss(self, key: str):
        """Record a cache miss."""
        self.metrics['cache_misses'] += 1
        logger.info("Cache MISS", extra={
            'metric_type': 'cache',
            'result': 'miss',
            'key': key[:50]
        })
    
    def record_aws_call(self, service: str, operation: str, duration_ms: float):
        """Record an AWS API call."""
        self.metrics['aws_api_calls'] += 1
        logger.info("AWS API call", extra={
            'metric_type': 'aws_api',
            'service': service,
            'operation': operation,
            'duration_ms': round(duration_ms, 2)
        })
    
    def record_tool_call(self, tool_name: str, duration_ms: float, success: bool = True):
        """Record a tool invocation."""
        if tool_name not in self.metrics['tool_calls']:
            self.metrics['tool_calls'][tool_name] = {'count': 0, 'total_ms': 0, 'failures': 0}
        
        self.metrics['tool_calls'][tool_name]['count'] += 1
        self.metrics['tool_calls'][tool_name]['total_ms'] += duration_ms
        if not success:
            self.metrics['tool_calls'][tool_name]['failures'] += 1
        
        logger.info(f"Tool: {tool_name}", extra={
            'metric_type': 'tool',
            'tool_name': tool_name,
            'duration_ms': round(duration_ms, 2),
            'success': success
        })
    
    def record_agent_invocation(self, agent_name: str, duration_ms: float):
        """Record an agent invocation."""
        self.metrics['agent_invocations'] += 1
        logger.info(f"Agent: {agent_name}", extra={
            'metric_type': 'agent',
            'agent_name': agent_name,
            'duration_ms': round(duration_ms, 2)
        })
    
    def record_query_time(self, query: str, duration_ms: float):
        """Record overall query time."""
        self.metrics['query_times'].append({
            'query': query[:100],  # Truncate long queries
            'duration_ms': duration_ms,
            'timestamp': datetime.utcnow().isoformat()
        })
        
        logger.info("Query completed", extra={
            'metric_type': 'query',
            'query': query[:100],
            'duration_ms': round(duration_ms, 2)
        })
    
    def get_cache_hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.metrics['cache_hits'] + self.metrics['cache_misses']
        if total == 0:
            return 0.0
        return (self.metrics['cache_hits'] / total) * 100
    
    def get_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        return {
            'cache_hit_rate': round(self.get_cache_hit_rate(), 1),
            'total_cache_operations': self.metrics['cache_hits'] + self.metrics['cache_misses'],
            'aws_api_calls': self.metrics['aws_api_calls'],
            'agent_invocations': self.metrics['agent_invocations'],
            'tool_usage': {
                name: {
                    'count': stats['count'],
                    'avg_ms': round(stats['total_ms'] / stats['count'], 2),
                    'failure_rate': round((stats['failures'] / stats['count']) * 100, 1)
                }
                for name, stats in self.metrics['tool_calls'].items()
            },
            'query_count': len(self.metrics['query_times'])
        }
    
    def log_summary(self):
        """Log performance summary."""
        summary = self.get_summary()
        logger.info("Performance Summary", extra={
            'metric_type': 'summary',
            **summary
        })
        return summary


# Global tracker instance
_tracker = PerformanceTracker()


def get_tracker() -> PerformanceTracker:
    """Get the global performance tracker."""
    return _tracker


def track_performance(metric_type: str = 'function'):
    """
    Decorator to track function performance.
    
    Args:
        metric_type: Type of metric ('tool', 'agent', 'aws', 'function')
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start = time.time()
            success = True
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                raise
            finally:
                duration_ms = (time.time() - start) * 1000
                
                # Record based on metric type
                if metric_type == 'tool':
                    _tracker.record_tool_call(func.__name__, duration_ms, success)
                elif metric_type == 'agent':
                    _tracker.record_agent_invocation(func.__name__, duration_ms)
                else:
                    logger.info(f"Function: {func.__name__}", extra={
                        'metric_type': metric_type,
                        'function': func.__name__,
                        'duration_ms': round(duration_ms, 2),
                        'success': success
                    })
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.time()
            success = True
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                raise
            finally:
                duration_ms = (time.time() - start) * 1000
                
                if metric_type == 'tool':
                    _tracker.record_tool_call(func.__name__, duration_ms, success)
                elif metric_type == 'agent':
                    _tracker.record_agent_invocation(func.__name__, duration_ms)
                else:
                    logger.info(f"Function: {func.__name__}", extra={
                        'metric_type': metric_type,
                        'function': func.__name__,
                        'duration_ms': round(duration_ms, 2),
                        'success': success
                    })
        
        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator


def log_event(event_type: str, message: str, **kwargs):
    """
    Log a structured event.
    
    Args:
        event_type: Type of event (e.g., 'routing', 'error', 'decision')
        message: Human-readable message
        **kwargs: Additional structured data
    """
    logger.info(message, extra={
        'event_type': event_type,
        **kwargs
    })


def log_error(error: Exception, context: Dict[str, Any] = None):
    """
    Log an error with context.
    
    Args:
        error: The exception that occurred
        context: Additional context about the error
    """
    logger.error(f"Error: {str(error)}", extra={
        'event_type': 'error',
        'error_type': type(error).__name__,
        'error_message': str(error),
        'context': context or {}
    }, exc_info=True)
