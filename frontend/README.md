# Patch Automation Web App

React + Cloudscape Design System UI for intelligent patch automation.

## Quick Start

```bash
# From project root
./start-webapp.sh
```

This starts:
- **Backend** (FastAPI): http://localhost:8000
- **Frontend** (React): http://localhost:3000

## Features

### Chat Interface
- Real-time streaming responses from agents
- Natural language queries about patches, CVEs, instances
- Full conversation history

### Dashboard
- Environment health status (dev/staging/prod)
- Instance inventory overview
- Live system metrics

## Architecture

```
Frontend (React + Cloudscape)
    ↓ HTTP/SSE
Backend (FastAPI)
    ↓ subprocess
AgentCore CLI → Agents
```

## Development

```bash
# Backend only
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py

# Frontend only
cd frontend
npm install
npm run dev
```

## API Endpoints

- `POST /api/chat/stream` - Stream agent responses via SSE
- `GET /api/instances` - Get EC2 instance status
- `GET /api/health` - Health check

## Technology Stack

- **Frontend**: React 18, TypeScript, Vite, Cloudscape Design System
- **Backend**: FastAPI, Python 3.9+
- **Communication**: Server-Sent Events (SSE) for streaming
- **Agent Integration**: AgentCore CLI subprocess calls
