"""Who is calling the runtime, when it accepts Cognito tokens (RUNTIME_AUTH=jwt).

With an inbound JWT authorizer, AgentCore validates the bearer token (issuer,
signature, expiry, allowed client) before the handler runs and passes the
Authorization header through. The claims are read here without checking the
signature again: a request with a bad token never reaches the handler.
"""

import base64
import contextvars
import json
import logging
import os
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

OPERATORS_GROUP = "operators"

# The current request's context, so tools can check the caller too.
_request: contextvars.ContextVar[Any] = contextvars.ContextVar("_caller_request", default=None)


def set_request(context) -> None:
    _request.set(context)


def jwt_mode() -> bool:
    return os.environ.get("RUNTIME_AUTH", "").strip().lower() == "jwt"


def _claims(context) -> Optional[dict]:
    if context is None:
        context = _request.get()
    headers = getattr(context, "request_headers", None) or {}
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not auth.lower().startswith("bearer "):
        return None
    parts = auth[7:].strip().split(".")
    if len(parts) != 3:
        return None
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def groups(context=None) -> Optional[List[str]]:
    """The caller's Cognito groups, or None when the request carries no token."""
    claims = _claims(context)
    if claims is None:
        return None
    value = claims.get("cognito:groups") or []
    return [g for g in value if isinstance(g, str)] if isinstance(value, list) else []


def username(context=None) -> Optional[str]:
    claims = _claims(context) or {}
    return claims.get("username") or claims.get("sub")


# Cognito access tokens carry the username (a UUID when users sign in by email), not the
# email. Records show people by email, so it's read from the token's own user pool.
_ISSUER = re.compile(r"^https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9-]+_[A-Za-z0-9]+)$")
_emails: dict = {}


def display_name(context=None) -> Optional[str]:
    """The caller's email from their user pool, else their username; None without a token."""
    claims = _claims(context) or {}
    user = claims.get("username") or claims.get("sub")
    if not user:
        return None
    if user in _emails:
        return _emails[user]
    match = _ISSUER.match(str(claims.get("iss") or ""))
    if not match:
        return user
    try:
        import boto3
        resp = boto3.client("cognito-idp", region_name=match.group(1)).admin_get_user(
            UserPoolId=match.group(2), Username=user)
        email = next((a.get("Value") for a in resp.get("UserAttributes") or [] if a.get("Name") == "email"), None)
    except Exception as e:  # the username still identifies them
        logger.warning(f"[AUTH] couldn't read the email for {user}: {e}")
        return user
    _emails[user] = email or user
    return _emails[user]


def may_change(context=None) -> bool:
    """Whether the caller may run or stop changes: operators only in JWT mode.

    In IAM mode the UI checked the group. A bearer token there means the
    runtime has an authorizer that RUNTIME_AUTH doesn't know about, so it
    refuses rather than guess.
    """
    if not jwt_mode():
        return _claims(context) is None
    return OPERATORS_GROUP in (groups(context) or [])


def may_chat(context=None) -> bool:
    """Whether the caller may hold a chat turn: in JWT mode operators only, as the UI API allows.

    Chat reaches tools that start scans and store plans, so a viewer's token sent
    straight to the runtime gets no further than it would through the UI.
    """
    return may_change(context) if jwt_mode() else True


def claim_session(session_id: str, user: str) -> bool:
    """JWT mode: bind a conversation to the first user who uses it; False for anyone else.

    Session IDs are chosen by the client, so without this another user who
    learned one could read that conversation's memory or act as its owner.
    """
    from helper import plans
    key = f"session-owners/{session_id}.json"
    if plans.put_record(key, {"username": user}, create_only=True):
        return True
    owner = (plans.get_record(key) or {}).get("username")
    if owner != user:
        logger.warning(f"[AUTH] session {session_id} belongs to another user; refused {user}")
    return owner == user
