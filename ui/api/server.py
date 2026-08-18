"""
Thin API proxy for Intelligent Patch Automation.
Wraps AgentCore invoke_agent_runtime as HTTP endpoints with SSE streaming.
"""

import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

# Auto-source .env — try project root first, then Docker path
_env_paths = [
    Path(__file__).parent.parent.parent / ".env",  # dev: project root
    Path("/app/.env"),                                # docker
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break

import boto3
import yaml
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# AgentCore Memory SDK — used to rehydrate chat conversations on page refresh.
# Optional import: if the SDK isn't installed (older deployments), the
# /api/session/{id}/messages endpoint returns 503 instead of crashing import.
try:
    from bedrock_agentcore.memory import MemoryClient  # type: ignore
    _MEMORY_SDK_AVAILABLE = True
except ImportError:
    MemoryClient = None  # type: ignore
    _MEMORY_SDK_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Patch Automation API")

# CORS origins — configurable via environment for production deployments.
# When STATIC_DIR is set (container), frontend is same-origin so CORS isn't needed.
# Default to localhost for local dev only.
_default_cors = "" if os.environ.get("STATIC_DIR") else "http://localhost:5173,http://localhost:3000"
_cors_origins = os.environ.get("CORS_ORIGINS", _default_cors).split(",")
_cors_origins = [o.strip() for o in _cors_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Accept", "X-API-Key", "X-Role"],
)


# ── Request validation ──────────────────────────────────────────────

_SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    session_id: str | None = Field(None, max_length=100)
    timezone: str | None = Field(None, max_length=50)


# ── Rate limiting (in-memory token bucket) ──────────────────────────

_rate_limits: dict[str, list[float]] = defaultdict(list)
MAX_REQUESTS_PER_MINUTE = 20


def _check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    # Prune stale entries to prevent unbounded growth from unique IPs
    if len(_rate_limits) > 100:
        stale = [ip for ip, timestamps in _rate_limits.items() if not timestamps or now - timestamps[-1] > 60]
        for ip in stale:
            del _rate_limits[ip]
    window = [t for t in _rate_limits[client_ip] if now - t < 60]
    _rate_limits[client_ip] = window
    if len(window) >= MAX_REQUESTS_PER_MINUTE:
        return False
    _rate_limits[client_ip].append(now)
    return True


# ── ALB JWT verification ──────────────────────────────────────────────
# ALB injects x-amzn-oidc-data signed with ES256. Public keys at:
#   https://public-keys.auth.elb.{region}.amazonaws.com/{kid}

_alb_key_cache: dict[str, tuple[str, float]] = {}
_ALB_KEY_CACHE_TTL = 86400  # 24 hours


def _verify_alb_jwt(token: str, region: str) -> dict:
    """Verify ALB-signed OIDC JWT (ES256) and return claims."""
    import jwt
    from urllib.request import urlopen

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("JWT missing kid in header")

    # The kid is interpolated into the public-key URL path. Restrict to the
    # character set ALB uses for key IDs (alphanumeric + hyphen) so a malicious
    # JWT cannot inject path traversal, query strings, or scheme tricks into
    # the URL we fetch.
    if not re.match(r"^[a-zA-Z0-9-]+$", kid):
        raise ValueError(f"JWT kid contains unexpected characters: {kid!r}")

    cached = _alb_key_cache.get(kid)
    if cached and (time.time() - cached[1]) < _ALB_KEY_CACHE_TTL:
        public_key = cached[0]
    else:
        # nosec B310 — kid validated above, hostname is AWS-controlled (HTTPS),
        # only AWS ALB public keys are returned from this endpoint.
        url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
        resp = urlopen(url, timeout=5)  # nosec B310
        public_key = resp.read().decode("utf-8")
        _alb_key_cache[kid] = (public_key, time.time())

    claims = jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        options={"verify_aud": False},
    )
    return claims


# ── Cognito access-token verification ─────────────────────────────────
# The ALB signs x-amzn-oidc-data, so identity is trustworthy. It does NOT sign
# x-amzn-oidc-accesstoken, and that is where cognito:groups lives — the claim
# that decides operator vs viewer. An unverified groups claim is worthless: a
# caller can grant themselves the operator role by editing one header. So the
# access token is verified against the user pool's JWKS.

_cognito_jwk_clients: dict = {}


def _cognito_jwk_client(region: str, user_pool_id: str):
    """Return a cached PyJWKClient for a user pool. Caches signing keys itself."""
    import jwt

    # user_pool_id is interpolated into the URL we fetch. It comes from our own
    # environment rather than the request, but validate the charset anyway to
    # match the defence on the ALB key path above.
    if not re.match(r"^[a-zA-Z0-9_-]+$", user_pool_id):
        raise ValueError(f"user pool id contains unexpected characters: {user_pool_id!r}")

    cache_key = f"{region}/{user_pool_id}"
    client = _cognito_jwk_clients.get(cache_key)
    if client is None:
        url = (
            f"https://cognito-idp.{region}.amazonaws.com/"
            f"{user_pool_id}/.well-known/jwks.json"
        )
        client = jwt.PyJWKClient(url, cache_keys=True)
        _cognito_jwk_clients[cache_key] = client
    return client


def _verify_cognito_access_token(
    token: str, region: str, user_pool_id: str, client_id: str
) -> dict:
    """Verify a Cognito access token (RS256) and return its claims.

    Raises on any failure. Callers must fail closed rather than falling back to
    an unverified read of the token.
    """
    import jwt

    signing_key = _cognito_jwk_client(region, user_pool_id).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}",
        # Access tokens carry client_id rather than aud, so PyJWT's audience
        # check does not apply and the comparison is made explicitly below.
        options={
            "verify_aud": False,
            "require": ["exp", "iss", "token_use", "client_id"],
        },
    )

    if claims.get("client_id") != client_id:
        raise ValueError("access token was issued to a different app client")

    # ID tokens also carry cognito:groups. They are issued for a different
    # purpose and must not be accepted in place of an access token.
    if claims.get("token_use") != "access":
        raise ValueError(f"expected token_use=access, got {claims.get('token_use')!r}")

    return claims


# ── Auth + RBAC middleware ────────────────────────────────────────────
# Roles: "operator" (full access) and "viewer" (read-only: dashboard + health).
#
# Configure via environment variables:
#   API_KEY=my-secret-key                     → single key, operator role (backward compatible)
#   API_KEY_OPERATOR=operator-secret-key      → explicit operator key
#   API_KEY_VIEWER=viewer-secret-key          → explicit viewer key
#
# When no API_KEY* vars are set, auth is disabled (open access, operator role).
# For production, use Cognito + JWT instead — see Production Hardening in README.

API_KEY = os.environ.get("API_KEY")
API_KEY_OPERATOR = os.environ.get("API_KEY_OPERATOR")
API_KEY_VIEWER = os.environ.get("API_KEY_VIEWER")

# Paths that viewers can access (GET only)
_VIEWER_ALLOWED = {"/api/health", "/api/dashboard"}

# Paths that require operator role (POST to chat = patch operations)
_OPERATOR_PATHS = {"/api/chat"}


def _resolve_role(request: Request) -> str | None:
    """Resolve the user's role from Cognito JWT, API key, or X-Role header.
    
    Priority:
    1. Cognito (ALB-injected x-amzn-oidc-data JWT) → extract cognito:groups
    2. API key (X-API-Key header) → map to role
    3. Pilot mode (no auth) → X-Role header from frontend
    """
    # 1. Cognito JWT from ALB (highest priority)
    # The ALB injects two relevant headers:
    #   x-amzn-oidc-data        → UserInfo claims (email, sub, etc.) — NO groups
    #   x-amzn-oidc-accesstoken → Access token (cognito:groups lives here)
    oidc_data = request.headers.get("x-amzn-oidc-data")
    if oidc_data:
        try:
            # Verify ES256 signature on the ALB-signed OIDC data token
            region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            claims = _verify_alb_jwt(oidc_data, region)
            email = claims.get("email", "unknown")
            request.state.cognito_email = email

            # Access token carries cognito:groups, which decides operator vs
            # viewer. The ALB does not sign it, so verify against the pool JWKS.
            groups = []
            access_token = request.headers.get("x-amzn-oidc-accesstoken", "")
            pool_id = os.environ.get("COGNITO_USER_POOL_ID")
            client_id = os.environ.get("COGNITO_CLIENT_ID")
            if access_token and pool_id and client_id:
                at_claims = _verify_cognito_access_token(
                    access_token, region, pool_id, client_id
                )
                groups = at_claims.get("cognito:groups", [])
            elif access_token:
                # Without the pool config the groups claim cannot be verified, and
                # an unverified claim grants nothing — a caller can set it freely.
                # Fall through with no groups, which resolves to viewer.
                logger.error(
                    "[AUTH] COGNITO_USER_POOL_ID/COGNITO_CLIENT_ID are not set, so the "
                    "groups claim cannot be verified. Treating caller as viewer. "
                    "Redeploy the UI stack to populate them."
                )

            logger.info(f"Cognito auth: email={email}, groups={groups}")
            if "operators" in groups:
                return "operator"
            elif "viewers" in groups:
                return "viewer"
            return "viewer"
        except Exception as e:
            logger.warning(f"JWT verification failed: {e}")
            return None  # Fail closed — reject if JWT is present but invalid

    # 2. API key auth (constant-time comparison)
    if any([API_KEY, API_KEY_OPERATOR, API_KEY_VIEWER]):
        import secrets
        provided_key = request.headers.get("X-API-Key")
        if not provided_key:
            return None
        if API_KEY_OPERATOR and secrets.compare_digest(provided_key, API_KEY_OPERATOR):
            return "operator"
        if API_KEY_VIEWER and secrets.compare_digest(provided_key, API_KEY_VIEWER):
            return "viewer"
        if API_KEY and secrets.compare_digest(provided_key, API_KEY):
            return "operator"
        return None

    # 3. No auth configured — default to viewer (least privilege)
    # Set DEFAULT_ROLE=operator in .env for pilot mode without Cognito/API keys
    return os.environ.get("DEFAULT_ROLE", "viewer")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Health check is always open (also bypassed at ALB level)
    if request.url.path == "/api/health":
        return await call_next(request)
    
    # Logout needs ALB headers but not role-based auth
    if request.url.path == "/api/logout":
        return await call_next(request)
    
    # Signed-out landing page is unauthenticated (bypassed at ALB level too)
    if request.url.path == "/signed-out":
        return await call_next(request)

    role = _resolve_role(request)

    if role is None:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Viewer role: block write operations
    if role == "viewer" and request.url.path in _OPERATOR_PATHS:
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden: viewer role cannot perform patch operations. Contact your admin for operator access."}
        )

    # Attach role to request state for downstream use
    request.state.role = role
    return await call_next(request)

_local_agent_dir = Path(__file__).parent.parent.parent / "agent"
_docker_agent_dir = Path("/app/agent")
AGENT_DIR = _local_agent_dir if _local_agent_dir.exists() else _docker_agent_dir

_agent_arn = None
_region = None


