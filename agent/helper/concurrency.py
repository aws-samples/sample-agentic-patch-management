"""A thread pool whose tasks run in a copy of the submitting thread's context.

The agent keeps the operator, session and request caches in contextvars.
Plain ThreadPoolExecutor workers don't inherit them, so fan-outs across
accounts assumed spoke roles as "unknown" (spoke CloudTrail lost the operator)
and the client cache rebuilt clients between the tool thread and its workers.
"""
import contextvars
from concurrent.futures import ThreadPoolExecutor


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that runs every task in the caller's context (copied at submit)."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)
