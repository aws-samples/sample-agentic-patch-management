"""Retrying reads that AWS throttles.

The clients' own retries (3 adaptive attempts) give up quickly, and Patchy's fan-outs
(Inspector per CVE and Region, patch state per instance, EC2 per location) can outlast
them. A read that is still throttled after this raises, so the caller reports it as
unreadable instead of treating it as "nothing found".
"""

import time

THROTTLE_RETRIES = 4


def is_throttle(e: Exception) -> bool:
    code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
    return any(t in f"{code} {e}" for t in ("Throttl", "TooManyRequests", "Rate exceeded", "RequestLimitExceeded"))


def throttled(read, retries: int = THROTTLE_RETRIES, sleep=time.sleep):
    """read(), tried again with a growing pause (1, 2, 4 s) while AWS throttles it."""
    for attempt in range(retries):
        try:
            return read()
        except Exception as e:
            if attempt == retries - 1 or not is_throttle(e):
                raise
            sleep(2 ** attempt)