def _load_config():
    global _agent_arn, _region
    if _agent_arn:
        return

    _region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    # Env var (set by deploy.sh / CDK container env) — the most reliable source.
    _agent_arn = os.environ.get("AGENTCORE_AGENT_ARN")
    if _agent_arn:
        logger.info(f"Loaded agent ARN from env: region={_region}")
        return

    # CLI deployment state — newer @aws/agentcore CLI uses 'runtimes' key,
    # older versions used 'agents'. Walk both.
    cli_state = AGENT_DIR / "agentcore" / ".cli"
    if cli_state.exists():
        import glob
        for sf in glob.glob(str(cli_state / "**" / "*.json"), recursive=True):
            try:
                with open(sf) as f:
                    state = json.load(f)
                targets = state.get("targets", {})
                for target in targets.values():
                    resources = target.get("resources", {})
                    # New CLI: targets.<name>.resources.runtimes.<rt>.runtimeArn
                    # Old CLI: targets.<name>.resources.agents.<a>.runtimeArn
                    runtime_block = resources.get("runtimes") or resources.get("agents") or {}
                    for runtime in runtime_block.values():
                        arn = runtime.get("runtimeArn") or runtime.get("agentArn")
                        if arn:
                            _agent_arn = arn
                            logger.info(f"Loaded agent ARN from CLI state ({sf}): region={_region}")
                            return
                # Old top-level format fallback
                arn = state.get("agentRuntimeArn") or state.get("agent_arn")
                if arn:
                    _agent_arn = arn
                    logger.info(f"Loaded agent ARN from CLI state: region={_region}")
                    return
            except Exception:
                continue

    # CloudFormation fallback — the @aws/agentcore CLI deploys via a CFN stack
    # named AgentCore-<projectname>-<targetname> (default: AgentCore-patchy-default)
    # and emits a Runtime ARN as a stack output. Read it directly. This is the
    # same fallback deploy.sh::resolve_agentcore_role uses for the role ARN.
    try:
        agent_name = os.environ.get("AGENT_NAME", "patchy")
        target_name = os.environ.get("AGENTCORE_TARGET_NAME", "default")
        stack_name = f"AgentCore-{agent_name}-{target_name}"
        cfn = boto3.client("cloudformation", region_name=_region)
        resp = cfn.describe_stacks(StackName=stack_name)
        for output in resp.get("Stacks", [{}])[0].get("Outputs", []) or []:
            key = output.get("OutputKey", "")
            if "RuntimeArn" in key or "AgentArn" in key:
                _agent_arn = output.get("OutputValue")
                if _agent_arn:
                    logger.info(f"Loaded agent ARN from CloudFormation stack {stack_name}: region={_region}")
                    return
    except Exception as e:
        logger.warning(f"Could not read agent ARN from CloudFormation fallback: {e}")

    raise FileNotFoundError(
        "No AgentCore config found. Set AGENTCORE_AGENT_ARN env var, "
        "or run ./deploy.sh agent to deploy the runtime."
    )


def _get_client():
    """Create a fresh boto3 client per request to avoid connection pool issues."""
    _load_config()
    profile = os.environ.get("AWS_PROFILE")
    boto_config = BotoConfig(read_timeout=300, connect_timeout=60, max_pool_connections=1)
    session = boto3.Session(profile_name=profile, region_name=_region) if profile else boto3.Session(region_name=_region)
    return session.client("bedrock-agentcore", config=boto_config)


# ── SSE stream parser ──────────────────────────────────────────────


def _sse_event(event_type: str, **kwargs) -> str:
    """Format a single SSE data line."""
    payload = {"type": event_type, **kwargs}
    return f"data: {json.dumps(payload)}\n\n"


def _parse_agentcore_stream(event_stream, session_id: str = ""):
    """
    Generator that reads the raw AgentCore SSE byte stream and yields
    simplified SSE events for the browser.

    Pure SSE proxy — no SQS, no threads, no workflow event polling.
    Uses read1() on the underlying urllib3 stream for immediate byte delivery.
    """
    current_tool = None
    start_time = time.time()
    line_buffer = ""

    def _process_sse_line(line: str):
        """Parse a single SSE line and return an SSE string to yield, or None."""
        nonlocal current_tool
        line = line.strip()
        if not line or not line.startswith("data: "):
            return None

        try:
            data = json.loads(line[6:])
            if not isinstance(data, dict):
                return None

            # Tool start events
            if "event" in data and "contentBlockStart" in data["event"]:
                block_start = data["event"]["contentBlockStart"]
                start = block_start.get("start", {})
                if "toolUse" in start:
                    tool_name = start["toolUse"].get("name")
                    if tool_name and tool_name != current_tool:
                        current_tool = tool_name
                        return _sse_event("tool_start", tool=tool_name)

            # Text delta events
            if "event" in data and "contentBlockDelta" in data["event"]:
                delta = data["event"]["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    return _sse_event("text", content=delta["text"])

        except json.JSONDecodeError:
            pass
        return None

    # Use read1() on the underlying urllib3 stream for immediate delivery.
    # read1(N) returns up to N bytes already in the socket buffer — no
    # blocking wait to fill the buffer like read(N) does.
    raw_stream = getattr(event_stream, "_raw_stream", None)

    try:
        while True:
            if raw_stream and hasattr(raw_stream, "read1"):
                chunk = raw_stream.read1(8192)
            else:
                chunk = event_stream.read(1)

            if not chunk:
                break

            try:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            except UnicodeDecodeError:
                continue

            line_buffer += text

            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                result = _process_sse_line(line)
                if result:
                    yield result

    except Exception as e:
        logger.error(f"Stream parse error: {e}")
        yield _sse_event("error", message=str(e))
    finally:
        try:
            event_stream.close()
        except Exception:
            pass

    duration_ms = int((time.time() - start_time) * 1000)
    yield _sse_event("done", duration_ms=duration_ms)


# ── Endpoints ───────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """
    Send a message to the agent and stream the response as SSE.

    Body: { "message": "...", "session_id": "..." }
    Returns: text/event-stream with tool_start / text / done events.
    """
    # Rate limit by client IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(status_code=429, content={"error": "Rate limit exceeded. Max 20 requests/minute."})

    message = req.message.strip()
    session_id = req.session_id or f"web-{uuid.uuid4().hex}"

    # Validate session_id format
    if req.session_id and not _SESSION_ID_PATTERN.match(req.session_id):
        return JSONResponse(status_code=422, content={"error": "Invalid session_id format"})

    if not message:
        return JSONResponse(status_code=422, content={"error": "message is required"})

    logger.info(f"Chat request: session_id={session_id} role={getattr(request.state, 'role', 'unknown')} message={message[:80]}")
    client = _get_client()
    role = getattr(request.state, "role", "viewer")
    cognito_email = getattr(request.state, "cognito_email", None)
    operator = cognito_email or f"{role}@{session_id[:12]}"
    payload = json.dumps({"prompt": message, "operator": operator, "timezone": req.timezone or "UTC"}).encode()

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=_agent_arn,
            runtimeSessionId=session_id,
            payload=payload,
        )
    except Exception as e:
        error_str = str(e)
        logger.error(f"invoke_agent_runtime failed: {error_str}")

        # Provide user-friendly error messages for common failures
        if "ValidationException" in error_str and ("too long" in error_str or "token" in error_str.lower() or "context" in error_str.lower()):
            user_message = "Session context limit reached. Please start a new conversation (click the notepad icon in the top right)."
        elif "ThrottlingException" in error_str or "TooManyRequestsException" in error_str:
            user_message = "Rate limit reached. Please wait a moment and try again."
        elif "AccessDeniedException" in error_str or "403" in error_str:
            user_message = "Access denied. The agent runtime may still be starting up — wait 1-2 minutes and retry."
        elif "ResourceNotFoundException" in error_str:
            user_message = "Agent runtime not found. It may have been redeployed — refresh the page."
        else:
            user_message = f"Agent error: {error_str[:200]}"

        def error_stream():
            yield _sse_event("error", message=user_message)
            yield _sse_event("done", duration_ms=0)

        return StreamingResponse(error_stream(), media_type="text/event-stream")

    event_stream = response["response"]
    return StreamingResponse(
        _parse_agentcore_stream(event_stream, session_id=session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    """Health check — verifies agent config is loadable."""
    try:
        _load_config()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/role")
async def get_role(request: Request):
    """Return the authenticated user's role. Used by frontend to toggle UI."""
    role = getattr(request.state, "role", "viewer")
    return {"role": role}


@app.get("/api/auth/config")
async def auth_config(request: Request):
    """Return auth configuration for the frontend (logout URL, user email)."""
    email = getattr(request.state, "cognito_email", None)
    cognito_domain = os.environ.get("COGNITO_DOMAIN_PREFIX", "")

    # Build logout URL — points to /api/logout which clears ALB cookie then Cognito session
    logout_url = None
    if cognito_domain and email:
        host = request.headers.get("host", "")
        scheme = request.headers.get("x-forwarded-proto", "https")
        logout_url = f"{scheme}://{host}/api/logout"

    return {
        "email": email,
        "logoutUrl": logout_url,
        "cognitoEnabled": email is not None,
    }


# ── Session message rehydration ─────────────────────────────────────
# Lets the chat panel restore conversation history on page refresh by
# reading from AgentCore Memory (the same store the agent reads). The
# UI doesn't cache messages client-side — Memory is the source of truth.

_MEMORY_ID = os.environ.get("MEMORY_PATCHMEMORYV2_ID")
_memory_client = None


def _get_memory_client():
    global _memory_client
    if _memory_client is None and _MEMORY_SDK_AVAILABLE:
        _memory_client = MemoryClient(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return _memory_client


def _sanitize_actor_id(actor_id: str) -> str:
    """Match the actor_id format the agent writes (agent_factory._build_actor_id).

    The agent prefixes with "patch-automation/" for memory isolation between
    apps. We must use the SAME format here, otherwise get_last_k_turns
    reads from a different actor namespace and returns nothing.
    """
    base = actor_id or "anonymous"
    sanitized = re.sub(r'[^a-zA-Z0-9\-_/]', '-', base)
    sanitized = re.sub(r'-{2,}', '-', sanitized).strip('-')
    if not sanitized:
        sanitized = "anonymous"
    return f"patch-automation/{sanitized}"


@app.get("/api/session/{session_id}/messages")
async def get_session_messages(session_id: str, request: Request):
    """Return the last K turns of conversation history for a session.

    Reads from AgentCore Memory (server-side store). Returns 404 if the
    memory ID isn't configured, the session doesn't exist, or no messages
    are stored — the frontend treats any non-200 as "start fresh".
    """
    if not _MEMORY_SDK_AVAILABLE or not _MEMORY_ID:
        return JSONResponse(status_code=503, content={"error": "memory_not_configured"})

    if not _SESSION_ID_PATTERN.match(session_id):
        return JSONResponse(status_code=422, content={"error": "Invalid session_id format"})

    cognito_email = getattr(request.state, "cognito_email", None)
    if not cognito_email:
        # Without a known operator we can't determine the actor_id.
        # Return 404 so the frontend mints a fresh session.
        return JSONResponse(status_code=404, content={"error": "no_user_context"})

    actor_id = _sanitize_actor_id(cognito_email)

    try:
        client = _get_memory_client()
        if client is None:
            return JSONResponse(status_code=503, content={"error": "memory_client_unavailable"})
        turns = client.get_last_k_turns(
            memory_id=_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=50,
            branch_name="main",
        )
    except Exception as e:
        logger.info(f"get_last_k_turns failed for session={session_id}: {e}")
        return JSONResponse(status_code=404, content={"error": "session_not_found"})

    messages = []
    # AgentCore's list_events returns events newest-first. get_last_k_turns
    # iterates that order and appends — so `turns` ends up newest-first,
    # and within each turn the messages are also reverse-chronological.
    # Reverse both levels to restore oldest→newest for chat display.
    for turn in reversed(turns or []):
        if not isinstance(turn, list):
            continue
        for m in reversed(turn):
            if not isinstance(m, dict):
                continue
            content_obj = m.get("content", {})
            raw_text = content_obj.get("text", "") if isinstance(content_obj, dict) else str(content_obj)
            role = (m.get("role") or "user").lower()

            # The Strands AgentCoreMemorySessionManager stores each message as
            # a JSON-stringified SessionMessage blob in content.text. We need
            # to parse that and extract just the user-visible text content,
            # skipping toolUse/toolResult blocks (those are Strands plumbing
            # and shouldn't appear as chat bubbles).
            display_text = ""
            try:
                parsed = json.loads(raw_text)
                inner_message = parsed.get("message", parsed) if isinstance(parsed, dict) else None
                if isinstance(inner_message, dict):
                    inner_role = inner_message.get("role")
                    if inner_role:
                        role = inner_role.lower()
                    blocks = inner_message.get("content", [])
                    if isinstance(blocks, list):
                        text_parts = []
                        for block in blocks:
                            if isinstance(block, dict) and "text" in block:
                                t = block.get("text") or ""
                                if t.strip():
                                    text_parts.append(t)
                        display_text = "\n".join(text_parts).strip()
            except (json.JSONDecodeError, TypeError):
                # Not JSON — treat the whole text as plain content (older
                # write format or non-Strands writer).
                display_text = raw_text.strip()

            # Only surface user/assistant turns. Skip tool-only turns
            # (no extracted text means the message was tool plumbing).
            if not display_text:
                continue
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": display_text})

    if not messages:
        return JSONResponse(status_code=404, content={"error": "session_empty"})

    return {"session_id": session_id, "messages": messages}


@app.get("/api/logout")
async def logout(request: Request):
    """Clear ALB session cookie and redirect to Cognito logout.
    
    Per AWS docs: set ALB cookie expiry to -1, then redirect to Cognito /logout.
    The logout_uri must point to an unauthenticated page (not behind ALB auth).
    We use /signed-out which is bypassed in the ALB listener rules.
    """
    cognito_domain = os.environ.get("COGNITO_DOMAIN_PREFIX", "")
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    host = request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", "https")
    # logout_uri must be an unauthenticated page (bypassed in ALB listener rules)
    signed_out_uri = f"{scheme}://{host}/signed-out"

    # Extract client_id from access token
    client_id = ""
    access_token = request.headers.get("x-amzn-oidc-accesstoken", "")
    if access_token:
        try:
            import base64
            payload = access_token.split('.')[1]
            payload += '=' * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            client_id = claims.get("client_id", "")
        except Exception:
            pass

    if cognito_domain and client_id:
        cognito_logout = (
            f"https://{cognito_domain}.auth.{region}.amazoncognito.com/logout?"
            f"client_id={client_id}&"
            f"logout_uri={signed_out_uri}"
        )
    else:
        cognito_logout = signed_out_uri

    from starlette.responses import RedirectResponse
    response = RedirectResponse(url=cognito_logout)
    # Clear ALB session cookies per AWS docs: set expiry to -1
    for i in range(4):
        response.set_cookie(f"AWSELBAuthSessionCookie-{i}", "", max_age=-1, path="/")
    return response


@app.get("/signed-out")
async def signed_out():
    """Unauthenticated landing page after logout. Must be bypassed in ALB auth rules."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <!DOCTYPE html>
    <html><head><title>Signed Out — Patchy</title>
    <style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f9fafb}
    .card{text-align:center;padding:3rem;border-radius:12px;background:white;box-shadow:0 1px 3px rgba(0,0,0,.1)}
    a{color:#2563eb;text-decoration:none;font-weight:600}</style></head>
    <body><div class="card"><h2>Signed out</h2><p>You have been signed out of Patchy.</p><p><a href="/">Sign in again</a></p></div>
    <script>
    // Auto-redirect to login after 2 seconds — forces full page load through ALB auth
    setTimeout(function() { window.location.replace('/'); }, 2000);
    </script>
    </body></html>
    """)


# ── Dashboard ────────────────────────────────────────────────────────

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

ENVIRONMENTS = None  # Discovered dynamically from EC2 Environment tags

MULTI_ACCOUNT_ENABLED = os.environ.get("MULTI_ACCOUNT_ENABLED", "").lower() == "true"
SPOKE_EXECUTION_ROLE = os.environ.get("SPOKE_EXECUTION_ROLE", "PatchySpokeRole")
SPOKE_ACCOUNT_IDS = [a.strip() for a in os.environ.get("SPOKE_ACCOUNT_IDS", "").split(",") if a.strip()]
SPOKE_OU_IDS = [o.strip() for o in os.environ.get("SPOKE_OU_IDS", "").split(",") if o.strip()]
# Regions to query for fleet, vulnerability, and patch data.
# Defaults to the hub region (AWS_REGION) for backward compat.
# Inspector and EC2/SSM are regional APIs — each region needs its own client call.
_DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
SPOKE_REGIONS = [r.strip() for r in os.environ.get("SPOKE_REGIONS", _DEFAULT_REGION).split(",") if r.strip()] or [_DEFAULT_REGION]

_hub_account_id_cache: str | None = None
_sts_credential_cache: dict[str, dict] = {}
_sts_cache_lock = __import__("threading").Lock()


def _aws_session():
    """Create a boto3 session using .env config."""
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION", _region or "us-east-1")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _get_hub_account_id() -> str:
    global _hub_account_id_cache
    if not _hub_account_id_cache:
        _hub_account_id_cache = _aws_session().client("sts").get_caller_identity()["Account"]
    return _hub_account_id_cache


def _get_spoke_session(account_id: str) -> boto3.Session:
    """Assume PatchySpokeRole in a spoke account. Caches credentials for 50 min."""
    region = os.environ.get("AWS_REGION", _region or "us-east-1")
    now = time.time()

    with _sts_cache_lock:
        cached = _sts_credential_cache.get(account_id)
        if cached and cached["expiry"] - now > 300:
            return boto3.Session(
                aws_access_key_id=cached["creds"]["AccessKeyId"],
                aws_secret_access_key=cached["creds"]["SecretAccessKey"],
                aws_session_token=cached["creds"]["SessionToken"],
                region_name=region,
            )

    role_arn = f"arn:aws:iam::{account_id}:role/{SPOKE_EXECUTION_ROLE}"
    sts = _aws_session().client("sts")
    resp = sts.assume_role(RoleArn=role_arn, RoleSessionName=f"patchy-dashboard-{account_id}", DurationSeconds=3600)
    creds = resp["Credentials"]

    with _sts_cache_lock:
        _sts_credential_cache[account_id] = {"creds": creds, "expiry": creds["Expiration"].timestamp()}

    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def _resolve_ou_member_accounts(ou_ids: list[str]) -> list[str]:
    """Resolve OU IDs to active member account IDs via Organizations.

    Returns [] (and logs) on failure so the caller can degrade gracefully.
    Includes the hub if it's a member of one of the listed OUs — caller will
    union with hub anyway.
    """
    if not ou_ids:
        return []
    try:
        org = _aws_session().client("organizations", region_name="us-east-1")
        accounts: list[str] = []
        for ou_id in ou_ids:
            paginator = org.get_paginator("list_accounts_for_parent")
            for page in paginator.paginate(ParentId=ou_id):
                for a in page["Accounts"]:
                    if a["Status"] == "ACTIVE":
                        accounts.append(a["Id"])
        return list(dict.fromkeys(accounts))
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not resolve OU members for {ou_ids}: {e}")
        return []


# Process-lifetime cache for the configured account allowlist. OU expansion via
# Organizations is expensive enough to want caching; org membership rarely changes.
_configured_scope_cache: set | None = None


def _get_configured_scope_accounts() -> set:
    """Return the operator's configured account allowlist for filtering.

    Mirrors the agent's scope resolution (Option C — OUs win):
      1. SPOKE_OU_IDS set → OU members ∪ {hub}
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set → that list ∪ {hub}
      3. Both unset → empty set sentinel meaning "no filter"

    The empty-set sentinel preserves backward compatibility: callers should
    treat an empty result as "don't filter, include everything the AWS API
    returns" (current behavior for operators who haven't configured scope).

    Cached for the process lifetime to avoid hammering Organizations on every
    dashboard refresh.
    """
    global _configured_scope_cache
    if _configured_scope_cache is not None:
        return _configured_scope_cache

    if not MULTI_ACCOUNT_ENABLED:
        try:
            _configured_scope_cache = {_get_hub_account_id()}
        except Exception:
            _configured_scope_cache = set()
        return _configured_scope_cache

    try:
        hub_id = _get_hub_account_id()
    except Exception:
        hub_id = None

    if SPOKE_OU_IDS:
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS)
        scope = set(members) | ({hub_id} if hub_id else set())
        logger.info(f"[DASHBOARD] Configured scope (OU): {len(scope)} accounts from "
                    f"OUs={SPOKE_OU_IDS} (members={len(members)}, hub={'yes' if hub_id else 'no'})")
    elif SPOKE_ACCOUNT_IDS:
        scope = set(SPOKE_ACCOUNT_IDS) | ({hub_id} if hub_id else set())
        logger.info(f"[DASHBOARD] Configured scope (explicit): {len(scope)} accounts "
                    f"(spokes={len(SPOKE_ACCOUNT_IDS)}, hub={'yes' if hub_id else 'no'})")
    else:
        scope = set()
        logger.info("[DASHBOARD] No SPOKE_OU_IDS or SPOKE_ACCOUNT_IDS configured — "
                    "scope filter disabled (org-wide visibility)")

    _configured_scope_cache = scope
    return scope


