# Patch Automation Chat Client

Minimal, intelligent chat interface that trusts the agent's decision-making capabilities.

## Philosophy

This client is intentionally simple - it passes messages to the agent and lets the LLM handle:
- Workflow detection (information vs complete response)
- Multi-agent coordination
- Tool selection and execution
- Response formatting

**No rigid commands. No predefined workflows. Just natural conversation.**

## Quick Start

### Interactive Chat
```bash
# From project root
./chat.sh

# Or directly
cd chatbot-client
python src/patch_client.py
```

### Single Message
```bash
./chat.sh -m "What vulnerabilities do we have?"
./chat.sh -m "Handle critical CVE from Inspector"
```

### Debug Mode
```bash
./chat.sh -d
./chat.sh --debug
```

## Usage Examples

### Natural Language Queries
```bash
# Information queries
"What vulnerabilities exist in our environment?"
"Show me patch compliance status"
"Tell me about CVE-2025-38527"

# Action requests
"Handle the critical vulnerability from Inspector"
"Create a patch deployment plan for production"
"I need emergency response for CVE-2025-38527"

# Complex workflows
"Analyze our security posture and recommend actions"
"What's our compliance status and what needs patching?"
"Create an executive briefing for the latest CVE"
```

### Interactive Commands
While in chat mode:
- `export` - Save conversation to JSON file
- `debug` - Toggle debug mode
- `help` or `?` - Show examples
- `quit` or `exit` - End session

## Features

### ✅ Minimal & Intelligent
- ~200 lines of code
- Trust agent's intelligence
- No rigid command structure
- Natural language interface

### ✅ Rich UX
- Beautiful welcome screen with examples
- **Real-time progress indicators** showing agent activity
- Clean response formatting
- Session summary on exit

### ✅ Transparency
See what the agent is doing in real-time:
- 🔍 Consulting Vulnerability Analyst...
- 🛠️  Consulting Patch Manager...
- ⚙️  Using tools...
- ✍️  Generating response...
- ✅ Complete

### ✅ Error Handling
- Friendly error messages
- Timeout protection (5 min)
- Automatic retry suggestions
- Debug mode for troubleshooting

### ✅ Conversation Management
- Automatic session tracking
- Export to JSON
- Message count tracking
- Timestamp recording

## Command Line Options

```bash
python src/patch_client.py [OPTIONS]

Options:
  -m, --message TEXT    Send single message
  -d, --debug          Enable debug mode
  -e, --export FILE    Export conversation to file
  -h, --help           Show help message
```

## Examples

### Quick Vulnerability Check
```bash
./chat.sh -m "What are the highest severity vulnerabilities?"
```

### Emergency Response with Export
```bash
./chat.sh -m "Handle CVE-2025-38527" -e emergency_response.json
```

### Debug Mode for Troubleshooting
```bash
./chat.sh -d
```

## Architecture

```
chat.sh (wrapper)
    ↓
patch_client.py (minimal client)
    ↓
agentcore invoke (CLI)
    ↓
Supervisor Agent (orchestrator)
    ↓
├─> Vulnerability Analyst (security)
└─> Patch Manager (operations)
```

## Session Management

Each chat session gets a unique ID for conversation continuity:
- Format: `patch_<12-char-uuid>`
- Enables memory across messages
- Supports follow-up questions
- Maintains context

## Export Format

```json
{
  "session_id": "patch_a1b2c3d4e5f6",
  "exported_at": "2025-10-03T17:00:00",
  "message_count": 5,
  "conversations": [
    {
      "timestamp": "2025-10-03T16:55:00",
      "user": "What vulnerabilities exist?",
      "agent": "Based on AWS Inspector findings..."
    }
  ]
}
```

## Troubleshooting

### Agent Not Found
```bash
❌ AgentCore CLI not found
→ Install: pip install bedrock-agentcore
```

### Agent Not Deployed
```bash
❌ Agent not deployed
→ Run: ./deploy.sh agent
```

### Timeout
```bash
⏱️ Agent timeout (5 min)
→ Try a simpler query or check agent status
```

### Debug Mode
```bash
./chat.sh -d
# Shows:
# - Session ID
# - Request payload
# - Response parsing
# - Error details
```

## Comparison to AWS Operations Agent

| Feature | This Implementation | AWS Ops Agent |
|---------|-------------------|---------------|
| Lines of Code | ~200 | ~800+ |
| Authentication | None (local) | OAuth2/JWT |
| Configuration | None | Dual config system |
| Runtime Selection | Single agent | Multi-runtime |
| Complexity | Minimal | Enterprise-grade |
| Use Case | Local/Demo | Production |

## Design Principles

1. **Trust the Agent**: Let LLM handle intelligence
2. **Minimal Client**: Just transport and UX
3. **Natural Language**: No rigid commands
4. **Rich Feedback**: Clear status and errors
5. **Easy Export**: Save important conversations

## Future Enhancements (Optional)

- [ ] Syntax highlighting for code blocks
- [ ] Streaming response display
- [ ] Conversation search
- [ ] Multi-session management
- [ ] Web UI (Streamlit/Gradio)

## Contributing

Keep it minimal! Before adding features, ask:
1. Can the agent handle this?
2. Is this essential for UX?
3. Does it add unnecessary complexity?

---

**Status**: ✅ Production Ready  
**Philosophy**: Minimal client, maximum agent intelligence  
**Next**: Let the agent evolve, keep the client simple
