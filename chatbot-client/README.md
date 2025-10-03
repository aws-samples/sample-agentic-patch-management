# Patch Automation Chat Client

Interactive chat interface for the intelligent patch automation agent.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Interactive chat
python src/patch_client.py

# Single message
python src/patch_client.py --message "What are your capabilities?"

# Debug mode
python src/patch_client.py --debug
```

## Features

- **Interactive Chat**: Real-time conversation with the patch automation agent
- **Session Management**: Maintains conversation context across messages
- **Debug Mode**: Detailed request/response logging
- **Conversation Export**: Save chat history to JSON files
- **Command Support**: Built-in commands for common operations

## Commands

- `/quit`, `/exit` - End conversation
- `/debug` - Toggle debug mode
- `/history` - Show conversation history
- `/export` - Export conversation to file

## Usage Examples

### Interactive Mode
```bash
python src/patch_client.py
```

### Single Message Mode
```bash
python src/patch_client.py --message "Analyze latest vulnerabilities from AWS Inspector"
```

### Debug Mode
```bash
python src/patch_client.py --debug --message "Check patch compliance status"
```

### Export Conversation
```bash
python src/patch_client.py --export my_conversation.json
```

## Architecture

The client uses the AgentCore CLI (`agentcore invoke`) to communicate with the deployed patch automation agent. It follows the same patterns as the AWS Operations Agent but simplified for local usage.

### Session Management
- Each client instance generates a unique session ID
- Session ID is passed with every message for conversation continuity
- Conversation history is maintained locally

### Error Handling
- Graceful fallback when AgentCore CLI fails
- Clear error messages for debugging
- Debug mode for detailed troubleshooting