def _get_dashboard_accounts() -> list[str]:
    """Return SPOKE account IDs (excluding the hub) for fanout queries.

    Mirrors the agent's `_get_spoke_accounts` precedence (Option C — OUs win):
      1. SPOKE_OU_IDS set → resolve OU members.
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set → use that list.
      3. Both unset → full org discovery.

    All branches exclude the hub account and dedupe — the hub is queried
    separately with local credentials, so listing it as a spoke would cause
    duplicate fan-outs and misleading 'scopes' output.
    """
    if not MULTI_ACCOUNT_ENABLED:
        return []
    try:
        hub_id = _get_hub_account_id()
    except Exception:
        hub_id = None

    if SPOKE_OU_IDS:
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS)
        return list(dict.fromkeys(a for a in members if a and a != hub_id))
    if SPOKE_ACCOUNT_IDS:
        return list(dict.fromkeys(a for a in SPOKE_ACCOUNT_IDS if a and a != hub_id))
    # Full org discovery
    try:
        org = _aws_session().client("organizations", region_name="us-east-1")
        accounts: list[str] = []
        paginator = org.get_paginator("list_accounts")
        for page in paginator.paginate():
            for a in page["Accounts"]:
                if a["Status"] == "ACTIVE" and a["Id"] != hub_id:
                    accounts.append(a["Id"])
        return list(dict.fromkeys(accounts))
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not discover spoke accounts: {e}")
        return []


def _fetch_environments():
    """Sync: Fleet overview via SSM Explorer (cross-account native).
    Falls back to per-account EC2/SSM queries if Explorer unavailable.

    Returns (environments_list, explorer_status_dict) where explorer_status_dict is:
      {'state': 'ok' | 'empty' | 'missing' | 'error',
       'sync_age_minutes': int | None,   # how long ago the sync was created (when known)
       'detail': str}                    # short diagnostic for logs / UI
    """
    explorer_data, status = _fetch_environments_via_explorer_with_status()
    if status['state'] == 'ok':
        return explorer_data, status
    # Fall back to per-account fan-out — still serves data, just slower / less complete
    return _fetch_environments_via_fanout(), status


def _check_explorer_sync_status() -> dict:
    """Look up patchy-fleet-sync metadata to distinguish 'missing' vs 'warming up'.

    Returns:
        {'exists': bool, 'sync_age_minutes': int | None, 'source_regions': list[str]}
    """
    sync_name = "patchy-fleet-sync"
    region = os.environ.get("AWS_REGION", _region or "us-east-1")
    try:
        ssm = _aws_session().client("ssm", region_name=region)
        resp = ssm.list_resource_data_sync(SyncType="SyncFromSource")
        for item in resp.get("ResourceDataSyncItems", []):
            if item.get("SyncName") != sync_name:
                continue
            created = item.get("SyncCreatedTime")
            age_min = None
            if created:
                # boto3 returns timezone-aware datetime
                from datetime import datetime as _dt, timezone as _tz
                age_min = int((_dt.now(_tz.utc) - created).total_seconds() / 60)
            return {
                "exists": True,
                "sync_age_minutes": age_min,
                "source_regions": item.get("SyncSource", {}).get("SourceRegions", []),
            }
        return {"exists": False, "sync_age_minutes": None, "source_regions": []}
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not list resource data syncs: {e}")
        return {"exists": False, "sync_age_minutes": None, "source_regions": []}


