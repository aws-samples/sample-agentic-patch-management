FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app

# Copy entire project
COPY . .

# Install dependencies
RUN python -m pip install --no-cache-dir -r requirements.txt

# Set AWS region environment variable
ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1

# Signal that this is running in Docker
ENV DOCKER_CONTAINER=1

# Force unbuffered stdout/stderr so logs reach CloudWatch immediately
ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

# Use the full module path
CMD ["opentelemetry-instrument", "python", "-m", "supervisor"]
