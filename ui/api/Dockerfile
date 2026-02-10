FROM python:3.12-slim

WORKDIR /app
COPY ui/api/requirements.txt /app/ui/api/requirements.txt
RUN pip install --no-cache-dir -r /app/ui/api/requirements.txt

COPY agent/.bedrock_agentcore.yaml /app/agent/.bedrock_agentcore.yaml
COPY ui/api/ /app/ui/api/

WORKDIR /app/ui/api
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