def _fetch_environments_via_explorer_with_status() -> tuple[list, dict]:
    """SSM Explorer GetOpsSummary — returns (data, status_dict).

    status_dict.state is one of:
      - 'ok':      Explorer returned data
      - 'empty':   Explorer call succeeded but returned no entities (sync warming up,
                   or no scoped instances anywhere)
      - 'missing': Sync 'patchy-fleet-sync' does not exist
      - 'error':   API call raised (permissions, throttling, etc.)
    """
    data = _fetch_environments_via_explorer()
    if data is not None and len(data) > 0:
        return data, {"state": "ok", "sync_age_minutes": None, "detail": "explorer returned data"}

    # No data — figure out why before falling back to fan-out
    sync_info = _check_explorer_sync_status()
    if not sync_info["exists"]:
        return [], {
            "state": "missing",
            "sync_age_minutes": None,
            "detail": "patchy-fleet-sync not found",
        }

    age = sync_info["sync_age_minutes"]
    return [], {
        "state": "empty",
        "sync_age_minutes": age,
        "detail": f"sync exists ({sync_info['source_regions']}) but no entities yet",
    }


def _fetch_environments_via_explorer() -> list | None:
    """SSM Explorer GetOpsSummary with Resource Data Sync — single call, cross-account."""
    try:
        session = _aws_session()
        region = os.environ.get("AWS_REGION", _region or "us-east-1")
        sync_name = "patchy-fleet-sync"
        scope_tag_key = os.environ.get("SSM_SCOPE_TAG_KEY", "PatchAutomation")
        scope_tag_value = os.environ.get("SSM_SCOPE_TAG_VALUE", "enabled")

        # Step 1: Get all instances from Explorer (one API call)
        ssm = session.client("ssm", region_name=region)
        raw_instances: dict[str, dict] = {}
        next_token = None
        while True:
            kwargs: dict = {
                "SyncName": sync_name,
                "ResultAttributes": [{"TypeName": "AWS:EC2InstanceInformation"}],
                "MaxResults": 50,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = ssm.get_ops_summary(**kwargs)
            for entity in resp.get("Entities", []):
                iid = entity.get("Id", "")
                if not iid.startswith("i-"):
                    continue
                content = (entity.get("Data", {}).get("AWS:EC2InstanceInformation", {}).get("Content") or [{}])[0]
                raw_instances[iid] = {
                    "account_id": content.get("SourceAccountId", ""),
                    "region": content.get("SourceRegion", region),
                    "online": content.get("IsManaged") == "true",
                }
            next_token = resp.get("NextToken")
            if not next_token:
                break

        if not raw_instances:
            return None

        logger.info(f"[DASHBOARD] Explorer: {len(raw_instances)} instances from sync")

        # Scope filter — Explorer's Resource Data Sync ingests OpsData from
        # EntireOrganization, so it returns instances from accounts outside the
        # operator's configured scope (e.g., management account). Drop those
        # before paying the cost of EC2/SSM enrichment. This mirrors the agent's
        # _get_fleet_summary scope filter.
        allowed = _get_configured_scope_accounts()
        if allowed:
            before = len(raw_instances)
            raw_instances = {iid: inst for iid, inst in raw_instances.items()
                             if inst.get("account_id") in allowed}
            dropped = before - len(raw_instances)
            if dropped:
                logger.info(f"[DASHBOARD] Explorer scope filter: dropped {dropped} instance(s) "
                            f"from accounts outside configured scope ({len(allowed)} allowed)")
            if not raw_instances:
                return None

        # Step 2: Enrich with tags and patch data per (account, region) bucket.
        # Bucketing by account alone caused cross-region instances to inherit the FIRST
        # instance's region for the EC2/SSM lookups, silently dropping any instance in
        # a different region. Now each (account, region) pair gets its own clients.
        instances: dict[str, dict] = {}
        by_account_region: dict[tuple, list] = {}
        for iid, inst in raw_instances.items():
            key = (inst["account_id"], inst["region"])
            by_account_region.setdefault(key, []).append(iid)

        for (account_id, rgn), iids in by_account_region.items():
            tags_map: dict[str, dict] = {}
            patch_map: dict[str, dict] = {}
            spoke = None
            try:
                spoke = _get_spoke_session(account_id) if account_id != _get_hub_account_id() else session
                ec2 = spoke.client("ec2", region_name=rgn)
                for page in ec2.get_paginator("describe_instances").paginate(InstanceIds=iids):
                    for res in page.get("Reservations", []):
                        for inst in res.get("Instances", []):
                            tags_map[inst["InstanceId"]] = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            except Exception as e:
                logger.warning(f"[DASHBOARD] Tag lookup failed for {account_id}/{rgn}: {e}")
            try:
                spoke_ssm = (spoke if (spoke is not None and account_id != _get_hub_account_id()) else session).client("ssm", region_name=rgn)
                for i in range(0, len(iids), 50):
                    resp = spoke_ssm.describe_instance_patch_states(InstanceIds=iids[i:i + 50])
                    for s in resp.get("InstancePatchStates", []):
                        patch_map[s["InstanceId"]] = {"missing": s.get("MissingCount", 0), "installed": s.get("InstalledCount", 0), "failed": s.get("FailedCount", 0)}
            except Exception as e:
                logger.warning(f"[DASHBOARD] Patch state lookup failed for {account_id}/{rgn}: {e}")

            for iid in iids:
                tags = tags_map.get(iid, {})
                if tags.get(scope_tag_key) != scope_tag_value:
                    continue
                patch = patch_map.get(iid, {})
                instances[iid] = {
                    "account_id": account_id,
                    "region": rgn,
                    "environment": tags.get("Environment", "unknown"),
                    "online": raw_instances[iid]["online"],
                    "missing": int(patch.get("missing", 0)),
                    "installed": int(patch.get("installed", 0)),
                    "failed": int(patch.get("failed", 0)),
                }

        if not instances:
            return None

        envs: dict[str, dict] = {}
        for inst in instances.values():
            env = inst["environment"]
            if env not in envs:
                envs[env] = {"environment": env, "total": 0, "online": 0, "offline": 0,
                             "unmanaged_count": 0, "status": "inactive",
                             "accounts": set(), "per_account": {},
                             "patch_compliance": {"compliant_instances": 0, "scanned_instances": 0,
                                                  "compliance_pct": None, "missing_patches": 0,
                                                  "installed_patches": 0, "failed_patches": 0}}
            e = envs[env]
            e["total"] += 1
            e["online" if inst["online"] else "offline"] += 1
            pc = e["patch_compliance"]
            pc["missing_patches"] += inst["missing"]
            pc["installed_patches"] += inst["installed"]
            pc["failed_patches"] += inst["failed"]
            if inst["installed"] > 0:
                pc["scanned_instances"] += 1
                if inst["missing"] == 0:
                    pc["compliant_instances"] += 1
            acct = inst["account_id"]
            e["accounts"].add(acct)
            if acct not in e["per_account"]:
                e["per_account"][acct] = {
                    "total": 0, "online": 0, "offline": 0,
                    "patch_compliance": {"compliant_instances": 0, "scanned_instances": 0,
                                         "compliance_pct": None, "missing_patches": 0,
                                         "installed_patches": 0, "failed_patches": 0}}
            pa = e["per_account"][acct]
            pa["total"] += 1
            pa["online" if inst["online"] else "offline"] += 1
            papc = pa["patch_compliance"]
            papc["missing_patches"] += inst["missing"]
            papc["installed_patches"] += inst["installed"]
            papc["failed_patches"] += inst["failed"]
            if inst["installed"] > 0:
                papc["scanned_instances"] += 1
                if inst["missing"] == 0:
                    papc["compliant_instances"] += 1

        for e in envs.values():
            sc = e["patch_compliance"]["scanned_instances"]
            ci = e["patch_compliance"]["compliant_instances"]
            e["patch_compliance"]["compliance_pct"] = round(ci / sc * 100) if sc > 0 else None
            e["status"] = ("healthy" if e["online"] == e["total"] and e["total"] > 0
                           else "warning" if e["online"] > 0
                           else "error" if e["total"] > 0 else "inactive")
            e["accounts"] = sorted(e["accounts"])
            for pa in e["per_account"].values():
                pasc = pa["patch_compliance"]["scanned_instances"]
                paci = pa["patch_compliance"]["compliant_instances"]
                pa["patch_compliance"]["compliance_pct"] = round(paci / pasc * 100) if pasc > 0 else None

        logger.info(f"[DASHBOARD] Explorer: {len(instances)} instances, {len(envs)} environments")
        return sorted(envs.values(), key=lambda x: x["environment"])

    except Exception as e:
        logger.warning(f"[DASHBOARD] Explorer unavailable, falling back to fan-out: {e}")
        return None


def _fetch_environments_via_fanout() -> list:
    """Fallback: per-(account, region) EC2/SSM queries when Explorer is unavailable.

    Iterates SPOKE_REGIONS for the hub AND each spoke account so cross-region
    instances aren't silently dropped.
    """
    hub_session = _aws_session()
    hub_id = _get_account_id()

    # Hub: one fetch per region using local credentials
    hub_results: list[list] = []
    for rgn in SPOKE_REGIONS:
        try:
            hub_results.append(_fetch_environments_for_session(hub_session, account_id=hub_id, region=rgn))
        except Exception as e:
            logger.warning(f"[DASHBOARD] Hub env fetch failed for {hub_id}/{rgn}: {e}")

    spoke_accounts = _get_dashboard_accounts()
    if not spoke_accounts:
        return _merge_environment_results(hub_results)

    # Spokes: one assume-role per account, then iterate regions on the same session
    spoke_results: list[list] = []
    with ThreadPoolExecutor(max_workers=min(len(spoke_accounts), 10)) as executor:
        futures = {executor.submit(_fetch_environments_for_account, a): a for a in spoke_accounts}
        for future in as_completed(futures):
            try:
                spoke_results.extend(future.result(timeout=15))
            except TimeoutError:
                logger.warning(f"[DASHBOARD] Worker timed out for spoke {futures[future]}")
            except Exception as e:
                logger.warning(f"[DASHBOARD] Spoke env fetch failed for {futures[future]}: {e}")

    return _merge_environment_results(hub_results + spoke_results)


def _merge_environment_results(env_lists: list[list]) -> list:
    """Merge per-environment results from multiple (account, region) fetches.

    Important: per_account[account_id] is summed across regions, NOT overwritten.
    The shallow-dict merge ({**a, **b}) used by an earlier version let later
    regions clobber the counters from earlier regions when the same account
    appeared in both. Result: env-level totals were correct (because we use +=
    for those) but per-account drill-downs only reflected the last region merged.
    """
    merged: dict[str, dict] = {}
    for env_list in env_lists:
        for env in env_list:
            name = env["environment"]
            if name not in merged:
                # First time we've seen this env — defensive copy so later mutations
                # don't ripple back into the source dict (which is shared across
                # threads via the ThreadPoolExecutor result list)
                merged[name] = {**env}
                merged[name]["per_account"] = {
                    aid: {
                        **pa,
                        "patch_compliance": dict(pa.get("patch_compliance", {})),
                    }
                    for aid, pa in env.get("per_account", {}).items()
                }
                merged[name]["accounts"] = list(env.get("accounts", []))
                merged[name]["patch_compliance"] = dict(env.get("patch_compliance", {})) if env.get("patch_compliance") else {}
                continue

            m = merged[name]
            m["total"] += env["total"]
            m["online"] += env["online"]
            m["offline"] += env["offline"]
            m["unmanaged_count"] = m.get("unmanaged_count", 0) + env.get("unmanaged_count", 0)
            m["accounts"] = list(set(m.get("accounts", []) + env.get("accounts", [])))

            # per_account: sum across regions for the same account, not overwrite
            for aid, pa_new in env.get("per_account", {}).items():
                pa_existing = m["per_account"].get(aid)
                if pa_existing is None:
                    # First contribution from this account — defensive copy
                    m["per_account"][aid] = {
                        **pa_new,
                        "patch_compliance": dict(pa_new.get("patch_compliance", {})),
                    }
                else:
                    # Same account, additional region — accumulate counters
                    for k in ("total", "online", "offline"):
                        pa_existing[k] = pa_existing.get(k, 0) + pa_new.get(k, 0)
                    pc_e = pa_existing.setdefault("patch_compliance", {})
                    pc_n = pa_new.get("patch_compliance", {})
                    for k in ("compliant_instances", "scanned_instances",
                              "missing_patches", "installed_patches", "failed_patches"):
                        pc_e[k] = pc_e.get(k, 0) + pc_n.get(k, 0)
                    sc = pc_e.get("scanned_instances", 0)
                    ci = pc_e.get("compliant_instances", 0)
                    pc_e["compliance_pct"] = round(ci / sc * 100) if sc > 0 else None

            # Env-level patch_compliance: sum and recompute pct
            if env.get("patch_compliance") and m.get("patch_compliance"):
                for k in ("compliant_instances", "scanned_instances", "missing_patches", "installed_patches", "failed_patches"):
                    m["patch_compliance"][k] = m["patch_compliance"].get(k, 0) + env["patch_compliance"].get(k, 0)
                sc = m["patch_compliance"]["scanned_instances"]
                ci = m["patch_compliance"]["compliant_instances"]
                m["patch_compliance"]["compliance_pct"] = round(ci / sc * 100) if sc > 0 else None
            m["status"] = ("healthy" if m["online"] == m["total"] and m["total"] > 0
                           else "warning" if m["online"] > 0
                           else "error" if m["total"] > 0 else "inactive")

    return sorted(merged.values(), key=lambda x: x["environment"])


def _fetch_environments_for_account(account_id: str) -> list[list]:
    """Fetch environments from a spoke account across all SPOKE_REGIONS.

    Returns a list of per-region results (one element per region) so the caller
    can feed them into _merge_environment_results alongside hub results.
    """
    try:
        session = _get_spoke_session(account_id)
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not assume role in spoke {account_id}: {e}")
        return []

    results: list[list] = []
    for rgn in SPOKE_REGIONS:
        try:
            results.append(_fetch_environments_for_session(session, account_id=account_id, region=rgn))
        except Exception as e:
            logger.warning(f"[DASHBOARD] Spoke env fetch failed for {account_id}/{rgn}: {e}")
    return results


def _fetch_environments_for_session(session, account_id: str = "", region: str | None = None):
    """Fetch environments from a single (account, region) using the provided session.

    Inspector, EC2, and SSM are regional services. Callers pass an explicit region
    so a single boto3 session can be reused across regions without rebuilding it.
    Falls back to the session's default region if not supplied.
    """
    ec2 = session.client("ec2", region_name=region) if region else session.client("ec2")
    ssm = session.client("ssm", region_name=region) if region else session.client("ssm")

    scope_tag_key = os.environ.get("SSM_SCOPE_TAG_KEY", "PatchAutomation")
    scope_tag_value = os.environ.get("SSM_SCOPE_TAG_VALUE", "enabled")

    env_instances: dict[str, list[str]] = {}
    env_unmanaged: dict[str, int] = {}
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": "tag-key", "Values": ["Environment"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    ):
        for res in page.get("Reservations", []):
            for inst in res.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                env = tags.get("Environment", "")
                if not env:
                    continue
                if tags.get(scope_tag_key) == scope_tag_value:
                    env_instances.setdefault(env, []).append(inst["InstanceId"])
                else:
                    env_unmanaged[env] = env_unmanaged.get(env, 0) + 1

    all_ids = [i for ids in env_instances.values() for i in ids]
    ssm_online: set[str] = set()
    if all_ids:
        ssm_paginator = ssm.get_paginator("describe_instance_information")
        for page in ssm_paginator.paginate(
            Filters=[{"Key": "InstanceIds", "Values": all_ids}]
        ):
            for info in page.get("InstanceInformationList", []):
                if info.get("PingStatus") == "Online":
                    ssm_online.add(info["InstanceId"])

    # Fetch patch compliance state (same data as SSM Patch Manager console)
    # describe_instance_patch_states has a max of 50 IDs per call.
    patch_states: dict[str, dict] = {}
    if all_ids:
        try:
            for i in range(0, len(all_ids), 50):
                batch = all_ids[i:i + 50]
                resp = ssm.describe_instance_patch_states(InstanceIds=batch)
                for state in resp.get("InstancePatchStates", []):
                    patch_states[state["InstanceId"]] = {
                        "missing": state.get("MissingCount", 0),
                        "installed": state.get("InstalledCount", 0),
                        "failed": state.get("FailedCount", 0),
                    }
        except Exception as e:
            logger.warning(f"Could not fetch patch states: {e}")  # Task role may lack ssm:DescribeInstancePatchStates

    logger.info(f"[DASHBOARD] patch_states: {len(patch_states)} instances, all_ids: {len(all_ids)}")

    results = []
    for env in sorted(env_instances.keys()):
        ids = env_instances[env]
        online = sum(1 for i in ids if i in ssm_online)

        # Aggregate patch compliance per environment
        env_missing = sum(patch_states.get(i, {}).get("missing", 0) for i in ids)
        env_installed = sum(patch_states.get(i, {}).get("installed", 0) for i in ids)
        env_failed = sum(patch_states.get(i, {}).get("failed", 0) for i in ids)
        compliant_instances = sum(1 for i in ids if i in patch_states and patch_states[i]["missing"] == 0)
        scanned_instances = sum(1 for i in ids if i in patch_states)
        compliance_pct = round(compliant_instances / scanned_instances * 100) if scanned_instances > 0 else None

        _pa = {
            "total": len(ids), "online": online, "offline": len(ids) - online,
            "patch_compliance": {
                "compliant_instances": compliant_instances, "scanned_instances": scanned_instances,
                "compliance_pct": compliance_pct, "missing_patches": env_missing,
                "installed_patches": env_installed, "failed_patches": env_failed,
            },
        }
        results.append({
            "environment": env,
            "total": len(ids),
            "unmanaged_count": env_unmanaged.get(env, 0),
            "online": online,
            "offline": len(ids) - online,
            "status": "healthy" if online == len(ids) and len(ids) > 0
                      else "warning" if online > 0
                      else "error" if len(ids) > 0
                      else "inactive",
            "accounts": [account_id] if account_id else [],
            "per_account": {account_id: _pa} if account_id else {},
            "patch_compliance": _pa["patch_compliance"],
        })

    # Include environments that have ONLY unmanaged instances
    for env, count in env_unmanaged.items():
        if env not in env_instances:
            results.append({
                "environment": env,
                "total": 0,
                "unmanaged_count": count,
                "online": 0,
                "offline": 0,
                "status": "unmanaged",
                "accounts": [account_id] if account_id else [],
                "per_account": {},
                "patch_compliance": {},
            })

    return results


def _fetch_vulnerabilities():
    """Sync: Active Inspector findings with full pagination across all configured regions.

    Inspector at a delegated administrator returns org-wide findings per region
    regardless of which account's session is used. We only need to query each
    region ONCE (using the hub session) to get all findings. Per-account fanout
    is unnecessary and would cause double-counting of instance_count.

    The scope filter (inside _fetch_vulnerabilities_for_session) drops findings
    from accounts outside the configured allowlist.
    """
    hub_id = _get_account_id()
    hub_session = _aws_session()

    # One call per region using the hub session — Inspector returns org-wide data
    work: list[tuple[str, str, "boto3.Session"]] = []
    for rgn in SPOKE_REGIONS:
        work.append((hub_id, rgn, hub_session))

    # Concurrent fan-out (one per region, not per account)
    all_findings: dict[str, dict] = {}
    if not work:
        return {"findings": [], "severity_counts": {}}

    max_workers = min(len(work), 20)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_vulnerabilities_for_session, sess, rgn, acct): (acct, rgn)
            for (acct, rgn, sess) in work
        }
        for future in as_completed(futures):
            acct, rgn = futures[future]
            try:
                result = future.result(timeout=15)
                _merge_findings_into(all_findings, result.get("findings_map", {}))
            except TimeoutError:
                logger.warning(f"[DASHBOARD] Worker timed out for vuln fetch {acct}/{rgn}")
            except Exception as e:
                logger.warning(f"[DASHBOARD] Vuln fetch failed for {acct}/{rgn}: {e}")

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFORMATIONAL": 4, "UNTRIAGED": 5, "UNKNOWN": 6}
    findings = sorted(all_findings.values(), key=lambda x: (sev_order.get(x["severity"], 9), -(x["cvss_score"] or 0)))
    severity_counts: dict[str, int] = {}
    for cve in findings:
        severity_counts[cve["severity"]] = severity_counts.get(cve["severity"], 0) + 1
    return {"findings": findings, "severity_counts": severity_counts}


