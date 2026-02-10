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


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
