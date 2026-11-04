"""
Thin API proxy for Intelligent Patch Automation.
Wraps AgentCore invoke_agent_runtime as HTTP endpoints with SSE streaming.
"""

import asyncio
import codecs
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

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
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
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
    allow_headers=["Content-Type", "Accept", "X-API-Key"],
)


# ── Request validation ──────────────────────────────────────────────

_SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10_000)
    session_id: str | None = Field(None, max_length=100)
    timezone: str | None = Field(None, max_length=50)


# ── Rate limiting (in-memory token bucket) ──────────────────────────

_rate_limits: dict[str, list[float]] = defaultdict(list)
# Requests that invoke the agent (chat and plan approvals), per user per minute.
RATE_LIMIT_PER_MINUTE = 20


def _rate_key(request) -> str:
    """Who a request counts against: the signed-in user, else role and client address.

    Behind the ALB the client address is the load balancer, so keying on it
    alone made every user share one budget.
    """
    email = getattr(request.state, "cognito_email", None)
    if email:
        return email.lower()
    host = request.client.host if request.client else "unknown"
    return f"{getattr(request.state, 'role', 'viewer')}@{host}"


def _check_rate_limit(key: str) -> bool:
    now = time.time()
    # Prune stale entries to prevent unbounded growth
    if len(_rate_limits) > 100:
        stale = [k for k, timestamps in _rate_limits.items() if not timestamps or now - timestamps[-1] > 60]
        for k in stale:
            del _rate_limits[k]
    window = [t for t in _rate_limits[key] if now - t < 60]
    _rate_limits[key] = window
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        return False
    _rate_limits[key].append(now)
    return True


def _rate_limited() -> JSONResponse:
    return JSONResponse(status_code=429, content={
        "error": f"Rate limit exceeded. Max {RATE_LIMIT_PER_MINUTE} requests a minute."})


# ── ALB JWT verification ──────────────────────────────────────────────
# ALB injects x-amzn-oidc-data signed with ES256. Public keys at:
#   https://public-keys.auth.elb.{region}.amazonaws.com/{kid}

_alb_key_cache: dict[str, tuple[str, float]] = {}
_ALB_KEY_CACHE_TTL = 86400  # 24 hours


# A key ID that couldn't be fetched isn't asked for again for this long, so a
# stream of made-up IDs can't hold every request on a 5 s fetch.
_ALB_KEY_MISS_TTL = 300


def _verify_alb_jwt(token: str, region: str) -> dict:
    """Verify the ALB-signed OIDC JWT (ES256) and return its claims.

    Every ALB in a region signs with the same AWS keys, so the signature alone
    doesn't say which ALB sent it: the header's signer must be this ALB
    (ALB_ARN) and its client this app client (COGNITO_CLIENT_ID), as AWS
    recommends.
    """
    import jwt

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("JWT missing kid in header")
    expected_signer = os.environ.get("ALB_ARN")
    if not expected_signer:
        raise ValueError("ALB_ARN is not set, so the token's signer can't be checked")
    if header.get("signer") != expected_signer:
        raise ValueError("token was signed for a different load balancer")
    expected_client = os.environ.get("COGNITO_CLIENT_ID")
    if expected_client and header.get("client") != expected_client:
        raise ValueError("token was issued for a different app client")

    # The kid is interpolated into the public-key URL path. Restrict to the
    # character set ALB uses for key IDs (alphanumeric + hyphen) so a malicious
    # JWT cannot inject path traversal, query strings, or scheme tricks into
    # the URL we fetch.
    if not re.match(r"^[a-zA-Z0-9-]+$", kid):
        raise ValueError(f"JWT kid contains unexpected characters: {kid!r}")

    public_key = _alb_public_key(kid, region)

    claims = jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        options={"verify_aud": False},
    )
    return claims


# The ALB rotates a handful of signing keys; anything beyond this many is junk kids.
_ALB_KEY_CACHE_MAX = 64


def _alb_public_key(kid: str, region: str) -> str:
    """The ALB signing key for a key ID, cached; a miss is remembered briefly. The cache stays small."""
    cached = _alb_key_cache.get(kid)
    if cached and cached[0] is None and (time.time() - cached[1]) < _ALB_KEY_MISS_TTL:
        raise ValueError("unknown key id")
    if cached and cached[0] is not None and (time.time() - cached[1]) < _ALB_KEY_CACHE_TTL:
        return cached[0]
    if len(_alb_key_cache) >= _ALB_KEY_CACHE_MAX:
        for old in sorted(_alb_key_cache, key=lambda k: _alb_key_cache[k][1])[:len(_alb_key_cache) - _ALB_KEY_CACHE_MAX + 1]:
            del _alb_key_cache[old]
    # nosec B310: kid validated by the caller, the hostname is AWS-controlled (HTTPS),
    # and only ALB public keys are returned from this endpoint.
    url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
    try:
        with urlopen(url, timeout=5) as resp:  # nosec B310
            public_key = resp.read().decode("utf-8")
    except Exception:
        _alb_key_cache[kid] = (None, time.time())
        raise
    _alb_key_cache[kid] = (public_key, time.time())
    return public_key


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
# Cognito's "auditors" group adds a read-only view of every operator's plans.
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

# Paths that require operator role (POST to chat = patch operations)
_OPERATOR_PATHS = {"/api/chat"}


def _cognito_configured() -> bool:
    return bool(os.environ.get("COGNITO_USER_POOL_ID"))


def _default_role() -> str:
    """DEFAULT_ROLE, accepted only as exactly operator or viewer; anything else is viewer."""
    value = os.environ.get("DEFAULT_ROLE", "viewer").strip().lower()
    if value in ("operator", "viewer"):
        return value
    logger.warning(f"[AUTH] DEFAULT_ROLE={value!r} is not operator or viewer; using viewer")
    return "viewer"


def _resolve_role(request: Request) -> str | None:
    """Resolve the user's role from the Cognito tokens the ALB adds, an API key, or DEFAULT_ROLE.

    Priority:
    1. Cognito (ALB-injected x-amzn-oidc-data JWT and access token) → cognito:groups.
       Only when Cognito is configured: then the headers are required, and in any
       other mode they are ignored, since only the authenticating ALB can be
       trusted to have set them.
    2. API key (X-API-Key header) → map to role
    3. No auth configured → DEFAULT_ROLE (viewer unless set to operator)
    """
    # 1. Cognito JWT from ALB (highest priority)
    # The ALB injects two relevant headers:
    #   x-amzn-oidc-data        → UserInfo claims (email, sub, etc.) — NO groups
    #   x-amzn-oidc-accesstoken → Access token (cognito:groups lives here)
    oidc_data = request.headers.get("x-amzn-oidc-data") if _cognito_configured() else None
    if _cognito_configured() and not oidc_data:
        return None  # behind the authenticating ALB every request has it
    if oidc_data:
        try:
            # Verify ES256 signature on the ALB-signed OIDC data token
            region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            claims = _verify_alb_jwt(oidc_data, region)
            # Without an email (some federated users), the subject is the identity.
            email = claims.get("email") or claims.get("sub") or "unknown"
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
                # Identity and groups come from two tokens; they must be the same user.
                if at_claims.get("sub") != claims.get("sub"):
                    raise ValueError("access token belongs to a different user than the identity token")
                groups = at_claims.get("cognito:groups", [])
                # Verified, so it can be forwarded to the runtime (RUNTIME_AUTH=jwt).
                request.state.access_token = access_token
                request.state.cognito_username = at_claims.get("username") or at_claims.get("sub")
            elif access_token:
                # Without the pool config the groups claim cannot be verified, and
                # an unverified claim grants nothing — a caller can set it freely.
                # Fall through with no groups, which resolves to viewer.
                logger.error(
                    "[AUTH] COGNITO_USER_POOL_ID/COGNITO_CLIENT_ID are not set, so the "
                    "groups claim cannot be verified. Treating caller as viewer. "
                    "Redeploy the UI stack to populate them."
                )

            logger.debug(f"Cognito auth: email={email}, groups={groups}")
            # Auditors see every operator's plans, read-only (GET /api/changes). It adds to the role:
            # an auditor who isn't an operator is a viewer, and approving stays with a plan's conversation.
            request.state.auditor = "auditors" in groups
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
    return _default_role()


# Same-origin only: no remote scripts, images or frames, so text injected into
# an answer can't send data elsewhere or be framed for clickjacking.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "no-referrer",
}


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

    # Token checks can fetch keys over the network; keep them off the event loop.
    role = await run_in_threadpool(_resolve_role, request)

    if role is None:
        body = {"error": "Unauthorized"}
        if not _cognito_configured() and any([API_KEY, API_KEY_OPERATOR, API_KEY_VIEWER]):
            body["auth"] = "api_key"  # the browser asks the user for a key
        return JSONResponse(status_code=401, content=body)

    if request.method == "POST":
        refused = _cross_site_refusal(request)
        if refused:
            return refused

    # Viewer role: block write operations
    if role == "viewer" and request.url.path in _OPERATOR_PATHS:
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden: viewer role cannot perform patch operations. Contact your admin for operator access."}
        )

    # Attach role to request state for downstream use
    request.state.role = role
    return await call_next(request)