def _merge_findings_into(target: dict[str, dict], source: dict[str, dict]) -> None:
    """Merge a per-region findings_map into the cross-region aggregate.

    For each CVE: increment instance_count, union accounts/regions/environments/services.
    Mutates `target` in place.
    """
    for cve_id, entry in source.items():
        if cve_id in target:
            existing = target[cve_id]
            existing["instance_count"] += entry["instance_count"]
            for key in ("accounts", "environments", "services", "regions"):
                for v in entry.get(key, []):
                    if v not in existing.get(key, []):
                        existing.setdefault(key, []).append(v)
        else:
            # Defensive copy so subsequent mutations don't ripple back into the source map
            target[cve_id] = {**entry, **{k: list(entry.get(k, [])) for k in ("accounts", "environments", "services", "regions")}}


def _fetch_vulns_for_account(account_id: str) -> dict:
    """Backwards-compat helper kept for any external callers — no longer used internally."""
    try:
        session = _get_spoke_session(account_id)
        # Default to first SPOKE_REGION; primary fan-out goes through _fetch_vulnerabilities now
        return _fetch_vulnerabilities_for_session(session, region=SPOKE_REGIONS[0], account_id=account_id)
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not query vulns for spoke {account_id}: {e}")
        return {"findings_map": {}, "findings": [], "severity_counts": {}}


