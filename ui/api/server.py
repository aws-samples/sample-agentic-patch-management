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
    window = [t for t in _rate_limits[client_ip] if now - t < 60]
    _rate_limits[client_ip] = window
    if len(window) >= MAX_REQUESTS_PER_MINUTE:
        return False
    _rate_limits[client_ip].append(now)
    return True


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
            import base64
            # Parse user info claims for email
            payload = oidc_data.split('.')[1]
            payload += '=' * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            email = claims.get("email", "unknown")
            request.state.cognito_email = email

            # Parse access token for groups
            groups = []
            access_token = request.headers.get("x-amzn-oidc-accesstoken", "")
            if access_token:
                try:
                    at_payload = access_token.split('.')[1]
                    at_payload += '=' * (4 - len(at_payload) % 4)
                    at_claims = json.loads(base64.b64decode(at_payload))
                    groups = at_claims.get("cognito:groups", [])
                except Exception:
                    pass

            logger.info(f"Cognito auth: email={email}, groups={groups}")
            if "operators" in groups:
                return "operator"
            elif "viewers" in groups:
                return "viewer"
            # Authenticated but no group → default to viewer
            return "viewer"
        except Exception as e:
            logger.warning(f"Failed to parse Cognito JWT: {e}")

    # 2. API key auth
    if any([API_KEY, API_KEY_OPERATOR, API_KEY_VIEWER]):
        provided_key = request.headers.get("X-API-Key")
        if not provided_key:
            return None
        if API_KEY_OPERATOR and provided_key == API_KEY_OPERATOR:
            return "operator"
        if API_KEY_VIEWER and provided_key == API_KEY_VIEWER:
            return "viewer"
        if API_KEY and provided_key == API_KEY:
            return "operator"
        return None

    # 3. Pilot mode — no auth configured, use X-Role header
    role_header = request.headers.get("X-Role", "operator")
    return "viewer" if role_header == "viewer" else "operator"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Health check is always open
    if request.url.path == "/api/health":
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

# Load AgentCore config — resolve for both local dev and Docker container
_local_agent_dir = Path(__file__).parent.parent.parent / "agent"
_docker_agent_dir = Path("/app/agent")
AGENT_DIR = _local_agent_dir if _local_agent_dir.exists() else _docker_agent_dir
CONFIG_PATH = AGENT_DIR / ".bedrock_agentcore.yaml"

_agent_arn = None
_region = None


def _load_config():
    global _agent_arn, _region
    if _agent_arn:
        return
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    default_agent = config["default_agent"]
    agent_config = config["agents"][default_agent]
    _agent_arn = agent_config["bedrock_agentcore"]["agent_arn"]
    _region = agent_config["aws"]["region"]
    logger.info(f"Loaded agent config: region={_region}")


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

            # Tool start events (supervisor-level)
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
    role = getattr(request.state, "role", "operator")
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
        logger.error(f"invoke_agent_runtime failed: {e}")

        def error_stream():
            yield _sse_event("error", message=str(e))
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
    role = getattr(request.state, "role", "operator")
    return {"role": role}


@app.get("/api/auth/config")
async def auth_config(request: Request):
    """Return auth configuration for the frontend (logout URL, user email, etc.).

    When behind an ALB with Cognito, the logout URL points to the Cognito
    hosted UI /logout endpoint which clears the session and redirects back.
    """
    email = getattr(request.state, "cognito_email", None)
    cognito_domain = os.environ.get("COGNITO_DOMAIN_PREFIX", "")
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    # Build logout URL if Cognito is configured
    logout_url = None
    if cognito_domain and email:
        # Extract client_id from the ALB-injected OIDC access token
        oidc_data = request.headers.get("x-amzn-oidc-data", "")
        client_id = ""
        if oidc_data:
            try:
                import base64
                payload = oidc_data.split('.')[1]
                payload += '=' * (4 - len(payload) % 4)
                claims = json.loads(base64.b64decode(payload))
                client_id = claims.get("aud", "") or claims.get("client_id", "")
            except Exception:
                pass

        if client_id:
            # Use the current request's host as the logout redirect target
            host = request.headers.get("host", "")
            scheme = request.headers.get("x-forwarded-proto", "https")
            logout_redirect = f"{scheme}://{host}"
            cognito_base = f"https://{cognito_domain}.auth.{region}.amazoncognito.com"
            logout_url = (
                f"{cognito_base}/logout?"
                f"client_id={client_id}&"
                f"logout_uri={logout_redirect}"
            )

    return {
        "email": email,
        "logoutUrl": logout_url,
        "cognitoEnabled": email is not None,
    }


# ── Dashboard ────────────────────────────────────────────────────────

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

ENVIRONMENTS = None  # Discovered dynamically from EC2 Environment tags

# Cache account ID — never changes during a server session
_cached_account_id: str | None = None


def _aws_session():
    """Create a boto3 session using .env config."""
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION", _region or "us-east-1")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _get_account_id() -> str:
    """Cached STS get_caller_identity — avoids 3 redundant calls per refresh."""
    global _cached_account_id
    if not _cached_account_id:
        session = _aws_session()
        _cached_account_id = session.client("sts").get_caller_identity()["Account"]
    return _cached_account_id