def _cross_site_refusal(request: Request) -> JSONResponse | None:
    """Refuse POSTs another site could send on the user's behalf (CSRF).

    The ALB session cookie goes with any request to this host, so a POST must be
    JSON (a cross-site form or a no-cors fetch can't set that type) and, when the
    browser says where it came from, from this site.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return JSONResponse(status_code=415, content={"error": "Send JSON (Content-Type: application/json)."})
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return JSONResponse(status_code=403, content={"error": "Cross-site request refused."})
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if urllib.parse.urlparse(origin).netloc != host and origin not in _cors_origins:
            return JSONResponse(status_code=403, content={"error": "Cross-site request refused."})
    return None


# Registered after auth so it wraps every response, including 401 and 403.
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response

_local_agent_dir = Path(__file__).parent.parent.parent / "agent"
_docker_agent_dir = Path("/app/agent")
AGENT_DIR = _local_agent_dir if _local_agent_dir.exists() else _docker_agent_dir

_agent_arn = None
_region = None


# When the agent ARN can't be found, don't search again on every health check.
_CONFIG_RETRY_SECONDS = 60
_config_failed_at = 0.0


def _load_config():
    global _agent_arn, _region, _config_failed_at
    if _agent_arn:
        return
    if _config_failed_at and time.time() - _config_failed_at < _CONFIG_RETRY_SECONDS:
        raise FileNotFoundError("No AgentCore config found (looked recently; retrying shortly)")

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
        cfn = _aws_session().client("cloudformation", region_name=_region)
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

    _config_failed_at = time.time()
    raise FileNotFoundError(
        "No AgentCore config found. Set AGENTCORE_AGENT_ARN env var, "
        "or run ./deploy.sh agent to deploy the runtime."
    )


_runtime_client_cache = None
# Each open chat stream holds a connection until it ends, so the shared client
# needs one per concurrent stream.
_RUNTIME_CLIENT_POOL = 20


def _get_client():
    """The bedrock-agentcore client, created once and shared (boto3 clients are thread-safe)."""
    global _runtime_client_cache
    _load_config()
    if _runtime_client_cache is None:
        boto_config = BotoConfig(read_timeout=300, connect_timeout=60, max_pool_connections=_RUNTIME_CLIENT_POOL)
        _runtime_client_cache = _aws_session().client("bedrock-agentcore", config=boto_config)
    return _runtime_client_cache


# ── SSE stream parser ──────────────────────────────────────────────


def _sse_event(event_type: str, **kwargs) -> str:
    """Format a single SSE data line."""
    payload = {"type": event_type, **kwargs}
    return f"data: {json.dumps(payload)}\n\n"


# Seconds of silence before the chat stream sends an SSE comment. The ALB
# closes connections idle for 300 s, and a scan can poll longer than that.
SSE_KEEPALIVE_SECONDS = 15

# How the UI calls the runtime. "iam" (default): SigV4 with the UI task role.
# "jwt": the runtime accepts only Cognito access tokens for the UI's app
# client, so each call carries the signed-in user's token and the runtime
# checks the operators group itself.
RUNTIME_AUTH = os.environ.get("RUNTIME_AUTH", "iam").strip().lower()
# Instance tags recorded on reports; the first one groups the dashboard's SLA results.
REPORT_TAG_KEYS = [k.strip() for k in os.environ.get("REPORT_TAGS", "Team,Product").split(",") if k.strip()][:10]


async def _parse_agentcore_stream(event_stream, session_id: str = ""):
    """Browser SSE events from the runtime stream, with keepalives during silence.

    The runtime stream is read on a thread and handed to this async generator
    through an asyncio queue, so an open chat holds no worker thread. When
    nothing has arrived for SSE_KEEPALIVE_SECONDS it sends an SSE comment
    (": keepalive"), which browsers and the UI's parser ignore.
    """
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue" = asyncio.Queue()
    done = object()
    stopped = threading.Event()

    def put(item):
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # the event loop has shut down
            pass

    def pump():
        try:
            for item in _runtime_events(event_stream, session_id, stopped):
                put(item)
        finally:
            put(done)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is done:
                return
            yield item
    finally:
        # The browser left (or the stream ended): stop reading the runtime. Closing
        # wakes a blocked read with an error, which the reader treats as the end.
        stopped.set()
        try:
            event_stream.close()
        except Exception:
            pass


def _runtime_events(event_stream, session_id: str = "", stopped: "threading.Event | None" = None):
    """
    Generator that reads the raw AgentCore SSE byte stream and yields
    simplified SSE events for the browser.

    Uses read1() on the underlying urllib3 stream for immediate byte delivery.
    """
    current_tool = None
    start_time = time.time()
    line_buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

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

            # Result cards rendered by the UI (typed display blocks from tools)
            card = data.get("card")
            if isinstance(card, dict) and isinstance(card.get("type"), str):
                return _sse_event("card", card=card)

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

            # Incremental: a character split across two reads is kept whole.
            text = decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
            line_buffer += text

            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                result = _process_sse_line(line)
                if result:
                    yield result

    except Exception as e:
        if stopped is not None and stopped.is_set():
            return  # the browser left and the stream was closed under the read
        logger.error(f"Stream parse error: {e}")
        yield _sse_event("error", message="The agent's reply was cut off. Try again.")
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
    if not _check_rate_limit(_rate_key(request)):
        return _rate_limited()

    message = req.message.strip()
    session_id = req.session_id or f"web-{uuid.uuid4().hex}"

    # Validate session_id format
    if req.session_id and not _SESSION_ID_PATTERN.match(req.session_id):
        return JSONResponse(status_code=422, content={"error": "Invalid session_id format"})

    if not message:
        return JSONResponse(status_code=422, content={"error": "message is required"})

    logger.info(f"Chat request: session_id={session_id} role={getattr(request.state, 'role', 'unknown')} "
                f"message={_log_text(message, 80)}")
    role = getattr(request.state, "role", "viewer")
    cognito_email = getattr(request.state, "cognito_email", None)
    operator = cognito_email or f"{role}@{session_id[:12]}"
    payload = json.dumps({"prompt": message, "operator": operator, "timezone": req.timezone or "UTC"}).encode()
    return await _invoke_off_loop(session_id, payload, _access_token(request))


async def _invoke_off_loop(session_id: str, payload: bytes, access_token: str = ""):
    """Invoke the runtime from a worker thread: the call blocks until the first bytes arrive."""
    return await run_in_threadpool(lambda: _invoke_and_stream(_get_client(), session_id, payload, access_token))


def _log_text(text: str, limit: int) -> str:
    """User text for a log line: one line, so a prompt can't forge log entries."""
    return text[:limit].encode("unicode_escape").decode("ascii")


def _access_token(request: Request) -> str:
    return getattr(request.state, "access_token", "") or ""


class _HttpEventStream:
    """An HTTPS response shaped like botocore's StreamingBody, for _runtime_events."""

    def __init__(self, response):
        self._raw_stream = response

    def read(self, n=-1):
        return self._raw_stream.read(n)

    def close(self):
        self._raw_stream.close()


def _invoke_with_token(session_id: str, payload: bytes, access_token: str):
    """Invoke the runtime over HTTPS with the user's Cognito access token (RUNTIME_AUTH=jwt)."""
    region = _agent_arn.split(":")[3]
    url = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
           f"{urllib.parse.quote(_agent_arn, safe='')}/invocations?qualifier=DEFAULT")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    })
    try:
        # The URL is the fixed https AgentCore endpoint for the configured runtime ARN.
        return _HttpEventStream(urllib.request.urlopen(req, timeout=300))  # nosec B310
    except urllib.error.HTTPError as e:
        # Same shape as botocore's message so the mapping below applies.
        detail = e.read(500).decode("utf-8", "replace")
        name = "AccessDeniedException" if e.code in (401, 403) else f"HTTP {e.code}"
        raise RuntimeError(f"{name}: {detail}") from None