def _fetch_vulnerabilities_for_session(session, region: str | None = None, account_id: str = ""):
    """Fetch vulnerabilities from a single (account, region) using the provided session.

    Inspector is regional — `region` is required to query a non-hub region.
    Falls back to the session's default region if not supplied.
    """
    inspector = session.client("inspector2", region_name=region) if region else session.client("inspector2")

    # Inspector at the DA returns org-wide findings on every call, so we must
    # filter to the operator's configured scope. Mirrors agent behaviour. Empty
    # set = "no filter" sentinel for operators who haven't set SPOKE_OU_IDS or
    # SPOKE_ACCOUNT_IDS.
    allowed_accounts = _get_configured_scope_accounts()

    # Deduplicate: CVE ID → aggregated finding
    cve_map: dict[str, dict] = {}
    next_token = None

    while True:
        # Apply the same resource type filter as the agent tools
        resource_types_str = os.environ.get("INSPECTOR_RESOURCE_TYPES", "EC2")
        type_map = {"EC2": "AWS_EC2_INSTANCE", "ECR": "AWS_ECR_CONTAINER_IMAGE", "LAMBDA": "AWS_LAMBDA_FUNCTION"}
        resource_filters = [
            {"comparison": "EQUALS", "value": type_map.get(t.strip().upper(), t.strip())}
            for t in resource_types_str.split(",") if t.strip()
        ]
        kwargs: dict = {
            "filterCriteria": {
                "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
                "resourceType": resource_filters,
            },
            "sortCriteria": {"field": "SEVERITY", "sortOrder": "DESC"},
            "maxResults": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        resp = inspector.list_findings(**kwargs)

        for f in resp.get("findings", []):
            vuln = f.get("packageVulnerabilityDetails", {})
            sev = f.get("severity", "UNKNOWN")
            cve_id = vuln.get("vulnerabilityId", "N/A")

            env = "unknown"
            resource_type = "unknown"
            resource_id = ""
            resource_region = region or ""
            acct = f.get("awsAccountId", "unknown")

            # Scope filter — drop findings from accounts outside the allowlist
            if allowed_accounts and acct not in allowed_accounts:
                continue
            for r in f.get("resources", []):
                resource_type = r.get("type", "unknown")
                resource_id = r.get("id", "")
                # Inspector reports the resource's region per-finding; prefer it over
                # the caller-supplied region in case they differ (shouldn't, but defensive).
                resource_region = r.get("region", resource_region) or resource_region
                for t in r.get("tags", {}).items():
                    if t[0] == "Environment":
                        env = t[1]

            service = _resource_type_to_service(resource_type)

            if cve_id in cve_map:
                entry = cve_map[cve_id]
                entry["instance_count"] += 1
                if env not in entry["environments"]:
                    entry["environments"].append(env)
                if service not in entry["services"]:
                    entry["services"].append(service)
                if acct not in entry["accounts"]:
                    entry["accounts"].append(acct)
                if resource_region and resource_region not in entry.get("regions", []):
                    entry.setdefault("regions", []).append(resource_region)
            else:
                cve_map[cve_id] = {
                    "cve_id": cve_id,
                    "severity": sev,
                    "cvss_score": (vuln.get("cvss") or [{}])[0].get("baseScore") if vuln.get("cvss") else None,
                    "title": f.get("title", ""),
                    "environment": env,
                    "environments": [env],
                    "accounts": [acct],
                    "regions": [resource_region] if resource_region else [],
                    "services": [service],
                    "resource_id": resource_id,
                    "fix_available": f.get("fixAvailable", "UNKNOWN"),
                    "instance_count": 1,
                }

        next_token = resp.get("nextToken")
        if not next_token:
            break

    # Sort by severity order, then CVSS desc
    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFORMATIONAL": 4, "UNTRIAGED": 5, "UNKNOWN": 6}
    findings = sorted(cve_map.values(), key=lambda x: (sev_order.get(x["severity"], 9), -(x["cvss_score"] or 0)))

    # Count unique CVEs per severity (not total findings which double-counts per instance)
    severity_counts = {}
    for cve in findings:
        sev = cve["severity"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    return {"findings": findings, "severity_counts": severity_counts, "findings_map": cve_map}


def _resource_type_to_service(resource_type: str) -> str:
    """Map Inspector resource type to friendly service name."""
    mapping = {
        "AWS_EC2_INSTANCE": "EC2",
        "AWS_LAMBDA_FUNCTION": "Lambda",
        "AWS_ECR_CONTAINER_IMAGE": "ECR",
        "AWS_ECR_REPOSITORY": "ECR",
    }
    return mapping.get(resource_type, resource_type.replace("AWS_", "").replace("_", " ").title())


def _list_report_keys(s3, bucket: str, days: int) -> list[dict]:
    """List all S3 report objects for the last N days in a single pass.

    Uses year/month prefix to minimize list_objects_v2 calls — e.g. 30 days
    spanning 2 months = 2 API calls instead of 30.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    # Collect unique year/month prefixes covering the range
    months: set[str] = set()
    d = cutoff
    while d <= now:
        months.add(f"{d.year}/{d.month:02d}/")
        d += timedelta(days=28)  # jump by ~month
    months.add(f"{now.year}/{now.month:02d}/")  # ensure current month

    objects = []
    for prefix in sorted(months):
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    # Filter by actual date cutoff
                    if obj["LastModified"].replace(tzinfo=None) >= cutoff:
                        objects.append(obj)
        except Exception:
            continue
    return objects


def _read_report_json(s3, bucket: str, key: str) -> dict | None:
    """Read a single report JSON from S3. Returns parsed dict or None."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except Exception:
        return None


def _count_execution_outcomes(ssm_hub, execution_id: str, hub_region: str) -> dict:
    """Walk a parent Automation's child SendCommand invocations to count outcomes.

    Returns {success_count, failure_count, instance_count, instance_ids}
    aggregated across every (account, region) child of the parent execution.
    Used by the compliance reconciler to populate fields the agent can't
    supply at start time — for tag-based runs the instance list isn't known
    until SSM resolves the tag query.

    Cross-account: child Automation executions live in spoke accounts and
    their CommandIds are spoke-scoped. We assume PatchySpokeRole into each
    child's owning account before calling list_command_invocations. Without
    this, ListCommandInvocations from the hub silently returns zero results
    even though the patches actually ran.

    Best-effort: any per-step lookup failure is logged and skipped, returning
    partial counts rather than failing the whole reconcile.
    """
    success = 0
    failure = 0
    seen_instances: set[str] = set()

    # Cache one ssm client per (account, region) so we don't repeat assume-role
    # for siblings of the same child. Hub session keyed by ('', '').
    ssm_clients: dict[tuple[str, str], object] = {}

    def _ssm_client_for(account_id: str, region: str):
        # The hub is reachable with our own credentials, and assuming a spoke
        # role into our own account fails with AccessDenied — so normalise the
        # hub account to the hub session rather than paying a failed STS call
        # for every same-account child.
        if account_id:
            try:
                if account_id == _get_hub_account_id():
                    account_id = ""
            except Exception:
                pass  # Can't resolve our own account; fall through and try.
        # Empty account_id means use the hub session.
        key = (account_id or "", region or hub_region)
        if key in ssm_clients:
            return ssm_clients[key]
        if not account_id:
            client = _aws_session().client("ssm", region_name=region or hub_region)
        else:
            try:
                spoke_session = _get_spoke_session(account_id)
                client = spoke_session.client("ssm", region_name=region or hub_region)
            except Exception as exc:
                logger.warning(
                    f"[RECONCILE] Could not assume role into {account_id} for {execution_id}: {exc}"
                )
                # Fallback to hub session — unlikely to find anything but
                # avoids an exception breaking the loop.
                client = _aws_session().client("ssm", region_name=region or hub_region)
        ssm_clients[key] = client
        return client

    def _tally_command(client, command_id: str) -> None:
        nonlocal success, failure
        next_token = None
        while True:
            kwargs = {"CommandId": command_id, "MaxResults": 50}
            if next_token:
                kwargs["NextToken"] = next_token
            try:
                resp = client.list_command_invocations(**kwargs)
            except Exception as exc:
                logger.warning(f"[RECONCILE] list_command_invocations failed for {command_id}: {exc}")
                return
            for inv in resp.get("CommandInvocations", []):
                seen_instances.add(inv["InstanceId"])
                status = inv.get("Status", "Unknown")
                if status == "Success":
                    success += 1
                elif status in ("Cancelled", "TimedOut", "Failed"):
                    failure += 1
            next_token = resp.get("NextToken")
            if not next_token:
                break

    def _walk_steps(client, steps: list, depth: int = 0) -> None:
        """Walk automation steps to find and tally SendCommand invocations.

        Recurses into aws:executeAutomation steps (up to depth 3) to handle
        the 3-level tree created by instance-ID patching with TargetLocations:
          Parent → Child (per-account) → Grandchild (per-instance) → aws:runCommand
        """
        if depth > 3:
            return
        for step in steps or []:
            outputs = step.get("Outputs") or {}
            cmd_id = outputs.get("CommandId", [None])[0]
            if cmd_id:
                _tally_command(client, cmd_id)
            elif step.get("Action") == "aws:executeAutomation":
                # Recurse into sub-executions (handles instance-ID TargetLocations)
                for exec_id in outputs.get("ExecutionId", []) or outputs.get("AutomationExecutionId", []) or []:
                    if not exec_id:
                        continue
                    try:
                        sub_exec = client.get_automation_execution(
                            AutomationExecutionId=exec_id
                        )["AutomationExecution"]
                        _walk_steps(client, sub_exec.get("StepExecutions", []), depth + 1)
                    except Exception as exc:
                        logger.warning(f"[RECONCILE] Could not walk sub-execution {exec_id}: {exc}")

    # Parent execution lives in the hub.
    try:
        parent = ssm_hub.get_automation_execution(
            AutomationExecutionId=execution_id
        )["AutomationExecution"]
    except Exception as exc:
        logger.warning(f"[RECONCILE] get_automation_execution failed for {execution_id}: {exc}")
        return {"success_count": 0, "failure_count": 0, "instance_count": 0, "instance_ids": []}

    # Path A (instance-ID, no TargetLocations) emits the CommandId directly
    # on the parent's StepExecutions and runs in the hub.
    _walk_steps(_ssm_client_for("", hub_region), parent.get("StepExecutions", []))

    # Path A with TargetLocations (now the default for instance-ID runs) and
    # Path B (tag-based) both spawn child executions. Two ways to find them:
    #   1. describe_automation_executions(ParentExecutionId=...) — works for
    #      cross-account, sometimes empty for same-account dispatch
    #   2. parent's `aws:executeAutomation` step Outputs — always reliable
    # Use both and dedupe so we never miss a child.
    child_id_set: set[str] = set()

    # Source 1: describe_automation_executions
    try:
        children_meta = ssm_hub.describe_automation_executions(
            Filters=[{"Key": "ParentExecutionId", "Values": [execution_id]}],
            MaxResults=50,
        ).get("AutomationExecutionMetadataList", [])
    except Exception as exc:
        logger.warning(f"[RECONCILE] describe_automation_executions failed for {execution_id}: {exc}")
        children_meta = []

    children: list[dict] = []
    for child in children_meta:
        cid = child.get("AutomationExecutionId")
        if cid and cid not in child_id_set:
            child_id_set.add(cid)
            children.append(child)

    # Source 2: parent's aws:executeAutomation step Outputs. The dispatcher
    # step's Outputs include the spawned child execution IDs even when
    # describe_automation_executions hasn't surfaced them yet.
    for step in parent.get("StepExecutions", []):
        if step.get("Action") != "aws:executeAutomation":
            continue
        outputs = step.get("Outputs") or {}
        for key in ("ExecutionId", "AutomationExecutionId"):
            for cid in outputs.get(key, []) or []:
                if cid and cid not in child_id_set:
                    child_id_set.add(cid)
                    # Synthetic child entry — only the ID is needed since
                    # the loop below fetches full details via get_automation_execution.
                    children.append({"AutomationExecutionId": cid})

    # (account, region) pairs the parent dispatched to. A cross-account child
    # execution lives in one of these accounts, and hub credentials cannot read
    # it, so we need the right spoke client to fetch it at all. Reading the
    # child with the hub client is the bug this list exists to avoid: it raises
    # AutomationExecutionNotFound for every spoke child, which previously meant
    # they were all skipped and the report recorded zero instances.
    parent_locations: list[tuple[str, str]] = []
    for loc in parent.get("TargetLocations") or []:
        for acct in loc.get("Accounts") or []:
            for reg in (loc.get("Regions") or [hub_region]):
                if (acct, reg) not in parent_locations:
                    parent_locations.append((acct, reg))

    def _account_from_executed_by(child_meta: dict) -> str:
        """Pull the owning account out of a child's ExecutedBy ARN.

        AutomationExecutionMetadata carries no account field. ExecutedBy is an
        assumed-role ARN — arn:aws:sts::<account>:assumed-role/... — and for a
        cross-account child that account is the spoke. Note `Target` is a plain
        string (the target resource id), not a structure, so it is no help here.
        """
        arn = child_meta.get("ExecutedBy") or ""
        parts = arn.split(":")
        if len(parts) >= 5 and parts[4].isdigit():
            return parts[4]
        return ""

    def _fetch_child(child_meta: dict, child_id: str):
        """Fetch a child execution, trying each account it could live in.

        Returns (execution, account, region) or (None, None, None). A child
        spawned by TargetLocations lives in the spoke account and is not
        readable with hub credentials at all, so the account has to be right.
        Cheapest correct guess first.
        """
        candidates: list[tuple[str, str]] = []
        regions = [r for _, r in parent_locations] or [hub_region]

        # 1. The account named in the child's own ExecutedBy ARN.
        executed_by = _account_from_executed_by(child_meta)
        if executed_by:
            for reg in regions:
                if (executed_by, reg) not in candidates:
                    candidates.append((executed_by, reg))

        # 2. The hub, for same-account dispatch (AutomationType 'Local').
        if ("", hub_region) not in candidates:
            candidates.append(("", hub_region))

        # 3. Every account/region the parent dispatched to.
        for pair in parent_locations:
            if pair not in candidates:
                candidates.append(pair)

        last_exc = None
        for acct, reg in candidates:
            try:
                client = _ssm_client_for(acct, reg)
                execution = client.get_automation_execution(
                    AutomationExecutionId=child_id
                )["AutomationExecution"]
                return execution, acct, reg
            except Exception as exc:
                last_exc = exc
                continue

        logger.warning(
            f"[RECONCILE] child {child_id} not readable from any of "
            f"{[a or 'hub' for a, _ in candidates]}: {last_exc}"
        )
        return None, None, None

    for child in children:
        child_id = child.get("AutomationExecutionId")
        if not child_id:
            continue

        child_full, child_account, child_region = _fetch_child(child, child_id)
        if child_full is None:
            continue

        # The child's own TargetLocations is the authoritative account/region,
        # when present — prefer it over whichever candidate happened to work.
        target_locs = child_full.get("TargetLocations") or []
        if target_locs:
            accts = target_locs[0].get("Accounts") or []
            regions = target_locs[0].get("Regions") or []
            if accts:
                child_account = accts[0]
            if regions:
                child_region = regions[0]

        try:
            client = _ssm_client_for(child_account, child_region or hub_region)
            _walk_steps(client, child_full.get("StepExecutions", []))
        except Exception as exc:
            logger.warning(
                f"[RECONCILE] failed to walk child {child_id} "
                f"(account={child_account or 'hub'} region={child_region}): {exc}"
            )
            continue

    return {
        "success_count": success,
        "failure_count": failure,
        "instance_count": len(seen_instances),
        "instance_ids": sorted(seen_instances),
    }


def _reconcile_pending_reports(s3, bucket: str, session) -> tuple[int, list[dict]]:
    """Process pending compliance contexts and generate final reports.

    Walks s3://{bucket}/pending-reports/, checks each automation execution status,
    and for completed ones (Success/Failed/Cancelled/TimedOut) generates the final
    compliance report and deletes the pending file.

    Returns: (number of reports generated, list of running operations).

    Idempotent: if a final report already exists for an execution_id, the pending
    file is deleted without writing a duplicate.
    """
    generated_count = 0
    running: list[dict] = []
    try:
        # List pending contexts
        resp = s3.list_objects_v2(Bucket=bucket, Prefix='pending-reports/', MaxKeys=100)
        pending_objects = resp.get('Contents', [])
    except Exception as e:
        logger.warning(f"[RECONCILE] Could not list pending-reports: {e}")
        return 0, []

    if not pending_objects:
        return 0, []

    ssm = session.client('ssm')

    for obj in pending_objects:
        key = obj['Key']
        if not key.endswith('.json'):
            continue
        execution_id = key.replace('pending-reports/', '').replace('.json', '')

        try:
            # Read pending context
            ctx_resp = s3.get_object(Bucket=bucket, Key=key)
            context = json.loads(ctx_resp['Body'].read())

            # Check execution status
            try:
                exec_data = ssm.get_automation_execution(
                    AutomationExecutionId=execution_id
                )['AutomationExecution']
                status = exec_data.get('AutomationExecutionStatus', 'Unknown')
            except Exception as e:
                logger.warning(f"[RECONCILE] Could not get status for {execution_id}: {e}")
                continue

            # Skip if still in progress — collect as running operation
            if status in ('InProgress', 'Pending', 'Waiting'):
                running.append({
                    'execution_id': execution_id,
                    'operation_type': context.get('operation_type', 'patch'),
                    'environment': context.get('environment', 'unknown'),
                    'started_at': context.get('started_at'),
                    'targeting': context.get('targeting'),
                    'instance_count': len(context.get('instance_ids', [])),
                    'status': status,
                })
                continue

            # Check if final report already exists (idempotent)
            started_at_str = context.get('started_at', '')
            try:
                started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            except Exception:
                started_at = datetime.now(timezone.utc)

            date_prefix = f"{started_at.year}/{started_at.month:02d}/{started_at.day:02d}/"
            final_key = f"{date_prefix}{execution_id}.json"

            try:
                s3.head_object(Bucket=bucket, Key=final_key)
                # Already exists — just delete the pending file
                s3.delete_object(Bucket=bucket, Key=key)
                logger.info(f"[RECONCILE] Report already exists for {execution_id}, cleaned up pending")
                continue
            except Exception:
                pass  # Doesn't exist, proceed to generate

            # Build the final compliance report
            ended_at = exec_data.get('ExecutionEndTime')
            duration_seconds = None
            completed_at = None  # remediation completion time, for the SLA calc
            if ended_at and started_at:
                ended = ended_at if isinstance(ended_at, datetime) else datetime.fromisoformat(str(ended_at))
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
                completed_at = ended
                duration_seconds = (ended - started_at).total_seconds()

            # Walk child executions to count actual outcomes. The pending
            # context can't supply these — for tag-based runs SSM only
            # resolves the instance list at execution time. Cross-account
            # children require assume-role into the spoke; the helper
            # handles that.
            outcomes = _count_execution_outcomes(ssm, execution_id, _DEFAULT_REGION)

            # Resolve instance_ids: prefer the live list from SendCommand
            # invocations; fall back to the pending context (Path A only).
            resolved_instance_ids = outcomes.get("instance_ids") or context.get("instance_ids", []) or []

            operation_type = context.get('operation_type', 'patch')

            if operation_type == 'rollback':
                # Rollback reports: only execution + scope + operator. No
                # vulnerability, compliance, or patch_state sections.
                report = {
                    'report_id': execution_id,
                    'timestamp': started_at.isoformat(),
                    'operation_type': 'rollback',
                    'execution': {
                        'execution_id': execution_id,
                        'status': status,
                        'duration_seconds': duration_seconds,
                        'failure_message': exec_data.get('FailureMessage', ''),
                        'success_count': outcomes['success_count'],
                        'failure_count': outcomes['failure_count'],
                    },
                    'scope': {
                        'environment': context.get('environment', 'unknown'),
                        'targeting': context.get('targeting', 'unknown'),
                        'instance_ids': resolved_instance_ids,
                        'instance_count': outcomes['instance_count'] or len(resolved_instance_ids),
                        'account_ids': context.get('account_ids', []),
                        'regions': context.get('regions', []),
                    },
                    'operator': context.get('operator', 'unknown'),
                    'reconciled_at': datetime.now(timezone.utc).isoformat(),
                }
            else:
                # Patch reports (default): full compliance report
                report = {
                    'report_id': execution_id,
                    'timestamp': started_at.isoformat(),
                    'operation_type': 'patch',
                    'execution': {
                        'execution_id': execution_id,
                        'status': status,
                        'decision': context.get('decision', 'EMERGENCY'),
                        'duration_seconds': duration_seconds,
                        'failure_message': exec_data.get('FailureMessage', ''),
                        'success_count': outcomes['success_count'],
                        'failure_count': outcomes['failure_count'],
                    },
                    'vulnerability': {
                        'cve_id': context.get('cve_id') or 'N/A',
                        'severity': context.get('severity') or 'UNKNOWN',
                        'cvss_score': context.get('cvss_score'),
                        'additional_cve_ids': context.get('additional_cve_ids', []) or [],
                    },
                    'scope': {
                        'environment': context.get('environment', 'unknown'),
                        'targeting': context.get('targeting', 'unknown'),
                        'instance_ids': resolved_instance_ids,
                        'instance_count': outcomes['instance_count'] or len(resolved_instance_ids),
                        'account_ids': context.get('account_ids', []),
                        'regions': context.get('regions', []),
                        'severity_filter': context.get('severity_filter'),
                        'team': context.get('team', 'unknown'),
                        'product': context.get('product', 'unknown'),
                    },
                    'compliance': {
                        'sla_hours': context.get('sla_hours'),
                        'sla_source': context.get('sla_source'),
                        'first_observed_at': context.get('first_observed_at'),
                        'sla_met': _evaluate_sla(
                            context.get('sla_hours'),
                            context.get('first_observed_at'),
                            completed_at,
                        ),
                        'frameworks': context.get('frameworks') or [],
                    },
                    'patch_state': {
                        'pre_patch': context.get('pre_patch_state'),
                        'post_patch': context.get('post_patch_state'),
                    },
                    'operator': context.get('operator', 'unknown'),
                    'reconciled_at': datetime.now(timezone.utc).isoformat(),
                }

            # Write final report.
            #
            # S3 object Metadata mirrors the JSON body's key audit fields so
            # query_compliance_reports (compliance analyst tool) can use the
            # cheap head_object path instead of fetching every body. Field
            # names follow the SSM Resource Data Sync convention (kebab-case).
            sla_met_value = _evaluate_sla(
                context.get("sla_hours"),
                context.get("first_observed_at"),
                completed_at,
            )
            frameworks_list = context.get("frameworks") or []
            metadata: dict[str, str] = {
                "cve-id": (context.get("cve_id") or "N/A"),
                "severity": (context.get("severity") or "UNKNOWN"),
                "environment": context.get("environment", "unknown"),
                "decision-type": context.get("decision", "EMERGENCY"),
                "sla-met": sla_met_value,
                # S3 metadata values are flat strings; pack the frameworks
                # list as a comma-separated string for cheap head_object reads.
                "frameworks": ",".join(frameworks_list) if frameworks_list else "",
                "team": context.get("team", "unknown"),
                "product": context.get("product", "unknown"),
                "operator": context.get("operator", "unknown"),
            }
            # S3 metadata values must be ASCII strings; coerce defensively.
            metadata = {k: str(v)[:1024] for k, v in metadata.items() if v is not None}

            s3.put_object(
                Bucket=bucket,
                Key=final_key,
                Body=json.dumps(report, default=str).encode("utf-8"),
                ContentType="application/json",
                Metadata=metadata,
            )

            # Delete pending file
            s3.delete_object(Bucket=bucket, Key=key)
            generated_count += 1
            logger.info(f"[RECONCILE] Generated report for {execution_id} (status={status})")

        except Exception as e:
            logger.warning(f"[RECONCILE] Failed to process pending {key}: {e}")
            continue

    if generated_count > 0:
        logger.info(f"[RECONCILE] Generated {generated_count} compliance report(s)")
    return generated_count, running


def _coerce_utc_dt(value) -> "datetime | None":
    """Parse an ISO-8601 string or datetime into a tz-aware UTC datetime.

    Returns None when the value is missing or unparseable. Naive datetimes are
    assumed to be UTC.
    """
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _evaluate_sla(sla_hours, first_observed_at, remediation_completed_at) -> "bool | None":
    """Was the vulnerability remediated within its SLA window?

    Compliance measures the elapsed time from when the vulnerability was first
    observed (Inspector's firstObservedAt) to when remediation completed,
    against sla_hours. Returns a real bool so downstream `is True`/`is False`
    checks behave, or None when it cannot be determined: no sla_hours, no
    discovery timestamp, no completion time, or timestamps that fail to parse
    or run backwards.

    None must never read as met. The earlier implementation compared the patch
    job's *duration* against the SLA window — unrelated to compliance, and true
    for any fast job. "Unknown" is the honest answer until a discovery
    timestamp (first_observed_at) is captured and forwarded in the context.
    """
    if sla_hours is None:
        return None
    observed = _coerce_utc_dt(first_observed_at)
    completed = _coerce_utc_dt(remediation_completed_at)
    if observed is None or completed is None:
        return None
    elapsed_seconds = (completed - observed).total_seconds()
    if elapsed_seconds < 0:
        return None  # inconsistent timestamps — don't guess
    return elapsed_seconds <= sla_hours * 3600


def _fetch_reports() -> dict:
    """Fetch all S3 compliance reports (last 30 days) in one pass.

    Returns both activity (last 7 days, max 25) and compliance stats (30 days).
    Eliminates per-object head_object calls by reading JSON body in parallel.
    Also runs reconciliation: any pending compliance contexts are processed
    into final reports before fetching.
    """
    account_id = _get_account_id()
    bucket = f"patch-compliance-reports-{account_id}"
    session = _aws_session()
    s3 = session.client("s3")

    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        return {"activities": [], "compliance": None, "report_details": [], "running_operations": []}

    # Reconcile pending contexts before fetching reports
    running_operations = []
    try:
        _, running_operations = _reconcile_pending_reports(s3, bucket, session)
    except Exception as e:
        logger.warning(f"[RECONCILE] Reconciliation failed: {e}")

    # List all report keys for last 30 days (few API calls via month-prefix)
    objects = _list_report_keys(s3, bucket, days=30)
    if not objects:
        return {"activities": [], "compliance": None, "report_details": [], "running_operations": running_operations}

    # Read report JSON bodies in parallel (10 threads)
    reports: list[tuple[dict, dict]] = []  # (s3_obj, parsed_json)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_read_report_json, s3, bucket, obj["Key"]): obj for obj in objects}
        for future in futures:
            obj = futures[future]
            try:
                parsed = future.result(timeout=15)
            except TimeoutError:
                logger.warning(f"[DASHBOARD] Worker timed out reading report {obj.get('Key', 'unknown')}")
                continue
            except Exception as e:
                logger.warning(f"[DASHBOARD] Worker failed reading report {obj.get('Key', 'unknown')}: {e}")
                continue
            if parsed:
                reports.append((obj, parsed))

    # Build activity list (last 7 days, sorted, max 25)
    cutoff_7d = datetime.utcnow() - timedelta(days=7)
    activities = []
    for obj, report in reports:
        last_mod = obj["LastModified"].replace(tzinfo=None)
        if last_mod >= cutoff_7d:
            op_type = report.get("operation_type", "patch")
            vuln = report.get("vulnerability", {})
            scope = report.get("scope", {})
            comp_data = report.get("compliance", {})
            exec_data = report.get("execution", {})

            # Use report body timestamp (execution start time), fall back to S3 write time
            report_ts = report.get("timestamp") or obj["LastModified"].isoformat()

            if op_type == "rollback":
                activities.append({
                    "report_key": obj["Key"],
                    "timestamp": report_ts,
                    "operation_type": "rollback",
                    "cve_id": None,
                    "environment": scope.get("environment", "N/A"),
                    "severity": None,
                    "decision": None,
                    "sla_met": None,
                    "instance_count": scope.get("instance_count", 0),
                    "status": exec_data.get("status", "UNKNOWN"),
                })
            else:
                activities.append({
                    "report_key": obj["Key"],
                    "timestamp": report_ts,
                    "operation_type": "patch",
                    "cve_id": vuln.get("cve_id", "N/A"),
                    "environment": scope.get("environment", "N/A"),
                    "severity": vuln.get("severity", "N/A"),
                    "decision": exec_data.get("decision", "N/A"),
                    "sla_met": comp_data.get("sla_met"),
                })
    activities.sort(key=lambda x: x["timestamp"], reverse=True)

    # Build compliance stats (all 30 days) — only patches count
    total = 0
    sla_met = 0
    sla_breached = 0
    by_severity: dict[str, dict] = {}
    by_environment: dict[str, dict] = {}
    by_team: dict[str, dict] = {}

    # Also build detailed report list for explainability
    report_details = []

    for _, report in reports:
        # Rollback reports don't contribute to compliance stats
        if report.get("operation_type", "patch") == "rollback":
            continue

        vuln = report.get("vulnerability", {})
        scope = report.get("scope", {})
        comp_data = report.get("compliance", {})
        exec_data = report.get("execution", {})
        sla_status = comp_data.get("sla_met")
        severity = vuln.get("severity", "UNKNOWN")
        environment = scope.get("environment", "unknown")
        team = scope.get("team", "unknown")
        total += 1

        if sla_status is True:
            sla_met += 1
        elif sla_status is False:
            sla_breached += 1

        # By severity
        if severity not in by_severity:
            by_severity[severity] = {"total": 0, "met": 0, "breached": 0}
        by_severity[severity]["total"] += 1
        if sla_status is True:
            by_severity[severity]["met"] += 1
        elif sla_status is False:
            by_severity[severity]["breached"] += 1

        # By environment
        if environment not in by_environment:
            by_environment[environment] = {"total": 0, "met": 0, "breached": 0}
        by_environment[environment]["total"] += 1
        if sla_status is True:
            by_environment[environment]["met"] += 1
        elif sla_status is False:
            by_environment[environment]["breached"] += 1

        # By team
        if team not in by_team:
            by_team[team] = {"total": 0, "met": 0, "breached": 0}
        by_team[team]["total"] += 1
        if sla_status is True:
            by_team[team]["met"] += 1
        elif sla_status is False:
            by_team[team]["breached"] += 1

        # Detailed report for explainability
        report_details.append({
            "report_id": report.get("report_id", ""),
            "timestamp": report.get("timestamp", ""),
            "operator": report.get("operator", "unknown"),
            "cve_id": vuln.get("cve_id", "N/A"),
            "severity": severity,
            "cvss_score": vuln.get("cvss_score"),
            "environment": environment,
            "team": team,
            "product": scope.get("product", "unknown"),
            "instance_count": scope.get("instance_count", 0),
            "decision": exec_data.get("decision", "N/A"),
            "sla_hours": comp_data.get("sla_hours"),
            "sla_source": comp_data.get("sla_source", "N/A"),
            "frameworks": comp_data.get("frameworks") or [],
            "sla_met": sla_status,
            "status": exec_data.get("status", "UNKNOWN"),
            "success_count": exec_data.get("success_count", 0),
            "failure_count": exec_data.get("failure_count", 0),
        })

    report_details.sort(key=lambda x: x["timestamp"], reverse=True)

    compliance = None
    if total > 0:
        compliance = {
            "total_reports": total,
            "sla_met": sla_met,
            "sla_breached": sla_breached,
            "sla_rate_percent": round((sla_met / total) * 100, 1),
            "period_days": 30,
            "by_severity": by_severity,
            "by_environment": by_environment,
            "by_team": by_team,
        }

    return {
        "activities": activities[:25],
        "compliance": compliance,
        "report_details": report_details,
        "running_operations": running_operations,
    }


@app.get("/api/dashboard")
async def dashboard(force: bool = False):
    """
    Single endpoint returning all dashboard data.
    Runs 3 concurrent fetches: environments, vulnerabilities, and S3 reports.
    S3 reports are fetched once and split into activity + compliance views.

    Caches results for 30 seconds — returns stale data instantly while
    refreshing in the background on the next request after TTL expires.
    Pass ?force=true to bypass cache (manual refresh button).
    """
    global _dashboard_cache, _dashboard_cache_time
    now = time.time()

    # Return cached data if fresh (< 30s old) and not forced
    if not force and _dashboard_cache and (now - _dashboard_cache_time) < 30:
        return _dashboard_cache

    errors = []

    env_task = asyncio.to_thread(_fetch_environments)
    vuln_task = asyncio.to_thread(_fetch_vulnerabilities)
    reports_task = asyncio.to_thread(_fetch_reports)

    results = await asyncio.gather(env_task, vuln_task, reports_task, return_exceptions=True)

    warnings = []
    explorer_status = {"state": "error", "detail": "fetch failed"}
    if not isinstance(results[0], Exception):
        envs, explorer_status = results[0]
    else:
        envs = []
    vulns = results[1] if not isinstance(results[1], Exception) else {"findings": [], "severity_counts": {}}
    report_data = results[2] if not isinstance(results[2], Exception) else {"activities": [], "compliance": None, "report_details": [], "running_operations": []}

    if isinstance(results[0], Exception):
        logger.error(f"[DASHBOARD] Environments failed: {results[0]}")
        errors.append("Unable to fetch environment data")

    multi_account = os.environ.get("MULTI_ACCOUNT_ENABLED", "").lower() == "true"
    state = explorer_status.get("state", "error")

    if multi_account and state == "missing":
        warnings.append({
            "type": "explorer_sync_missing",
            "title": "Multi-account fleet visibility unavailable",
            "message": (
                "SSM Explorer Resource Data Sync 'patchy-fleet-sync' is not configured. "
                "The dashboard cannot discover instances across spoke accounts. "
                "Re-run ./deploy.sh, or ask your management account administrator to "
                "register this account as an SSM delegated administrator."
            ),
        })
    elif multi_account and state == "empty":
        # Sync exists but hasn't ingested anything yet. Most common right after a deploy
        # that recreated the sync — first ingestion is asynchronous, AWS doesn't publish
        # an SLA. Tell the user it's expected, give them a way to verify.
        warnings.append({
            "type": "explorer_sync_warming_up",
            "title": "Fleet data still warming up",
            "message": (
                "SSM Explorer Resource Data Sync ingestion typically takes a few "
                "minutes to a couple of hours after the sync is created. Verify with: "
                "aws ssm get-ops-summary --sync-name patchy-fleet-sync "
                "--result-attributes TypeName=AWS:EC2InstanceInformation"
            ),
        })
    elif multi_account and state == "error":
        warnings.append({
            "type": "explorer_sync_error",
            "title": "Could not query SSM Explorer",
            "message": (
                "The dashboard tried to query SSM Explorer but the call failed. "
                f"Detail: {explorer_status.get('detail', 'unknown')}. "
                "Check the Patchy-UI Fargate logs for the underlying boto3 exception."
            ),
        })
    if isinstance(results[1], Exception):
        logger.error(f"[DASHBOARD] Vulnerabilities failed: {results[1]}")
        errors.append("Unable to fetch vulnerability data")
    if isinstance(results[2], Exception):
        logger.error(f"[DASHBOARD] Reports failed: {results[2]}")
        errors.append("Unable to fetch compliance reports")

    # Build the scopes list as (account × region) pairs and dedupe defensively.
    # Hub first, then spokes; same region order as SPOKE_REGIONS within each account.
    # The frontend uses scopes for the chip ("N accounts · M regions") and for the
    # account filter dropdown (deduped by accountId in the UI).
    _hub_id = _get_account_id()
    _scope_account_ids: list[str] = [_hub_id]
    for _spoke in _get_dashboard_accounts():
        if _spoke and _spoke not in _scope_account_ids:
            _scope_account_ids.append(_spoke)

    _scope_pairs: list[dict] = []
    _seen_pairs: set[tuple[str, str]] = set()
    for _acct in _scope_account_ids:
        for _rgn in SPOKE_REGIONS:
            key = (_acct, _rgn)
            if key in _seen_pairs:
                continue
            _seen_pairs.add(key)
            _scope_pairs.append({"account_id": _acct, "region": _rgn})

    result = {
        "scopes": _scope_pairs,
        "environments": envs,
        "findings": vulns["findings"],
        "severity_counts": vulns["severity_counts"],
        "activities": report_data["activities"],
        "compliance": report_data["compliance"],
        "report_details": report_data["report_details"],
        "running_operations": report_data.get("running_operations", []),
        "errors": errors,
        "warnings": warnings,
    }

    _dashboard_cache = result
    _dashboard_cache_time = now
    return result


# Server-side dashboard cache (30s TTL)
_dashboard_cache: dict | None = None
_dashboard_cache_time: float = 0.0
_account_id_cache: str | None = None


def _get_account_id() -> str:
    """Get AWS account ID (cached for process lifetime)."""
    global _account_id_cache
    if _account_id_cache:
        return _account_id_cache
    try:
        session = _aws_session()
        sts = session.client("sts")
        _account_id_cache = sts.get_caller_identity()["Account"]
    except Exception:
        _account_id_cache = "unknown"
    return _account_id_cache


# ── Static file serving (production container) ──────────────────────
# When STATIC_DIR is set (Docker), serve the built React frontend.
# In dev mode (no STATIC_DIR), the Vite dev server handles frontend.

_static_dir = os.environ.get("STATIC_DIR")
if _static_dir and Path(_static_dir).is_dir():
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    _static_path = Path(_static_dir)

    # Catch-all route for SPA — serves index.html for any non-API path
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Try to serve the exact file first (JS, CSS, images, etc.)
        file_path = (_static_path / full_path).resolve()
        # Guard against path traversal — resolved path must stay within static dir
        if full_path and file_path.is_file() and str(file_path).startswith(str(_static_path.resolve())):
            return FileResponse(file_path)
        # Fall back to index.html for SPA routing
        return FileResponse(_static_path / "index.html")

    logger.info(f"Serving frontend from {_static_dir}")


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # ECS Fargate task — must bind 0.0.0.0 for the ALB target group health check
    # to reach the container on the private subnet. The container runs in a
    # private subnet behind the ALB; no direct public exposure.
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)  # nosec B104