def _fetch_environments():
    """Sync: EC2 instances + SSM health per environment.

    Discovers environments dynamically from EC2 'Environment' tags —
    no hardcoded list. Works with any customer naming convention.
    """
    session = _aws_session()
    ec2 = session.client("ec2")
    ssm = session.client("ssm")

    # Discover all running instances that have an Environment tag
    env_instances: dict[str, list[str]] = {}
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
                if env:
                    env_instances.setdefault(env, []).append(inst["InstanceId"])

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

    results = []
    for env in sorted(env_instances.keys()):
        ids = env_instances[env]
        online = sum(1 for i in ids if i in ssm_online)
        results.append({
            "environment": env,
            "total": len(ids),
            "online": online,
            "offline": len(ids) - online,
            "status": "healthy" if online == len(ids) and len(ids) > 0
                      else "warning" if online > 0
                      else "error" if len(ids) > 0
                      else "inactive",
        })
    return results


def _fetch_vulnerabilities():
    """Sync: Active Inspector findings with full pagination.

    Deduplicates by CVE ID — aggregates instance count, environments, and
    resource types per CVE. Returns unique CVEs sorted by severity.
    """
    session = _aws_session()
    inspector = session.client("inspector2")

    severity_counts: dict[str, int] = {}
    # Deduplicate: CVE ID → aggregated finding
    cve_map: dict[str, dict] = {}
    next_token = None

    while True:
        kwargs: dict = {
            "filterCriteria": {
                "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
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
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

            # Extract environment and resource type
            env = "unknown"
            resource_type = "unknown"
            resource_id = ""
            for r in f.get("resources", []):
                resource_type = r.get("type", "unknown")
                resource_id = r.get("id", "")
                for t in r.get("tags", {}).items():
                    if t[0] == "Environment":
                        env = t[1]

            # Map resource type to friendly service name
            service = _resource_type_to_service(resource_type)

            if cve_id in cve_map:
                # Aggregate: add instance, environment, service
                entry = cve_map[cve_id]
                entry["instance_count"] += 1
                if env not in entry["environments"]:
                    entry["environments"].append(env)
                if service not in entry["services"]:
                    entry["services"].append(service)
            else:
                cve_map[cve_id] = {
                    "cve_id": cve_id,
                    "severity": sev,
                    "cvss_score": (vuln.get("cvss") or [{}])[0].get("baseScore") if vuln.get("cvss") else None,
                    "title": f.get("title", ""),
                    "environment": env,
                    "environments": [env],
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

    return {"findings": findings, "severity_counts": severity_counts}


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


def _fetch_reports() -> dict:
    """Fetch all S3 compliance reports (last 30 days) in one pass.

    Returns both activity (last 7 days, max 25) and compliance stats (30 days).
    Eliminates per-object head_object calls by reading JSON body in parallel.
    """
    account_id = _get_account_id()
    bucket = f"patch-compliance-reports-{account_id}"
    session = _aws_session()
    s3 = session.client("s3")

    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        return {"activities": [], "compliance": None, "report_details": []}

    # List all report keys for last 30 days (few API calls via month-prefix)
    objects = _list_report_keys(s3, bucket, days=30)
    if not objects:
        return {"activities": [], "compliance": None, "report_details": []}

    # Read report JSON bodies in parallel (10 threads)
    reports: list[tuple[dict, dict]] = []  # (s3_obj, parsed_json)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_read_report_json, s3, bucket, obj["Key"]): obj for obj in objects}
        for future in futures:
            obj = futures[future]
            parsed = future.result()
            if parsed:
                reports.append((obj, parsed))

    # Build activity list (last 7 days, sorted, max 25)
    cutoff_7d = datetime.utcnow() - timedelta(days=7)
    activities = []
    for obj, report in reports:
        last_mod = obj["LastModified"].replace(tzinfo=None)
        if last_mod >= cutoff_7d:
            vuln = report.get("vulnerability", {})
            scope = report.get("scope", {})
            comp_data = report.get("compliance", {})
            exec_data = report.get("execution", {})
            activities.append({
                "report_key": obj["Key"],
                "timestamp": obj["LastModified"].isoformat(),
                "cve_id": vuln.get("cve_id", "N/A"),
                "environment": scope.get("environment", "N/A"),
                "severity": vuln.get("severity", "N/A"),
                "decision": exec_data.get("decision", "N/A"),
                "sla_met": str(comp_data.get("sla_met", "UNKNOWN")),
            })
    activities.sort(key=lambda x: x["timestamp"], reverse=True)

    # Build compliance stats (all 30 days)
    total = 0
    sla_met = 0
    sla_breached = 0
    by_severity: dict[str, dict] = {}
    by_environment: dict[str, dict] = {}
    by_team: dict[str, dict] = {}

    # Also build detailed report list for explainability
    report_details = []

    for _, report in reports:
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
            "time_to_patch_hours": comp_data.get("time_to_patch_hours"),
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

    envs = results[0] if not isinstance(results[0], Exception) else []
    vulns = results[1] if not isinstance(results[1], Exception) else {"findings": [], "severity_counts": {}}
    report_data = results[2] if not isinstance(results[2], Exception) else {"activities": [], "compliance": None, "report_details": []}

    if isinstance(results[0], Exception):
        errors.append(f"Environments: {results[0]}")
    if isinstance(results[1], Exception):
        errors.append(f"Vulnerabilities: {results[1]}")
    if isinstance(results[2], Exception):
        errors.append(f"Reports: {results[2]}")

    result = {
        "scopes": [{"account_id": _get_account_id(), "region": _region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}],
        "environments": envs,
        "findings": vulns["findings"],
        "severity_counts": vulns["severity_counts"],
        "activities": report_data["activities"],
        "compliance": report_data["compliance"],
        "report_details": report_data["report_details"],
        "errors": errors,
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

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