def _invoke_and_stream(client, session_id: str, payload: bytes, access_token: str = ""):
    """Invoke the runtime and stream its events to the browser as SSE."""
    if RUNTIME_AUTH == "jwt" and not access_token:
        def no_token():
            yield _sse_event("error", message="Your sign-in has no access token. Sign in again.")
            yield _sse_event("done", duration_ms=0)
        return StreamingResponse(no_token(), media_type="text/event-stream")
    try:
        if RUNTIME_AUTH == "jwt":
            event_stream = _invoke_with_token(session_id, payload, access_token)
        else:
            event_stream = client.invoke_agent_runtime(
                agentRuntimeArn=_agent_arn,
                runtimeSessionId=session_id,
                payload=payload,
            )["response"]
    except Exception as e:
        error_str = str(e)
        logger.error(f"invoke_agent_runtime failed: {error_str}")

        # Provide user-friendly error messages for common failures
        if "ValidationException" in error_str and ("too long" in error_str or "token" in error_str.lower() or "context" in error_str.lower()):
            user_message = "Session context limit reached. Please start a new conversation (click the notepad icon in the top right)."
        elif "ThrottlingException" in error_str or "TooManyRequestsException" in error_str:
            user_message = "Rate limit reached. Please wait a moment and try again."
        elif RUNTIME_AUTH == "jwt" and ("AccessDeniedException" in error_str or "403" in error_str):
            user_message = "The agent runtime didn't accept your sign-in. Refresh the page and sign in again."
        elif "AccessDeniedException" in error_str or "403" in error_str:
            user_message = "Access denied. The agent runtime may still be starting up. Wait 1 to 2 minutes and retry."
        elif "ResourceNotFoundException" in error_str:
            user_message = "Agent runtime not found. It may have been redeployed; refresh the page."
        else:
            user_message = "The agent couldn't answer. Try again in a moment."  # detail is in the log

        def error_stream():
            yield _sse_event("error", message=user_message)
            yield _sse_event("done", duration_ms=0)

        return StreamingResponse(error_stream(), media_type="text/event-stream")

    return StreamingResponse(
        _parse_agentcore_stream(event_stream, session_id=session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================================
# Plan approval
# ============================================================================
# Write tools store plans under plans/ in the compliance bucket. Only this API
# writes plan-decisions/, once per plan (the agent role is denied that prefix);
# the runtime runs a plan only with a matching, fresh approve decision.

_PLAN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# The runtime refuses to claim an approval older than this (APPROVAL_TTL_MIN in agent/helper/plans.py).
APPROVAL_TTL_MIN = 10
# When true, the operator who asked for a plan can't approve it.
REQUIRE_SEPARATE_APPROVER = os.environ.get("REQUIRE_SEPARATE_APPROVER", "false").lower() == "true"


class PlanActionRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
    timezone: str | None = Field(None, max_length=64)


class ApproveRequest(PlanActionRequest):
    # The hash on the card the operator approved; a plan changed since then is refused.
    plan_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")


def _approver_identities(request: Request) -> set:
    """Every name this caller is known by: the email the UI shows, and in JWT mode the
    Cognito username the runtime records as requested_by."""
    return {v for v in (getattr(request.state, "cognito_email", None),
                        getattr(request.state, "cognito_username", None)) if v}


def _plan_hash(tool: str, params: dict, session_id: str, created_at: str) -> str:
    """Same canonical hash as agent/helper/plans.py."""
    canonical = json.dumps({"tool": tool, "params": params, "session_id": session_id,
                            "created_at": created_at}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _plans_s3():
    return _aws_session().client("s3")


def _plans_bucket() -> str:
    return f"patch-compliance-reports-{_get_hub_account_id()}"


def _s3_json(key: str):
    try:
        return json.loads(_plans_s3().get_object(Bucket=_plans_bucket(), Key=key)["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _put_once(key: str, record: dict) -> bool:
    """Write key only if absent; False when it already exists."""
    try:
        _plans_s3().put_object(Bucket=_plans_bucket(), Key=key, Body=json.dumps(record),
                               ContentType="application/json", IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            return False
        raise


def _checked_plan(plan_id: str, req: PlanActionRequest, request: Request):
    """Load a plan the caller may act on, or return an error response."""
    if getattr(request.state, "role", "viewer") != "operator":
        return None, JSONResponse(status_code=403, content={"error": "Only operators can approve or cancel plans."})
    if not _PLAN_ID_PATTERN.match(plan_id):
        return None, JSONResponse(status_code=404, content={"error": "Plan not found."})
    if not _SESSION_ID_PATTERN.match(req.session_id):
        return None, JSONResponse(status_code=422, content={"error": "Invalid session_id format"})
    plan = _s3_json(f"plans/{plan_id}.json")
    if plan is None:
        return None, JSONResponse(status_code=404, content={"error": "Plan not found."})
    if plan.get("session_id") != req.session_id:
        # Same answer as a missing plan: no hint that the ID exists elsewhere.
        return None, JSONResponse(status_code=404, content={"error": "Plan not found."})
    expected = _plan_hash(plan["tool"], plan["params"], plan["session_id"], plan["created_at"])
    if plan.get("plan_hash") != expected:
        logger.warning(f"[PLAN] hash mismatch for {plan_id}")
        return None, JSONResponse(status_code=409, content={"error": "Plan contents changed after it was created."})
    if datetime.now(timezone.utc) > datetime.fromisoformat(plan["expires_at"]):
        return None, JSONResponse(status_code=410, content={"error": "Plan expired. Ask for a fresh plan."})
    return plan, None


@app.post("/api/plans/{plan_id}/approve")
async def approve_plan(plan_id: str, req: ApproveRequest, request: Request):
    """Record an operator's approval of a stored plan and start it (S3 and the runtime off the event loop)."""
    if not _check_rate_limit(_rate_key(request)):
        return _rate_limited()
    return await run_in_threadpool(_approve_plan, plan_id, req, request)


def _approve_plan(plan_id: str, req: ApproveRequest, request: Request):
    """Record an operator's approval of a stored plan and start it.

    Approve and cancel write one decision per plan (plan-decisions/<id>.json,
    put-once), so whichever lands first wins and the other gets 409. Everything
    that could refuse the request is checked before the decision is written.
    """
    if REQUIRE_SEPARATE_APPROVER and not _cognito_configured():
        # API keys and DEFAULT_ROLE give everyone the same identity, so two
        # people can't be told apart; refuse rather than block every approval.
        return JSONResponse(status_code=503, content={
            "error": "REQUIRE_SEPARATE_APPROVER needs Cognito sign-in to tell approvers apart."})
    plan, error = _checked_plan(plan_id, req, request)
    if error:
        return error
    if req.plan_hash != plan["plan_hash"]:
        return JSONResponse(status_code=409, content={"error": "The plan changed after the card was shown. Ask for a fresh plan."})
    access_token = _access_token(request)
    if RUNTIME_AUTH == "jwt" and not access_token:
        return JSONResponse(status_code=401, content={"error": "Your sign-in has no access token. Sign in again."})
    approver = getattr(request.state, "cognito_email", None) or f"operator@{req.session_id[:12]}"
    if REQUIRE_SEPARATE_APPROVER and plan.get("requested_by") in (_approver_identities(request) | {approver}):
        return JSONResponse(status_code=403, content={"error": "The person who requested a plan can't approve it."})
    now = datetime.now(timezone.utc).isoformat()
    decision = {"plan_id": plan_id, "decision": "approved", "plan_hash": plan["plan_hash"],
                "session_id": plan["session_id"], "approver": approver, "approver_role": "operator",
                "approved_at": now, "decided_at": now}
    if not _put_once(f"plan-decisions/{plan_id}.json", decision):
        existing = _s3_json(f"plan-decisions/{plan_id}.json") or {}
        if existing.get("decision") == "cancelled":
            return JSONResponse(status_code=409, content={"error": "Plan was cancelled."})
        # The same approver may start an approved plan again when its run never
        # started (the runtime call failed); the runtime still claims it once.
        if existing.get("approver") != approver or _s3_json(f"plan-runs/{plan_id}.json") is not None:
            return JSONResponse(status_code=409, content={"error": "Plan is already approved."})
        logger.info(f"[PLAN] retrying approved {plan_id} for {approver}")
    else:
        logger.info(f"[PLAN] approved {plan_id} by {approver}")
    payload = json.dumps({"action": "run_plan", "plan_id": plan_id, "operator": approver,
                          "timezone": req.timezone or "UTC"}).encode()
    return _invoke_and_stream(_get_client(), req.session_id, payload, access_token)  # already off the loop


@app.post("/api/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: str, req: PlanActionRequest, request: Request):
    """Cancel a stored plan so it can never be approved."""
    return await run_in_threadpool(_cancel_plan, plan_id, req, request)


def _cancel_plan(plan_id: str, req: PlanActionRequest, request: Request):
    plan, error = _checked_plan(plan_id, req, request)
    if error:
        return error
    who = getattr(request.state, "cognito_email", None) or f"operator@{req.session_id[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    if not _put_once(f"plan-decisions/{plan_id}.json",
                     {"plan_id": plan_id, "decision": "cancelled", "cancelled_by": who, "decided_at": now}):
        existing = _s3_json(f"plan-decisions/{plan_id}.json") or {}
        if existing.get("decision") == "approved":
            return JSONResponse(status_code=409, content={"error": "Plan is already approved."})
        return JSONResponse(status_code=409, content={"error": "Plan is already cancelled."})
    return {"status": "cancelled", "plan_id": plan_id}


def _session_visible(session_id: str, username: str | None) -> bool:
    """JWT mode: a conversation bound to a user (session-owners/) is visible only to that user."""
    if RUNTIME_AUTH != "jwt":
        return True
    owner = (_s3_json(f"session-owners/{session_id}.json") or {}).get("username")
    return owner is None or owner == username


@app.get("/api/plans/{plan_id}")
async def plan_detail(plan_id: str, session_id: str, request: Request):
    """A plan this conversation made, as the change record shows it: its view, decision and runs."""
    detail = await run_in_threadpool(_plan_detail, plan_id, session_id,
                                     getattr(request.state, "cognito_username", None))
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Plan not found."})
    return detail


def _plan_status(plan: dict, decision: dict) -> str:
    if decision.get("decision") in ("approved", "cancelled"):
        return decision["decision"]
    if datetime.now(timezone.utc) > datetime.fromisoformat(plan["expires_at"]):
        return "expired"
    return "awaiting_approval"


def _plan_detail(plan_id: str, session_id: str, username: str | None = None) -> dict | None:
    if not _PLAN_ID_PATTERN.match(plan_id or "") or not _SESSION_ID_PATTERN.match(session_id or ""):
        return None
    plan = _s3_json(f"plans/{plan_id}.json")
    if plan is None or plan.get("session_id") != session_id or not _session_visible(session_id, username):
        return None
    # The related records are independent; read them together.
    with ThreadPoolExecutor(max_workers=3) as pool:
        decision_f, runs_f, claim_f = (pool.submit(_s3_json, f"{prefix}/{plan_id}.json")
                                       for prefix in ("plan-decisions", "plan-executions", "plan-runs"))
        decision, runs, claimed = decision_f.result() or {}, runs_f.result() or {}, claim_f.result() is not None
    status = _plan_status(plan, decision)
    # Until when the runtime would still claim this approval: the plan's expiry or APPROVAL_TTL_MIN after approval.
    approval_expires_at = None
    if decision.get("decision") == "approved" and decision.get("approved_at") and plan.get("expires_at"):
        approval_expires_at = min(datetime.fromisoformat(plan["expires_at"]),
                                  datetime.fromisoformat(decision["approved_at"]) + timedelta(minutes=APPROVAL_TTL_MIN)
                                  ).isoformat()
    return {
        "plan_id": plan_id, "plan_hash": plan.get("plan_hash"), "tool": plan.get("tool"), "status": status,
        "created_at": plan.get("created_at"), "expires_at": plan.get("expires_at"),
        "requested_by": plan.get("requested_by"), "view": plan.get("view"),
        "decision": ({"by": decision.get("approver") or decision.get("cancelled_by"),
                      "at": decision.get("decided_at") or decision.get("approved_at")} if decision else None),
        "execution_ids": list(runs.get("execution_ids") or []),
        # Approved but not claimed: the run never started (calendar closed, stale approval, expiry).
        "claimed": claimed,
        "approval_expires_at": approval_expires_at,
    }


# ── Every operator's plans, for auditors ─────────────────────────────
# Read-only. Each row says what a plan was, who asked, what was decided and
# which runs it started. It leaves out the conversation (session ID) and the
# stored parameters: an auditor can't open the plan's conversation, and
# approval stays with it, since the runtime claims a plan only for the
# session its approval names.

ALL_CHANGES_DAYS = 7
ALL_CHANGES_MAX = 200
_ALL_CHANGES_WORKERS = 10


def _all_changes(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s3, bucket = _plans_s3(), _plans_bucket()
    since = now - timedelta(days=ALL_CHANGES_DAYS)
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="plans/"):
        objs.extend(o for o in page.get("Contents") or []
                    if o["Key"].endswith(".json") and o["LastModified"] >= since)
    objs.sort(key=lambda o: o["LastModified"], reverse=True)
    truncated = len(objs) > ALL_CHANGES_MAX

    def row(key: str) -> dict | None:
        plan_id = key[len("plans/"):-len(".json")]
        if not _PLAN_ID_PATTERN.match(plan_id):
            return None
        plan = _s3_json(key)
        if not plan or not plan.get("expires_at"):
            return None
        decision = _s3_json(f"plan-decisions/{plan_id}.json") or {}
        runs = _s3_json(f"plan-executions/{plan_id}.json") or {}
        view = plan.get("view") or {}
        return {
            "plan_id": plan_id, "headline": view.get("headline") or plan.get("tool"),
            "kind": view.get("kind"), "environment": view.get("environment"),
            "requested_by": plan.get("requested_by"), "status": _plan_status(plan, decision),
            "created_at": plan.get("created_at"),
            "decided_by": decision.get("approver") or decision.get("cancelled_by"),
            "decided_at": decision.get("decided_at") or decision.get("approved_at"),
            "execution_ids": list(runs.get("execution_ids") or []),
        }

    keys = [o["Key"] for o in objs[:ALL_CHANGES_MAX]]
    rows = []
    if keys:
        with ThreadPoolExecutor(max_workers=min(_ALL_CHANGES_WORKERS, len(keys))) as pool:
            rows = [r for r in pool.map(row, keys) if r]
    return {"changes": rows, "days": ALL_CHANGES_DAYS, "truncated": truncated}


@app.get("/api/changes")
async def all_changes(request: Request):
    """Every operator's plans from the last week, read-only. Auditors only."""
    if not getattr(request.state, "auditor", False):
        return JSONResponse(status_code=403, content={"error": "Only auditors can see every operator's plans."})
    return await run_in_threadpool(_all_changes)


# ── Runs: live status and Stop ──────────────────────────────────────
# The runtime records run-executions/<id>.json when an approved plan starts a
# run. The UI shows and stops only runs its own conversation started, and
# stops them through the runtime (stop_run), never with its own credentials.

_EXEC_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
def _run_ssm():
    return _aws_session().client("ssm")


def _own_run(execution_id: str, session_id: str, username: str | None = None) -> dict | None:
    """The run record if this session started the run, else None."""
    if not _EXEC_ID_PATTERN.match(execution_id or "") or not _SESSION_ID_PATTERN.match(session_id or ""):
        return None
    rec = _s3_json(f"run-executions/{execution_id}.json")
    if not rec or rec.get("session_id") != session_id or not _session_visible(session_id, username):
        return None
    return rec


def _run_status(execution_id: str) -> dict | None:
    """Overall and per-(account, region) status of a Patchy run.

    A run that ended in SSM (stopped or failed) while the patch commands it sent are
    still going reads as CommandsFinishing and active, so the run view keeps following
    it; ended_status is how it ended. The reconciler's outcome walk tells, as for the report.
    """
    ssm = _run_ssm()
    summary = run_summary(ssm, execution_id)
    if (summary and not summary.get("active") and summary.get("status") not in ("Success", "CompletedWithSuccess")
            and _ended_within({"ExecutionEndTime": summary.get("ended_at")}, COMMANDS_FINISH_HOURS)):
        still_running = _count_execution_outcomes(ssm, execution_id, _DEFAULT_REGION).get("still_running")
        if still_running:
            summary.update(ended_status=summary["status"], status="CommandsFinishing", active=True,
                           still_running=still_running)
    return summary


@app.get("/api/runs/{execution_id}")
async def run_status(execution_id: str, session_id: str, request: Request):
    """Live status of a run this conversation started."""
    if not await run_in_threadpool(_own_run, execution_id, session_id,
                                   getattr(request.state, "cognito_username", None)):
        return JSONResponse(status_code=404, content={"error": "Run not found."})
    status = await asyncio.to_thread(_run_status, execution_id)
    if status is None:
        return JSONResponse(status_code=404, content={"error": "Run not found."})
    return status


@app.post("/api/runs/{execution_id}/stop")
async def stop_run(execution_id: str, req: PlanActionRequest, request: Request):
    """Ask the runtime to stop a run this conversation started."""
    if getattr(request.state, "role", "viewer") != "operator":
        return JSONResponse(status_code=403, content={"error": "Only operators can stop runs."})
    if not _check_rate_limit(_rate_key(request)):
        return _rate_limited()
    if not await run_in_threadpool(_own_run, execution_id, req.session_id,
                                   getattr(request.state, "cognito_username", None)):
        return JSONResponse(status_code=404, content={"error": "Run not found."})
    who = getattr(request.state, "cognito_email", None) or f"operator@{req.session_id[:12]}"
    payload = json.dumps({"action": "stop_run", "execution_id": execution_id, "operator": who,
                          "timezone": req.timezone or "UTC"}).encode()
    return await _invoke_off_loop(req.session_id, payload, _access_token(request))


@app.get("/api/health")
async def health():
    """Health check — verifies agent config is loadable (off the event loop: it may read files and AWS)."""
    try:
        await run_in_threadpool(_load_config)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[HEALTH] {e}")
        return JSONResponse(status_code=503, content={"status": "error"})


@app.get("/api/role")
async def get_role(request: Request):
    """Return the authenticated user's role. Used by frontend to toggle UI."""
    role = getattr(request.state, "role", "viewer")
    return {"role": role, "auditor": bool(getattr(request.state, "auditor", False))}


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

    # The runtime keys memory by the operator it was given: the email in IAM mode, the access
    # token's username in JWT mode (with email sign-in that's a generated ID, not the email).
    if RUNTIME_AUTH == "jwt":
        operator = getattr(request.state, "cognito_username", None)
    else:
        operator = getattr(request.state, "cognito_email", None)
    if not operator:
        # Without a known operator we can't determine the actor_id.
        # Return 404 so the frontend mints a fresh session.
        return JSONResponse(status_code=404, content={"error": "no_user_context"})

    actor_id = _sanitize_actor_id(operator)

    try:
        client = _get_memory_client()
        if client is None:
            return JSONResponse(status_code=503, content={"error": "memory_client_unavailable"})
        # A paginated Memory read: off the event loop, so it doesn't stall other requests.
        turns = await run_in_threadpool(lambda: client.get_last_k_turns(
            memory_id=_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=50,
            branch_name="main",
        ))
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
    # The configured public URL and app client, not the request's Host header or
    # an unverified token: logout_uri and client_id go into a redirect.
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    signed_out_uri = f"{public_url}/signed-out" if public_url else "/signed-out"

    if cognito_domain and client_id and public_url:
        query = urllib.parse.urlencode({"client_id": client_id, "logout_uri": signed_out_uri})
        cognito_logout = f"https://{cognito_domain}.auth.{region}.amazoncognito.com/logout?{query}"
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
    <html><head><title>Signed out · Patchy</title>
    <meta http-equiv="refresh" content="2;url=/">
    <style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f9fafb}
    .card{text-align:center;padding:3rem;border-radius:12px;background:white;box-shadow:0 1px 3px rgba(0,0,0,.1)}
    a{color:#2563eb;text-decoration:none;font-weight:600}</style></head>
    <body><div class="card"><h2>Signed out</h2><p>You have been signed out of Patchy.</p><p><a href="/">Sign in again</a></p></div>
    </body></html>
    """)


# ── Dashboard ────────────────────────────────────────────────────────

MULTI_ACCOUNT_ENABLED = os.environ.get("MULTI_ACCOUNT_ENABLED", "").lower() == "true"
# The dashboard only reads from spokes, so it assumes the read-only role.
SPOKE_READ_ROLE = os.environ.get("SPOKE_READ_ROLE", "PatchySpokeReadRole")
SPOKE_ACCOUNT_IDS = [a.strip() for a in os.environ.get("SPOKE_ACCOUNT_IDS", "").split(",") if a.strip()]
SPOKE_OU_IDS = [o.strip() for o in os.environ.get("SPOKE_OU_IDS", "").split(",") if o.strip()]
# Regions to query for fleet, vulnerability, and patch data.
# Defaults to the hub region (AWS_REGION) for backward compat.
# Inspector and EC2/SSM are regional APIs — each region needs its own client call.
_DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
SPOKE_REGIONS = [r.strip() for r in os.environ.get("SPOKE_REGIONS", _DEFAULT_REGION).split(",") if r.strip()] or [_DEFAULT_REGION]



from reconcile import (  # noqa: E402  (shared with the report Lambda)
    COMMANDS_FINISH_HOURS, _aws_session, _count_execution_outcomes, _ended_within, _get_hub_account_id,
    _get_spoke_session, _read_report_json, _reconcile_pending_reports, run_summary,
)
def _resolve_ou_member_accounts(ou_ids: list[str]) -> list[str]:
    """Resolve OU IDs to active member account IDs via Organizations.

    Child OUs count, as in the console. Returns None (and logs) on failure, so
    the caller can tell a failed lookup from an OU with no accounts. Includes
    the hub if it's a member of one of the listed OUs — caller will union with
    hub anyway.
    """
    if not ou_ids:
        return []
    try:
        org = _aws_session().client("organizations", region_name="us-east-1")
        accounts: list[str] = []
        pending, seen = list(ou_ids), set()
        while pending:
            ou_id = pending.pop()
            if ou_id in seen:
                continue
            seen.add(ou_id)
            for page in org.get_paginator("list_accounts_for_parent").paginate(ParentId=ou_id):
                accounts.extend(a["Id"] for a in page["Accounts"] if a["Status"] == "ACTIVE")
            for page in org.get_paginator("list_organizational_units_for_parent").paginate(ParentId=ou_id):
                pending.extend(ou["Id"] for ou in page.get("OrganizationalUnits", []))
        return list(dict.fromkeys(accounts))
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not resolve OU members for {ou_ids}: {e}")
        return None


# Cache for the configured account allowlist. OU expansion via Organizations is
# expensive enough to want caching, but a moved account should count within minutes.
_configured_scope_cache: set | None = None
_configured_scope_expires = 0.0
SCOPE_CACHE_SECONDS = 900


_scope_lock = threading.Lock()


def _get_configured_scope_accounts() -> set:
    """The configured account allowlist (see _resolve_configured_scope), resolved by one caller at a time."""
    if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
        return _configured_scope_cache
    with _scope_lock:
        # Another caller may have resolved it while this one waited.
        if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
            return _configured_scope_cache
        return _resolve_configured_scope()


def _resolve_configured_scope() -> set:
    """Return the operator's configured account allowlist for filtering.

    Mirrors the agent's scope resolution (Option C — OUs win):
      1. SPOKE_OU_IDS set → OU members ∪ {hub}
      2. SPOKE_OU_IDS unset, SPOKE_ACCOUNT_IDS set → that list ∪ {hub}
      3. Both unset → empty set sentinel meaning "no filter"

    The empty-set sentinel preserves backward compatibility: callers should
    treat an empty result as "don't filter, include everything the AWS API
    returns" (current behavior for operators who haven't configured scope).

    Cached for SCOPE_CACHE_SECONDS. A failed lookup isn't cached, and gives the
    hub only rather than the empty "no filter" set, so a failure never widens
    what the dashboard shows.
    """
    global _configured_scope_cache, _configured_scope_expires
    if _configured_scope_cache is not None and time.time() < _configured_scope_expires:
        return _configured_scope_cache

    if not MULTI_ACCOUNT_ENABLED:
        try:
            _configured_scope_cache = {_get_hub_account_id()}
        except Exception:
            return set()  # not cached: try again next call
        _configured_scope_expires = time.time() + SCOPE_CACHE_SECONDS
        return _configured_scope_cache

    try:
        hub_id = _get_hub_account_id()
    except Exception:
        hub_id = None

    if SPOKE_OU_IDS:
        members = _resolve_ou_member_accounts(SPOKE_OU_IDS)
        if members is None:
            return {hub_id} if hub_id else set()
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
    _configured_scope_expires = time.time() + SCOPE_CACHE_SECONDS
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
        # The cached allowlist, so the fan-out and the scope filter always agree.
        return sorted(a for a in _get_configured_scope_accounts() if a and a != hub_id)
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
        items, token = [], None
        while True:  # paginated: in an account with many syncs, Patchy's can be on a later page
            resp = ssm.list_resource_data_sync(SyncType="SyncFromSource", **({"NextToken": token} if token else {}))
            items += resp.get("ResourceDataSyncItems", [])
            token = resp.get("NextToken")
            if not token:
                break
        for item in items:
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
        return {"exists": False, "sync_age_minutes": None, "source_regions": [], "error": _error_code(e)}


def _error_code(e: Exception) -> str:
    """The AWS error code of a failed call (AccessDeniedException, ThrottlingException), else the exception type."""
    if isinstance(e, ClientError):
        return e.response.get("Error", {}).get("Code") or type(e).__name__
    return type(e).__name__


def _fetch_environments_via_explorer_with_status() -> tuple[list, dict]:
    """SSM Explorer GetOpsSummary — returns (data, status_dict).

    status_dict.state is one of:
      - 'ok':      Explorer returned data
      - 'empty':   Explorer call succeeded but returned no entities (sync warming up,
                   or no scoped instances anywhere)
      - 'missing': Sync 'patchy-fleet-sync' does not exist
      - 'error':   API call raised (permissions, throttling, etc.)
    """
    unreadable: list[dict] = []
    errors: list[str] = []
    data = _fetch_environments_via_explorer(unreadable, errors)
    if data is not None and len(data) > 0:
        return data, {"state": "ok", "sync_age_minutes": None, "detail": "explorer returned data",
                      "unreadable": unreadable}
    if errors:
        return [], {"state": "error", "sync_age_minutes": None, "code": errors[0],
                    "detail": f"GetOpsSummary failed ({errors[0]})"}

    # No data — figure out why before falling back to fan-out
    sync_info = _check_explorer_sync_status()
    if sync_info.get("error"):
        return [], {"state": "error", "sync_age_minutes": None, "code": sync_info["error"],
                    "detail": f"ListResourceDataSync failed ({sync_info['error']})"}
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
        "unreadable": unreadable,
    }


def _fetch_environments_via_explorer(unreadable: list | None = None, errors: list | None = None) -> list | None:
    """SSM Explorer GetOpsSummary with Resource Data Sync — single call, cross-account.

    Locations whose instances couldn't be read are appended to `unreadable`; the error
    code of a failed Explorer read is appended to `errors`.
    """
    try:
        session = _aws_session()
        region = os.environ.get("AWS_REGION", _region or "us-east-1")
        raw_instances = _explorer_instances(session.client("ssm", region_name=region), region)
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

        # Scope-tagged instances EC2 reports that Explorer hasn't ingested yet: the agent's
        # plans and a tag-based run include them, so the dashboard counts them too (2 Nov:
        # after the sample fleet was rebuilt, Overview said 3 instances while plans covered 13).
        raw_instances.update(_ec2_only_instances(session, raw_instances))
        instances = _enrich_instances(session, raw_instances, unreadable)
        if not instances:
            return None
        envs = _environment_summaries(instances)
        logger.info(f"[DASHBOARD] Explorer: {len(instances)} instances, {len(envs)} environments")
        return envs

    except Exception as e:
        logger.warning(f"[DASHBOARD] Explorer unavailable, falling back to fan-out: {e}")
        if errors is not None:
            errors.append(_error_code(e))
        return None


def _explorer_instances(ssm, region: str) -> dict[str, dict]:
    """Every instance in the patchy-fleet-sync Resource Data Sync: account, region, managed."""
    raw: dict[str, dict] = {}
    next_token = None
    while True:
        kwargs: dict = {
            "SyncName": "patchy-fleet-sync",
            "ResultAttributes": [{"TypeName": "AWS:EC2InstanceInformation"}],
            # Only instance records; unfiltered, Explorer returns mostly empty pages.
            "Filters": [{"Key": "AWS:EC2InstanceInformation.InstanceId", "Values": ["i-"], "Type": "BeginWith"}],
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
            raw[iid] = {
                "account_id": content.get("SourceAccountId", ""),
                "region": content.get("SourceRegion", region),
                "online": content.get("IsManaged") == "true",
            }
        next_token = resp.get("NextToken")
        if not next_token:
            return raw


def _ec2_only_instances(session, known: dict[str, dict]) -> dict[str, dict]:
    """Running scope-tagged instances in each configured account and Region that Explorer doesn't list yet.

    Same shape as an Explorer record ({account_id, region, online}); online comes from
    Systems Manager in that location. A location that can't be read adds nothing.
    """
    scope_tag_key = os.environ.get("SSM_SCOPE_TAG_KEY", "PatchAutomation")
    scope_tag_value = os.environ.get("SSM_SCOPE_TAG_VALUE", "enabled")
    hub_id = _get_hub_account_id()
    locations = [(a, r) for a in [hub_id] + [x for x in _get_dashboard_accounts() if x != hub_id] for r in SPOKE_REGIONS]

    def read(location):
        account_id, rgn = location
        found: dict[str, dict] = {}
        try:
            source = session if account_id == hub_id else _get_spoke_session(account_id)
            ec2 = source.client("ec2", region_name=rgn)
            for page in ec2.get_paginator("describe_instances").paginate(Filters=[
                    {"Name": f"tag:{scope_tag_key}", "Values": [scope_tag_value]},
                    {"Name": "instance-state-name", "Values": ["pending", "running"]}]):
                for res in page.get("Reservations", []):
                    for inst in res.get("Instances", []):
                        if inst["InstanceId"] not in known:
                            found[inst["InstanceId"]] = {"account_id": account_id, "region": rgn, "online": False}
            if found:
                ssm = source.client("ssm", region_name=rgn)
                ids = list(found)
                for i in range(0, len(ids), 50):
                    for info in ssm.describe_instance_information(
                            Filters=[{"Key": "InstanceIds", "Values": ids[i:i + 50]}]).get("InstanceInformationList", []):
                        if info.get("InstanceId") in found:
                            found[info["InstanceId"]]["online"] = info.get("PingStatus") == "Online"
        except Exception as e:
            logger.info(f"[DASHBOARD] EC2 lookup for instances not in Explorer failed for {account_id}/{rgn}: {e}")
        return found

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(locations) or 1)) as pool:
        for found in pool.map(read, locations):
            out.update(found)
    if out:
        logger.info(f"[DASHBOARD] {len(out)} instance(s) not in Explorer yet, from EC2")
    return out


def _enrich_instances(session, raw_instances: dict[str, dict], unreadable: list | None = None) -> dict[str, dict]:
    """Scoped instances with Environment tag and patch counts, read per (account, region).

    Bucketing by account alone caused cross-region instances to inherit the
    first instance's region for the EC2/SSM lookups, silently dropping any
    instance in a different region, so each (account, region) gets its own
    clients.
    """
    scope_tag_key = os.environ.get("SSM_SCOPE_TAG_KEY", "PatchAutomation")
    scope_tag_value = os.environ.get("SSM_SCOPE_TAG_VALUE", "enabled")
    hub_id = _get_hub_account_id()
    by_location: dict[tuple, list] = {}
    for iid, inst in raw_instances.items():
        by_location.setdefault((inst["account_id"], inst["region"]), []).append(iid)

    instances: dict[str, dict] = {}
    for (account_id, rgn), iids in by_location.items():
        tags_map: dict[str, dict] = {}
        patch_map: dict[str, dict] = {}
        source = session
        try:
            source = _get_spoke_session(account_id) if account_id != hub_id else session
            ec2 = source.client("ec2", region_name=rgn)
            # instance-id filter, not InstanceIds: Explorer still lists terminated
            # instances for a while, and one unknown ID fails an InstanceIds call.
            for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "instance-id", "Values": iids}]):
                for res in page.get("Reservations", []):
                    for inst in res.get("Instances", []):
                        # EC2 lists a terminated instance, tags and all, for about an hour.
                        if (inst.get("State") or {}).get("Name") in ("shutting-down", "terminated"):
                            continue
                        tags_map[inst["InstanceId"]] = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        except Exception as e:
            logger.warning(f"[DASHBOARD] Tag lookup failed for {account_id}/{rgn}: {e}")
            # Without tags these instances can't be placed or counted; say so
            # instead of leaving them out of every total.
            if unreadable is not None:
                unreadable.append({"account_id": account_id, "region": rgn, "instances": len(iids),
                                   "error": str(e)[:200]})
            continue
        try:
            ssm = source.client("ssm", region_name=rgn)
            for i in range(0, len(iids), 50):
                resp = ssm.describe_instance_patch_states(InstanceIds=iids[i:i + 50])
                for st in resp.get("InstancePatchStates", []):
                    patch_map[st["InstanceId"]] = {"missing": st.get("MissingCount", 0),
                                                   "installed": st.get("InstalledCount", 0),
                                                   "failed": st.get("FailedCount", 0)}
        except Exception as e:
            logger.warning(f"[DASHBOARD] Patch state lookup failed for {account_id}/{rgn}: {e}")
        # Online is SSM's PingStatus, as for instances EC2 lists that Explorer doesn't; Explorer's
        # IsManaged only says the instance is registered. If it can't be read, IsManaged stands.
        ping: dict[str, str] | None = {}
        try:
            for i in range(0, len(iids), 50):
                kwargs: dict = {"Filters": [{"Key": "InstanceIds", "Values": iids[i:i + 50]}]}
                while True:
                    resp = ssm.describe_instance_information(**kwargs)
                    for info in resp.get("InstanceInformationList", []):
                        ping[info.get("InstanceId")] = info.get("PingStatus")
                    token = resp.get("NextToken")
                    if not token or token == kwargs.get("NextToken"):  # a repeated token would loop for ever
                        break
                    kwargs["NextToken"] = token
        except Exception as e:
            logger.warning(f"[DASHBOARD] Ping status lookup failed for {account_id}/{rgn}: {e}")
            ping = None

        for iid in iids:
            tags = tags_map.get(iid, {})
            if tags.get(scope_tag_key) != scope_tag_value:
                continue
            patch = patch_map.get(iid, {})
            instances[iid] = {
                "account_id": account_id,
                "region": rgn,
                "environment": tags.get("Environment", "unknown"),
                "online": raw_instances[iid]["online"] if ping is None else ping.get(iid) == "Online",
                "missing": int(patch.get("missing", 0)),
                "installed": int(patch.get("installed", 0)),
                "failed": int(patch.get("failed", 0)),
                "scanned": iid in patch_map,
            }
    return instances


def _empty_patch_compliance() -> dict:
    return {"compliant_instances": 0, "scanned_instances": 0, "compliance_pct": None,
            "missing_patches": 0, "installed_patches": 0, "failed_patches": 0}


def _add_patch_counts(pc: dict, inst: dict) -> None:
    """Add one instance's patch counts; it counts as scanned when it has a patch state, as in the fallback path."""
    pc["missing_patches"] += inst["missing"]
    pc["installed_patches"] += inst["installed"]
    pc["failed_patches"] += inst["failed"]
    if inst.get("scanned", inst["installed"] > 0):
        pc["scanned_instances"] += 1
        if inst["missing"] == 0:
            pc["compliant_instances"] += 1


def _compliance_pct(pc: dict) -> int | None:
    scanned = pc["scanned_instances"]
    return round(pc["compliant_instances"] / scanned * 100) if scanned > 0 else None


def _environment_summaries(instances: dict[str, dict]) -> list[dict]:
    """Per-environment counts, status and patch compliance, with a breakdown per account."""
    envs: dict[str, dict] = {}
    for inst in instances.values():
        env = inst["environment"]
        e = envs.setdefault(env, {"environment": env, "total": 0, "online": 0, "offline": 0,
                                  "unmanaged_count": 0, "status": "inactive",
                                  "accounts": set(), "per_account": {},
                                  "patch_compliance": _empty_patch_compliance()})
        e["total"] += 1
        e["online" if inst["online"] else "offline"] += 1
        _add_patch_counts(e["patch_compliance"], inst)
        acct = inst["account_id"]
        e["accounts"].add(acct)
        pa = e["per_account"].setdefault(acct, {"total": 0, "online": 0, "offline": 0,
                                                "patch_compliance": _empty_patch_compliance()})
        pa["total"] += 1
        pa["online" if inst["online"] else "offline"] += 1
        _add_patch_counts(pa["patch_compliance"], inst)

    for e in envs.values():
        e["patch_compliance"]["compliance_pct"] = _compliance_pct(e["patch_compliance"])
        e["status"] = ("healthy" if e["online"] == e["total"] and e["total"] > 0
                       else "warning" if e["online"] > 0
                       else "error" if e["total"] > 0 else "inactive")
        e["accounts"] = sorted(e["accounts"])
        for pa in e["per_account"].values():
            pa["patch_compliance"]["compliance_pct"] = _compliance_pct(pa["patch_compliance"])
    return sorted(envs.values(), key=lambda x: x["environment"])


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
                    pc_e["compliance_pct"] = _compliance_pct(pc_e)

            # Env-level patch_compliance: sum and recompute pct
            if env.get("patch_compliance") and m.get("patch_compliance"):
                for k in ("compliant_instances", "scanned_instances", "missing_patches", "installed_patches", "failed_patches"):
                    m["patch_compliance"][k] = m["patch_compliance"].get(k, 0) + env["patch_compliance"].get(k, 0)
                m["patch_compliance"]["compliance_pct"] = _compliance_pct(m["patch_compliance"])
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
        # The InstanceIds filter takes at most 50 values per call.
        for n in range(0, len(all_ids), 50):
            for page in ssm_paginator.paginate(Filters=[{"Key": "InstanceIds", "Values": all_ids[n:n + 50]}]):
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
    unread_regions: list[dict] = []
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
                unread_regions.append({"region": rgn, "error": "timed out"})
            except Exception as e:
                logger.warning(f"[DASHBOARD] Vuln fetch failed for {acct}/{rgn}: {e}")
                unread_regions.append({"region": rgn, "error": _error_code(e)})

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFORMATIONAL": 4, "UNTRIAGED": 5, "UNKNOWN": 6}
    findings = sorted(all_findings.values(), key=lambda x: (sev_order.get(x["severity"], 9), -(x["cvss_score"] or 0)))
    severity_counts: dict[str, int] = {}
    for cve in findings:
        severity_counts[cve["severity"]] = severity_counts.get(cve["severity"], 0) + 1
    unread_regions.sort(key=lambda u: SPOKE_REGIONS.index(u["region"]) if u["region"] in SPOKE_REGIONS else 99)
    return {"findings": findings, "severity_counts": severity_counts, "unread_regions": unread_regions}


def _findings_summary(findings: list[dict]) -> dict[str, dict]:
    """Per severity: distinct finding IDs, affected instances, and IDs with a fix available."""
    out: dict[str, dict] = {}
    for f in findings:
        row = out.setdefault(f["severity"], {"ids": 0, "instances": 0, "fixable_ids": 0})
        row["ids"] += 1
        row["instances"] += f.get("instance_count", 0)
        if f.get("fix_available") == "YES":
            row["fixable_ids"] += 1
    return out


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


def _list_report_keys(s3, bucket: str, days: int, unread: list | None = None) -> list[dict]:
    """List all S3 report objects for the last N days in a single pass.

    Uses year/month prefix to minimize list_objects_v2 calls — e.g. 30 days
    spanning 2 months = 2 API calls instead of 30. A month that can't be listed
    is appended to `unread` as (prefix, error code).
    """
    now = datetime.now(timezone.utc)
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
                    if obj["LastModified"] >= cutoff:
                        objects.append(obj)
        except ClientError as e:
            logger.warning(f"[DASHBOARD] Could not list reports under {prefix}: {e}")
            if unread is not None:
                unread.append((prefix, _error_code(e)))
            continue
    return objects


# The dashboard reconciles pending reports (or, with the Lambda, lists running ones) at most once a minute.
_RECONCILE_EVERY_SECONDS = 60
_last_reconcile: tuple = (0.0, [])
_last_reconcile_error: list[str] = [""]
_reconcile_lock = threading.Lock()


def _running_operations(s3, bucket: str, session) -> list[dict] | None:
    """Runs in progress, from the pending reports; reconciling them when the Lambda isn't deployed.

    None when the pending reports couldn't be read (the error code is kept in _last_reconcile_error).
    """
    global _last_reconcile
    with _reconcile_lock:
        at, running = _last_reconcile
        if time.time() - at < _RECONCILE_EVERY_SECONDS:
            return running
        try:
            # With the report Lambda deployed it writes the reports; the dashboard only lists what's running.
            _, running = _reconcile_pending_reports(
                s3, bucket, session, write=os.environ.get("REPORT_LAMBDA_ENABLED", "").lower() != "true")
        except Exception as e:
            logger.warning(f"[RECONCILE] Reconciliation failed: {e}")
            running = None
            _last_reconcile_error[0] = _error_code(e)
        _last_reconcile = (time.time(), running)
        return running


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

    unread: list[dict] = []
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not read the reports bucket {bucket}: {e}")
        unread.append({"type": "reports_unreadable",
                       "message": f"Reports couldn't be read from {bucket} ({_error_code(e)})."})
        return {"activities": [], "compliance": None, "report_details": [], "running_operations": [], "unread": unread}

    running_operations = _running_operations(s3, bucket, session)
    if running_operations is None:
        unread.append({"type": "running_unreadable",
                       "message": f"Runs in progress couldn't be read ({_last_reconcile_error[0]})."})
        running_operations = []

    # List all report keys for last 30 days (few API calls via month-prefix)
    unread_months: list[tuple[str, str]] = []
    objects = _list_report_keys(s3, bucket, days=30, unread=unread_months)
    if unread_months:
        months = ", ".join(f"{prefix.rstrip('/')} ({code})" for prefix, code in unread_months)
        unread.append({"type": "reports_unreadable", "message": f"Reports for {months} couldn't be listed."})
    if not objects:
        return {"activities": [], "compliance": None, "report_details": [], "running_operations": running_operations,
                "unread": unread}

    reports = _read_reports(s3, bucket, objects)
    compliance, report_details = _compliance_and_details(reports)
    return {
        "activities": _activities(reports)[:25],
        "compliance": compliance,
        "report_details": report_details,
        "runs": _runs(reports),
        "running_operations": running_operations,
        "unread": unread,
    }


# Reading report bodies for the dashboard: workers, and how long one rebuild may wait for them.
_REPORT_READ_WORKERS = 10
_REPORT_READ_SECONDS = 30
# Parsed reports by S3 key: (ETag, the fields the dashboard uses). Reports are create-only,
# so a rebuild reads only new or changed ones; keys no longer listed are dropped.
_REPORT_FIELDS = ("report_id", "timestamp", "operation_type", "operator", "scope", "execution",
                  "compliance", "vulnerability", "install_packages")
_report_cache: dict[str, tuple[str, dict]] = {}
_report_cache_lock = threading.Lock()


def _read_reports(s3, bucket: str, objects: list[dict]) -> list[tuple[dict, dict]]:
    """(S3 object, parsed report) for each object, in listing order, reading only what the cache lacks."""
    with _report_cache_lock:
        cached = dict(_report_cache)
    missing = [o for o in objects if cached.get(o["Key"], (None,))[0] != o.get("ETag")]
    fresh: dict[str, tuple[str, dict]] = {}
    if missing:
        pool = ThreadPoolExecutor(max_workers=_REPORT_READ_WORKERS)
        futures = {pool.submit(_read_report_json, s3, bucket, o["Key"]): o for o in missing}
        try:
            for future in as_completed(futures, timeout=_REPORT_READ_SECONDS):
                obj = futures[future]
                try:
                    parsed = future.result()
                except Exception as e:
                    logger.warning(f"[DASHBOARD] Worker failed reading report {obj.get('Key', 'unknown')}: {e}")
                    continue
                if parsed:
                    fresh[obj["Key"]] = (obj.get("ETag"), {k: parsed[k] for k in _REPORT_FIELDS if k in parsed})
        except TimeoutError:
            logger.warning(f"[DASHBOARD] Reading reports took over {_REPORT_READ_SECONDS}s; showing what was read")
        finally:
            # Don't wait on hung reads; the next rebuild picks up whatever is still missing.
            pool.shutdown(wait=False, cancel_futures=True)
    listed = {o["Key"] for o in objects}
    with _report_cache_lock:
        _report_cache.update(fresh)
        for key in [k for k in _report_cache if k not in listed]:
            del _report_cache[key]
        current = dict(_report_cache)
    return [(o, current[o["Key"]][1]) for o in objects if o["Key"] in current]


def _activities(reports: list[tuple[dict, dict]]) -> list[dict]:
    """Patch and rollback activity from the last 7 days, newest first."""
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    activities = []
    for obj, report in reports:
        if obj["LastModified"] < cutoff_7d:
            continue
        scope, exec_data = report.get("scope", {}), report.get("execution", {})
        # Report body timestamp (execution start), else the S3 write time.
        report_ts = report.get("timestamp") or obj["LastModified"].isoformat()
        if report.get("operation_type", "patch") == "rollback":
            activities.append({
                "report_key": obj["Key"], "timestamp": report_ts, "operation_type": "rollback",
                "cve_id": None, "environment": scope.get("environment", "N/A"), "severity": None,
                "decision": None, "sla_met": None, "instance_count": scope.get("instance_count", 0),
                "status": exec_data.get("status", "UNKNOWN"),
            })
        else:
            vuln = report.get("vulnerability", {})
            activities.append({
                "report_key": obj["Key"], "timestamp": report_ts, "operation_type": "patch",
                "cve_id": vuln.get("cve_id", "N/A"), "environment": scope.get("environment", "N/A"),
                "severity": vuln.get("severity", "N/A"), "decision": exec_data.get("decision", "N/A"),
                "sla_met": report.get("compliance", {}).get("sla_met"),
            })
    activities.sort(key=lambda x: _time_key(x["timestamp"]), reverse=True)
    return activities


def _runs(reports: list[tuple[dict, dict]]) -> list[dict]:
    """One row per patch or rollback run in the reports read, newest first."""
    rows = []
    for _, report in reports:
        exec_data, scope = report.get("execution", {}), report.get("scope", {})
        comp, vuln = report.get("compliance", {}), report.get("vulnerability", {})
        patch = report.get("operation_type", "patch") != "rollback"
        rows.append({
            "report_id": report.get("report_id", ""),
            "timestamp": report.get("timestamp", ""),
            "operation_type": "patch" if patch else "rollback",
            "execution_id": exec_data.get("execution_id"),
            "plan_id": exec_data.get("plan_id"),
            "operator": report.get("operator", "unknown"),
            "environment": scope.get("environment", "unknown"),
            "tags": _report_tags_of(scope),
            "tag_values": dict(scope.get("tag_values") or {}),
            "instance_count": scope.get("instance_count", 0),
            "cve_id": vuln.get("cve_id") if patch else None,
            "severity": vuln.get("severity") if patch else None,
            "decision": exec_data.get("decision") if patch else None,
            "decision_reason": exec_data.get("decision_reason") if patch else None,
            "status": exec_data.get("status", "UNKNOWN"),
            "success_count": exec_data.get("success_count", 0),
            "failure_count": exec_data.get("failure_count", 0),
            "failure_message": exec_data.get("failure_message") or "",
            "duration_seconds": exec_data.get("duration_seconds"),
            "sla_met": comp.get("sla_met") if patch else None,
            "sla_hours": comp.get("sla_hours") if patch else None,
            "sla_source": comp.get("sla_source") if patch else None,
            "sla_reason": comp.get("sla_reason") if patch else None,
            # For the SLA arithmetic in Reports: the deadline is first_observed_at + sla_hours.
            "first_observed_at": comp.get("first_observed_at") if patch else None,
            # Set on a severity run (many findings, no single discovery time).
            "severity_filter": scope.get("severity_filter") if patch else None,
            "frameworks": (comp.get("frameworks") or []) if patch else [],
            "install_packages": report.get("install_packages") or [],
        })
    rows.sort(key=lambda r: _time_key(r["timestamp"]), reverse=True)
    return rows


def _tally(totals: dict[str, dict], key: str, sla_status) -> None:
    row = totals.setdefault(key, {"total": 0, "met": 0, "breached": 0})
    row["total"] += 1
    if sla_status is True:
        row["met"] += 1
    elif sla_status is False:
        row["breached"] += 1


def _report_tags_of(scope: dict) -> dict:
    """A report's tags; a report written before REPORT_TAGS carries a team field instead."""
    if scope.get("tags") is not None:
        return dict(scope["tags"])
    legacy = {k.capitalize(): scope[k] for k in ("team", "product") if scope.get(k) and scope[k] != "unknown"}
    return legacy


def _compliance_and_details(reports: list[tuple[dict, dict]]) -> tuple[dict | None, list[dict]]:
    """SLA statistics over patch reports (rollbacks don't count), and one detail row per report."""
    overall: dict[str, dict] = {}
    by_severity: dict[str, dict] = {}
    by_environment: dict[str, dict] = {}
    by_tag: dict[str, dict] = {}
    group_key = REPORT_TAG_KEYS[0] if REPORT_TAG_KEYS else None
    details = []
    for _, report in reports:
        if report.get("operation_type", "patch") == "rollback":
            continue
        vuln, scope = report.get("vulnerability", {}), report.get("scope", {})
        comp_data, exec_data = report.get("compliance", {}), report.get("execution", {})
        sla_status = comp_data.get("sla_met")
        severity = vuln.get("severity", "UNKNOWN")
        environment = scope.get("environment", "unknown")
        tags = _report_tags_of(scope)
        _tally(overall, "all", sla_status)
        _tally(by_severity, severity, sla_status)
        _tally(by_environment, environment, sla_status)
        if group_key:
            _tally(by_tag, tags.get(group_key) or "unknown", sla_status)
        details.append({
            "report_id": report.get("report_id", ""),
            "timestamp": report.get("timestamp", ""),
            "operator": report.get("operator", "unknown"),
            "cve_id": vuln.get("cve_id", "N/A"),
            "severity": severity,
            "cvss_score": vuln.get("cvss_score"),
            "environment": environment,
            "tags": tags,
            "instance_count": scope.get("instance_count", 0),
            "decision": exec_data.get("decision", "N/A"),
            "decision_reason": exec_data.get("decision_reason"),
            "sla_hours": comp_data.get("sla_hours"),
            "sla_source": comp_data.get("sla_source", "N/A"),
            "frameworks": comp_data.get("frameworks") or [],
            "sla_met": sla_status,
            "status": exec_data.get("status", "UNKNOWN"),
            "success_count": exec_data.get("success_count", 0),
            "failure_count": exec_data.get("failure_count", 0),
        })
    details.sort(key=lambda x: _time_key(x["timestamp"]), reverse=True)
    totals = overall.get("all")
    if not totals:
        return None, details
    known = totals["met"] + totals["breached"]
    return {
        "total_reports": totals["total"],
        "sla_met": totals["met"],
        "sla_breached": totals["breached"],
        # Runs with no discovery time have no SLA result; they don't count either way.
        "sla_unknown": totals["total"] - known,
        "sla_rate_percent": round(totals["met"] / known * 100, 1) if known else None,
        "period_days": 30,
        "by_severity": by_severity,
        "by_environment": by_environment,
        # SLA results by the first REPORT_TAGS tag (Team by default).
        "by_tag": {"key": group_key, "groups": by_tag},
    }, details


# Forced refreshes (the Refresh button) per user per minute; each one fans out
# across every account and region.
FORCE_REFRESH_PER_MINUTE = 4
_force_refreshes: dict[str, list[float]] = defaultdict(list)
# One rebuild at a time: concurrent requests wait for it and share the result.
# One rebuild at a time. Waiters poll for the lock asynchronously, so they hold no worker
# thread, and it works from any event loop (an asyncio.Lock is bound to one).
_dashboard_build_lock = threading.Lock()


def _force_allowed(key: str) -> bool:
    now = time.time()
    recent = [t for t in _force_refreshes[key] if now - t < 60]
    _force_refreshes[key] = recent
    if len(recent) >= FORCE_REFRESH_PER_MINUTE:
        return False
    recent.append(now)
    return True


@app.get("/api/dashboard")
async def dashboard(request: Request, force: bool = False):
    """
    Single endpoint returning all dashboard data.
    Runs 3 concurrent fetches: environments, vulnerabilities, and S3 reports.
    S3 reports are fetched once and split into activity + compliance views.

    Results are cached for DASHBOARD_CACHE_TTL seconds and shared by every
    viewer. Pass ?force=true to bypass the cache (the Refresh button); that is
    limited per user, and only one rebuild runs at a time.
    """
    limited = False
    if force and not _force_allowed(_rate_key(request)):
        force, limited = False, True
    cached = _cached_dashboard(time.time(), force)
    if cached is not None:
        return {**cached, "refresh_limited": True} if limited else cached

    built_before = _dashboard_cache_time
    while not _dashboard_build_lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        # Another request may have rebuilt while this one waited.
        if _dashboard_cache is not None and _dashboard_cache_time != built_before:
            return _dashboard_cache
        return await _build_dashboard()
    finally:
        _dashboard_build_lock.release()


async def _build_dashboard() -> dict:
    global _dashboard_cache, _dashboard_cache_time
    now = time.time()
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
            "code": explorer_status.get("code"),
            "message": (
                "The dashboard tried to query SSM Explorer but the call failed. "
                f"Detail: {explorer_status.get('detail', 'unknown')}. "
                "The counts below come from reading each account directly. "
                "Check the Patchy-UI Fargate logs for the underlying boto3 exception."
            ),
        })
    unreadable = explorer_status.get("unreadable") or []
    if unreadable:
        where = "; ".join(f"{u['account_id']} {u['region']} ({u['instances']} instance(s))" for u in unreadable)
        warnings.append({
            "type": "accounts_unreadable",
            "title": "Some accounts couldn't be read",
            "message": (f"Not counted: {where}. The totals below leave these instances out. Check that the "
                        "spoke read role is deployed and trusts this UI (./deploy.sh spoke)."),
        })
    # A read that failed is named here (typed, so Findings and Changes show their own), not left as an empty list.
    if isinstance(results[1], Exception):
        logger.error(f"[DASHBOARD] Vulnerabilities failed: {results[1]}")
        warnings.append({"type": "findings_unreadable", "tone": "risk", "title": "Findings couldn't be read",
                         "message": f"Amazon Inspector findings couldn't be read ({_error_code(results[1])})."})
    elif vulns.get("unread_regions"):
        where = ", ".join(f"{u['region']} ({u['error']})" for u in vulns["unread_regions"])
        warnings.append({"type": "findings_unreadable", "tone": "risk", "title": "Some findings couldn't be read",
                         "message": f"Findings couldn't be read in {where}. The list leaves those Regions out."})
    if isinstance(results[2], Exception):
        logger.error(f"[DASHBOARD] Reports failed: {results[2]}")
        warnings.append({"type": "reports_unreadable", "tone": "risk", "title": "Reports couldn't be read",
                         "message": f"Run reports couldn't be read ({_error_code(results[2])})."})
    for u in report_data.get("unread") or []:
        title = "Runs in progress couldn't be read" if u["type"] == "running_unreadable" else "Reports couldn't be read"
        warnings.append({"type": u["type"], "tone": "risk", "title": title, "message": u["message"]})

    # Build the scopes list as (account × region) pairs and dedupe defensively.
    # Hub first, then spokes; same region order as SPOKE_REGIONS within each account.
    # The frontend uses scopes for the chip ("N accounts · M regions") and for the
    # account filter dropdown (deduped by accountId in the UI).
    _scope_pairs = await asyncio.to_thread(_dashboard_scopes)

    result = {
        "scopes": _scope_pairs,
        "environments": envs,
        "findings": vulns["findings"],
        "severity_counts": vulns["severity_counts"],
        "findings_summary": _findings_summary(vulns["findings"]),
        "unreadable_locations": unreadable,
        "activities": report_data["activities"],
        "compliance": report_data["compliance"],
        "report_details": report_data["report_details"],
        "runs": report_data.get("runs", []),
        "running_operations": report_data.get("running_operations", []),
        "errors": errors,
        "warnings": warnings,
    }

    _dashboard_cache = result
    _dashboard_cache_time = now
    return result


def _dashboard_scopes() -> list[dict]:
    """(account, region) pairs the dashboard covers: hub first, then spokes, regions in SPOKE_REGIONS order."""
    account_ids: list[str] = [_get_account_id()]
    for spoke in _get_dashboard_accounts():
        if spoke and spoke not in account_ids:
            account_ids.append(spoke)
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for acct in account_ids:
        for rgn in SPOKE_REGIONS:
            if (acct, rgn) not in seen:
                seen.add((acct, rgn))
                pairs.append({"account_id": acct, "region": rgn})
    return pairs


# Server-side dashboard cache (30s TTL)
_dashboard_cache: dict | None = None
_dashboard_cache_time: float = 0.0
# Shorter than the frontend's 30 s poll, so a poll during a run gets fresh data.
DASHBOARD_CACHE_TTL = 20


def _time_key(value) -> float:
    """A report timestamp as epoch seconds, whatever its zone; unparseable ones sort last."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _cached_dashboard(now: float, force: bool) -> dict | None:
    """The cached dashboard if it's fresh and the caller didn't force a rebuild."""
    if not force and _dashboard_cache and (now - _dashboard_cache_time) < DASHBOARD_CACHE_TTL:
        return _dashboard_cache
    return None
def _get_account_id() -> str:
    """The hub account ID (cached by reconcile once read; a failed lookup isn't cached)."""
    try:
        return _get_hub_account_id()
    except Exception as e:
        logger.warning(f"[DASHBOARD] Could not read the account ID: {e}")
        return "unknown"


# ── Static file serving (production container) ──────────────────────
# When STATIC_DIR is set (Docker), serve the built React frontend.
# In dev mode (no STATIC_DIR), the Vite dev server handles frontend.

_static_dir = os.environ.get("STATIC_DIR")
if _static_dir and Path(_static_dir).is_dir():
    from fastapi.responses import FileResponse

    _static_path = Path(_static_dir)

    # Catch-all route for SPA — serves index.html for any non-API path
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Try to serve the exact file first (JS, CSS, images, etc.)
        file_path = (_static_path / full_path).resolve()
        # Guard against path traversal — resolved path must stay within static dir
        if full_path and file_path.is_file() and file_path.is_relative_to(_static_path.resolve()):
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
