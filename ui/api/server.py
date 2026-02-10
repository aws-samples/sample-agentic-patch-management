"""
Thin API proxy for Intelligent Patch Automation.
Wraps AgentCore invoke_agent_runtime as HTTP endpoints with SSE streaming.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Auto-source .env from project root (same as deploy.sh / chat.sh)
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

import boto3
import yaml
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Patch Automation API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load AgentCore config
AGENT_DIR = Path(__file__).parent.parent.parent / "agent"
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

# Task 1.1: Tool-to-workflow-step mapping
WORKFLOW_TOOLS: dict[str, str] = {
    "get_patch_compliance": "discovery",
    "get_maintenance_windows": "window_check",
    "execute_patch_operation": "patch_execute",
    "get_command_status": "patch_execute",
    "check_instance_health": "health_check",
    "check_cloudwatch_alarms": "alarm_check",
    "generate_compliance_report": "report",
    "verify_and_proceed": "verification",
}


def _emit_workflow_event(event_type: str, **kwargs) -> str:
    """Emit a workflow-specific SSE event."""
    return _sse_event(event_type, **kwargs)


def _sse_event(event_type: str, **kwargs) -> str:
    """Format a single SSE data line."""
    payload = {"type": event_type, **kwargs}
    return f"data: {json.dumps(payload)}\n\n"


def _parse_agentcore_stream(event_stream):
    """
    Generator that reads the raw AgentCore SSE byte stream and yields
    simplified SSE events for the browser.

    Mirrors the parsing logic in chatbot-client/src/patch_client.py.
    """
    buffer = b""
    text_buffer = ""
    chunk_size = 4096
    current_tool = None
    start_time = time.time()

    # Task 1.2: Workflow tracking variables
    tool_input_buffer: str = ""          # Accumulates toolUse.input JSON chunks per content block
    current_block_tool: str | None = None  # Tool name for the active content block (if it's a workflow tool)
    current_block_index: int | None = None  # Index of the active content block
    # Task 1.6: Track whether workflow_start has been emitted this session
    workflow_started: bool = False

    try:
        while True:
            chunk = event_stream.read(chunk_size)
            if not chunk:
                break

            buffer += chunk

            try:
                decoded = buffer.decode("utf-8")
                text_buffer += decoded
                buffer = b""
            except UnicodeDecodeError:
                continue

            while "\n" in text_buffer:
                line, text_buffer = text_buffer.split("\n", 1)
                line = line.strip()

                if not line or not line.startswith("data: "):
                    continue

                json_str = line[6:]

                try:
                    data = json.loads(json_str)
                    if not isinstance(data, dict):
                        continue

                    # Tool start events
                    if "event" in data and "contentBlockStart" in data["event"]:
                        block_start = data["event"]["contentBlockStart"]
                        block_index = block_start.get("contentBlockIndex")
                        start = block_start.get("start", {})
                        if "toolUse" in start:
                            tool_name = start["toolUse"].get("name")
                            if tool_name and tool_name != current_tool:
                                current_tool = tool_name
                                yield _sse_event("tool_start", tool=tool_name)

                            # Task 1.3: Detect workflow tools and emit workflow_step started
                            if tool_name and tool_name in WORKFLOW_TOOLS:
                                current_block_tool = tool_name
                                current_block_index = block_index
                                tool_input_buffer = ""
                                step = WORKFLOW_TOOLS[tool_name]
                                yield _emit_workflow_event(
                                    "workflow_step",
                                    step=step,
                                    tool=tool_name,
                                    status="started",
                                )
                            else:
                                # Not a workflow tool — reset tracking
                                current_block_tool = None
                                current_block_index = None
                                tool_input_buffer = ""

                    # Text delta events + Task 1.4: toolUse.input buffering
                    if "event" in data and "contentBlockDelta" in data["event"]:
                        delta = data["event"]["contentBlockDelta"].get("delta", {})
                        if "text" in delta:
                            yield _sse_event("text", content=delta["text"])
                        # Task 1.4: Append toolUse.input chunks to buffer
                        if "toolUse" in delta and current_block_tool is not None:
                            input_chunk = delta["toolUse"].get("input", "")
                            if input_chunk:
                                tool_input_buffer += input_chunk

                    # Task 1.5: contentBlockStop — parse accumulated input, emit workflow events
                    if "event" in data and "contentBlockStop" in data["event"]:
                        if current_block_tool is not None and tool_input_buffer:
                            try:
                                tool_input = json.loads(tool_input_buffer)
                            except (json.JSONDecodeError, ValueError):
                                tool_input = {}

                            step = WORKFLOW_TOOLS.get(current_block_tool, "")

                            # Task 1.6: Emit workflow_start on first workflow tool
                            if not workflow_started:
                                workflow_started = True
                                start_payload: dict = {}
                                # Extract CVE ID and severity from tool input if available
                                if tool_input.get("cve_id"):
                                    start_payload["cve_id"] = tool_input["cve_id"]
                                if tool_input.get("severity"):
                                    start_payload["severity"] = tool_input["severity"]
                                if tool_input.get("environment"):
                                    start_payload["environments"] = [tool_input["environment"]]
                                yield _emit_workflow_event("workflow_start", **start_payload)

                            # Emit workflow_data with the parsed tool input
                            if tool_input:
                                yield _emit_workflow_event(
                                    "workflow_data",
                                    step=step,
                                    tool=current_block_tool,
                                    data=tool_input,
                                )

                            # Task 1.7: Emit workflow_env_done when generate_compliance_report completes
                            if current_block_tool == "generate_compliance_report":
                                env_name = tool_input.get("environment", "unknown")
                                yield _emit_workflow_event(
                                    "workflow_env_done",
                                    environment=env_name,
                                )

                        # Reset buffer state regardless of whether we emitted
                        current_block_tool = None
                        current_block_index = None
                        tool_input_buffer = ""

                except json.JSONDecodeError:
                    pass

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
async def chat(request: Request):
    """
    Send a message to the agent and stream the response as SSE.

    Body: { "message": "...", "session_id": "..." }
    Returns: text/event-stream with tool_start / text / done events.
    """
    body = await request.json()
    message = body.get("message", "").strip()
    session_id = body.get("session_id") or f"web-{uuid.uuid4().hex}"

    if not message:
        return {"error": "message is required"}, 400

    client = _get_client()
    payload = json.dumps({"prompt": message}).encode()

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
        _parse_agentcore_stream(event_stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    """Health check — verifies agent config is loadable."""
    try:
        _load_config()
        return {"status": "ok", "agent_arn": _agent_arn, "region": _region}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── Dashboard ────────────────────────────────────────────────────────

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

ENVIRONMENTS = ["dev", "staging", "prod"]

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
    """Sync: EC2 instances + SSM health per environment."""
    session = _aws_session()
    ec2 = session.client("ec2")
    ssm = session.client("ssm")

    ec2_resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Environment", "Values": ENVIRONMENTS},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )

    env_instances: dict[str, list[str]] = {e: [] for e in ENVIRONMENTS}
    for res in ec2_resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            env = tags.get("Environment", "")
            if env in env_instances:
                env_instances[env].append(inst["InstanceId"])

    all_ids = [i for ids in env_instances.values() for i in ids]
    ssm_online: set[str] = set()
    if all_ids:
        ssm_resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": all_ids}]
        )
        for info in ssm_resp.get("InstanceInformationList", []):
            if info.get("PingStatus") == "Online":
                ssm_online.add(info["InstanceId"])

    results = []
    for env in ENVIRONMENTS:
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
    """Sync: Active Inspector findings."""
    session = _aws_session()
    inspector = session.client("inspector2")

    resp = inspector.list_findings(
        filterCriteria={
            "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
        },
        sortCriteria={"field": "SEVERITY", "sortOrder": "DESC"},
        maxResults=50,
    )

    findings = []
    severity_counts: dict[str, int] = {}
    for f in resp.get("findings", []):
        vuln = f.get("packageVulnerabilityDetails", {})
        sev = f.get("severity", "UNKNOWN")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

        env = "unknown"
        for r in f.get("resources", []):
            for t in r.get("tags", {}).items():
                if t[0] == "Environment":
                    env = t[1]

        findings.append({
            "cve_id": vuln.get("vulnerabilityId", "N/A"),
            "severity": sev,
            "cvss_score": vuln.get("cvss", [{}])[0].get("baseScore"),
            "title": f.get("title", ""),
            "environment": env,
            "resource_id": f.get("resources", [{}])[0].get("id", ""),
            "fix_available": f.get("fixAvailable", "UNKNOWN"),
        })

    return {"findings": findings, "severity_counts": severity_counts}


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
        return {"activities": [], "compliance": None}

    # List all report keys for last 30 days (few API calls via month-prefix)
    objects = _list_report_keys(s3, bucket, days=30)
    if not objects:
        return {"activities": [], "compliance": None}

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


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
