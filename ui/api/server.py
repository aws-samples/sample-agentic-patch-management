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
                        start = data["event"]["contentBlockStart"].get("start", {})
                        if "toolUse" in start:
                            tool_name = start["toolUse"].get("name")
                            if tool_name and tool_name != current_tool:
                                current_tool = tool_name
                                yield _sse_event("tool_start", tool=tool_name)

                    # Text delta events
                    if "event" in data and "contentBlockDelta" in data["event"]:
                        delta = data["event"]["contentBlockDelta"].get("delta", {})
                        if "text" in delta:
                            yield _sse_event("text", content=delta["text"])

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


# ── Dashboard endpoints ─────────────────────────────────────────────

ENVIRONMENTS = ["dev", "staging", "prod"]


def _aws_session():
    """Create a boto3 session using .env config."""
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION", _region or "us-east-1")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


@app.get("/api/dashboard/environments")
async def dashboard_environments():
    """Environment status cards — instance counts, SSM health, vuln counts per env."""
    try:
        session = _aws_session()
        ec2 = session.client("ec2")
        ssm = session.client("ssm")
        inspector = session.client("inspector2")

        # Get all tagged instances in one call
        ec2_resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Environment", "Values": ENVIRONMENTS},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )

        # Build instance map by environment
        env_instances: dict[str, list[str]] = {e: [] for e in ENVIRONMENTS}
        for res in ec2_resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                env = tags.get("Environment", "")
                if env in env_instances:
                    env_instances[env].append(inst["InstanceId"])

        # Get SSM connectivity in one call
        all_ids = [i for ids in env_instances.values() for i in ids]
        ssm_online: set[str] = set()
        if all_ids:
            ssm_resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": all_ids}]
            )
            for info in ssm_resp.get("InstanceInformationList", []):
                if info.get("PingStatus") == "Online":
                    ssm_online.add(info["InstanceId"])

        # Get vuln counts per environment from Inspector
        env_vulns: dict[str, dict[str, int]] = {e: {} for e in ENVIRONMENTS}
        try:
            for env in ENVIRONMENTS:
                resp = inspector.list_findings(
                    filterCriteria={
                        "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
                        "resourceTags": [{"comparison": "EQUALS", "key": "Environment", "value": env}],
                    },
                    maxResults=100,
                )
                for f in resp.get("findings", []):
                    sev = f.get("severity", "UNKNOWN")
                    env_vulns[env][sev] = env_vulns[env].get(sev, 0) + 1
        except Exception as ve:
            logger.warning(f"Could not fetch per-env vulns: {ve}")

        results = []
        for env in ENVIRONMENTS:
            ids = env_instances[env]
            online = sum(1 for i in ids if i in ssm_online)
            vulns = env_vulns.get(env, {})
            results.append({
                "environment": env,
                "total": len(ids),
                "online": online,
                "offline": len(ids) - online,
                "status": "healthy" if online == len(ids) and len(ids) > 0
                          else "warning" if online > 0
                          else "error" if len(ids) > 0
                          else "inactive",
                "vulns": vulns,
                "vuln_total": sum(vulns.values()),
            })

        return {"environments": results}
    except Exception as e:
        logger.error(f"dashboard/environments error: {e}")
        return {"environments": [], "error": str(e)}


@app.get("/api/dashboard/vulnerabilities")
async def dashboard_vulnerabilities():
    """Active vulnerability findings from Inspector, sorted by severity."""
    try:
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

            # Extract environment from resource tags
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

        return {
            "findings": findings,
            "total": len(findings),
            "severity_counts": severity_counts,
        }
    except Exception as e:
        logger.error(f"dashboard/vulnerabilities error: {e}")
        return {"findings": [], "total": 0, "severity_counts": {}, "error": str(e)}


@app.get("/api/dashboard/activity")
async def dashboard_activity():
    """Recent patching activity from S3 compliance reports (last 7 days)."""
    try:
        session = _aws_session()
        s3 = session.client("s3")
        sts = session.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        bucket = f"patch-compliance-reports-{account_id}"

        # Check bucket exists
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            return {"activities": [], "total": 0, "message": "No compliance bucket found"}

        # Scan last 7 days of date-prefixed keys
        from datetime import datetime, timedelta

        activities = []
        now = datetime.utcnow()
        for day_offset in range(7):
            d = now - timedelta(days=day_offset)
            prefix = f"{d.year}/{d.month:02d}/{d.day:02d}/"
            try:
                resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
                for obj in resp.get("Contents", []):
                    # Read report metadata from S3 object metadata
                    head = s3.head_object(Bucket=bucket, Key=obj["Key"])
                    meta = head.get("Metadata", {})
                    activities.append({
                        "report_key": obj["Key"],
                        "timestamp": obj["LastModified"].isoformat(),
                        "cve_id": meta.get("cve-id", "N/A"),
                        "environment": meta.get("environment", "N/A"),
                        "severity": meta.get("severity", "N/A"),
                        "decision": meta.get("decision-type", "N/A"),
                        "sla_met": meta.get("sla-met", "UNKNOWN"),
                    })
            except Exception:
                continue

        # Sort newest first
        activities.sort(key=lambda x: x["timestamp"], reverse=True)

        return {"activities": activities[:25], "total": len(activities)}
    except Exception as e:
        logger.error(f"dashboard/activity error: {e}")
        return {"activities": [], "total": 0, "error": str(e)}


@app.get("/api/dashboard/compliance")
async def dashboard_compliance():
    """Compliance summary — SLA stats from recent reports."""
    try:
        session = _aws_session()
        s3 = session.client("s3")
        sts = session.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        bucket = f"patch-compliance-reports-{account_id}"

        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            return {"summary": {}, "message": "No compliance bucket found"}

        from datetime import datetime, timedelta

        total = 0
        sla_met = 0
        sla_breached = 0
        by_severity: dict[str, dict] = {}

        now = datetime.utcnow()
        for day_offset in range(30):
            d = now - timedelta(days=day_offset)
            prefix = f"{d.year}/{d.month:02d}/{d.day:02d}/"
            try:
                resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
                for obj in resp.get("Contents", []):
                    head = s3.head_object(Bucket=bucket, Key=obj["Key"])
                    meta = head.get("Metadata", {})
                    total += 1
                    sla_status = meta.get("sla-met", "UNKNOWN")
                    severity = meta.get("severity", "UNKNOWN")

                    if sla_status == "True":
                        sla_met += 1
                    elif sla_status == "False":
                        sla_breached += 1

                    if severity not in by_severity:
                        by_severity[severity] = {"total": 0, "met": 0, "breached": 0}
                    by_severity[severity]["total"] += 1
                    if sla_status == "True":
                        by_severity[severity]["met"] += 1
                    elif sla_status == "False":
                        by_severity[severity]["breached"] += 1
            except Exception:
                continue

        sla_rate = round((sla_met / total) * 100, 1) if total > 0 else 0

        return {
            "summary": {
                "total_reports": total,
                "sla_met": sla_met,
                "sla_breached": sla_breached,
                "sla_rate_percent": sla_rate,
                "period_days": 30,
                "by_severity": by_severity,
            }
        }
    except Exception as e:
        logger.error(f"dashboard/compliance error: {e}")
        return {"summary": {}, "error": str(e)}


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
