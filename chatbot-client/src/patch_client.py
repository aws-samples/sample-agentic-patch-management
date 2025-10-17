#!/usr/bin/env python3
"""
Patch Automation Chat Client

Minimal, intelligent chat interface that trusts the agent's decision-making.
Let the LLM handle workflow detection, tool selection, and response formatting.
"""

import json
import uuid
import sys
import os
import subprocess
import time
import threading
import re
import logging
from datetime import datetime
from typing import Optional

# Compile regex once at module level
TEXT_DELTA_PATTERN = re.compile(r'"text":\s*"([^"]*(?:\\.[^"]*)*)"')

# Configure logging
logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(message)s')

class PatchAutomationClient:
    """Minimal chat client - trust the agent's intelligence"""
    
    def __init__(self, debug: bool = False, profile: Optional[str] = None):
        """Initialize client"""
        self.debug = debug
        self.profile = profile
        self.conversation_history = []
        self.session_id = f"patch_{uuid.uuid4().hex[:12]}"
        
        if self.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            print(f"🔧 Debug Mode: ON")
            print(f"🔧 Session: {self.session_id}")
            if self.profile:
                print(f"🔧 AWS Profile: {self.profile}")
            print()
    
    def _handle_error(self, error: str) -> str:
        """Centralized error handling"""
        if "not found" in error.lower():
            return "❌ AgentCore CLI not found. Install: pip install bedrock-agentcore"
        elif "not deployed" in error.lower():
            return "❌ Agent not deployed. Run: ./deploy.sh agent"
        else:
            return f"❌ Agent error:\n{error}"
    
    def _parse_text_delta(self, line: str) -> Optional[str]:
        """Extract text from contentBlockDelta event"""
        try:
            match = TEXT_DELTA_PATTERN.search(line)
            if match:
                text = match.group(1)
                # Unescape JSON
                return text.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
        except Exception as e:
            logging.debug(f"Failed to parse text delta: {e}")
        return None
    
    def chat(self, message: str, stream: bool = True) -> str:
        """Send message to agent - it decides what to do"""
        payload = {"prompt": message, "session_id": self.session_id}
        
        logging.debug(f"Payload: {json.dumps(payload, indent=2)}")
        
        try:
            # Build command with optional profile
            cmd = ["agentcore", "invoke", json.dumps(payload)]
            env = os.environ.copy()
            if self.profile:
                env["AWS_PROFILE"] = self.profile
            
            if stream:
                response = self._run_streaming(cmd, env)
            else:
                response = self._run_blocking(cmd, env)
            
            # Store in history
            self.conversation_history.append({
                "timestamp": datetime.now().isoformat(),
                "user": message,
                "agent": response
            })
            
            return response
            
        except subprocess.TimeoutExpired:
            return "⏱️  Agent timeout (5 min). Try a simpler query or check agent status."
        except FileNotFoundError:
            return "❌ AgentCore CLI not found. Install: pip install bedrock-agentcore"
        except Exception as e:
            logging.error(f"Unexpected error: {e}", exc_info=True)
            return f"❌ Unexpected error: {str(e)}"
    
    def _run_streaming(self, cmd: list, env: dict) -> str:
        """Run agent with streaming output"""
        process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        # Capture stderr in background thread
        stderr_lines = []
        def capture_stderr():
            for line in iter(process.stderr.readline, ''):
                if line:
                    stderr_lines.append(line)
        
        stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
        stderr_thread.start()
        
        output_lines = []
        response_started = False
        last_agent_shown = None
        
        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            output_lines.append(line)
            
            # Parse JSON events properly
            try:
                event = json.loads(line.strip())
                
                # Detect tool usage
                if isinstance(event, dict):
                    # Check for toolUse event with name field
                    tool_name = None
                    if 'toolUse' in event:
                        tool_name = event.get('toolUse', {}).get('name') if isinstance(event['toolUse'], dict) else None
                    elif event.get('type') == 'toolUse' or 'name' in event:
                        tool_name = event.get('name')
                    
                    if tool_name:
                        if tool_name == 'consult_vulnerability_analyst' and last_agent_shown != 'vuln':
                            print("\n🔍 Consulting Vulnerability Analyst...", flush=True)
                            last_agent_shown = 'vuln'
                        elif tool_name == 'consult_patch_manager' and last_agent_shown != 'patch':
                            print("\n🛠️  Consulting Patch Manager...", flush=True)
                            last_agent_shown = 'patch'
                        elif tool_name == 'use_aws' and last_agent_shown != 'aws':
                            print("\n⚙️  Querying AWS...", flush=True)
                            last_agent_shown = 'aws'
                    
                    # Check for contentBlockDelta with text
                    if 'contentBlockDelta' in event:
                        delta = event['contentBlockDelta']
                        if isinstance(delta, dict) and 'delta' in delta:
                            text = delta['delta'].get('text')
                            if text:
                                if not response_started:
                                    print("\n🤖 Agent: ", end="", flush=True)
                                    response_started = True
                                print(text, end="", flush=True)
            
            except (json.JSONDecodeError, KeyError, TypeError):
                # Not valid JSON or unexpected structure - try regex fallback for text
                if '"contentBlockDelta"' in line and '"text"' in line:
                    text = self._parse_text_delta(line)
                    if text:
                        if not response_started:
                            print("\n🤖 Agent: ", end="", flush=True)
                            response_started = True
                        print(text, end="", flush=True)
        
        process.wait(timeout=300)
        stderr_thread.join(timeout=1)
        
        if not response_started:
            print("\n🤖 Agent: (processing...)", end="", flush=True)
        
        print()  # Newline after streaming
        
        # Check for errors
        if process.returncode != 0:
            error = ''.join(stderr_lines).strip() or "Unknown error"
            return self._handle_error(error)
        
        # Extract response for history, but return marker to prevent double print
        full_response = self._extract_clean_response(''.join(output_lines))
        return f"[STREAMED] {full_response}"
    
    def _run_blocking(self, cmd: list, env: dict) -> str:
        """Run agent with spinner (non-streaming)"""
        spinner_active = threading.Event()
        spinner_active.set()
        
        def show_spinner():
            chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while spinner_active.is_set():
                print(f"\r{chars[i % len(chars)]} Thinking...", end="", flush=True)
                i += 1
                time.sleep(0.1)
            print("\r" + " " * 20 + "\r", end="", flush=True)
        
        spinner_thread = threading.Thread(target=show_spinner, daemon=True)
        spinner_thread.start()
        
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        
        spinner_active.clear()
        spinner_thread.join(timeout=0.5)
        
        if result.returncode != 0:
            error = result.stderr.strip() or "Unknown error"
            return self._handle_error(error)
        
        return self._extract_clean_response(result.stdout)
    
    def _extract_clean_response(self, raw_output: str) -> str:
        """Extract clean response from streaming output"""
        try:
            collected_text = []
            lines = raw_output.split('\n')
            
            for line in lines:
                if not line.strip():
                    continue
                
                # Look for contentBlockDelta events with text
                if '"contentBlockDelta"' in line and '"text"' in line:
                    text = self._parse_text_delta(line)
                    if text:
                        collected_text.append(text)
            
            # Join all collected text
            if collected_text:
                full_response = ''.join(collected_text).strip()
                
                # Check for answer tags
                if '<answer>' in full_response and '</answer>' in full_response:
                    start = full_response.find('<answer>') + 8
                    end = full_response.find('</answer>')
                    return full_response[start:end].strip()
                
                return full_response
            
            logging.warning("Could not parse response from output")
            return "No response received. Try again or check agent status."
            
        except Exception as e:
            logging.error(f"Extraction error: {e}", exc_info=True)
            return "Error parsing response. Enable debug mode for details."
    
    def export_conversation(self, filename: Optional[str] = None) -> str:
        """Export conversation to JSON file"""
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"conversation_{timestamp}.json"
        
        export_data = {
            "session_id": self.session_id,
            "exported_at": datetime.now().isoformat(),
            "message_count": len(self.conversation_history),
            "conversations": self.conversation_history
        }
        
        with open(filename, 'w') as f:
            json.dump(export_data, f, indent=2)
        
        return filename
    
    def show_history(self):
        """Display conversation history"""
        if not self.conversation_history:
            print("📝 No conversation history yet\n")
            return
        
        print(f"\n📜 Conversation History ({len(self.conversation_history)} messages)")
        print("=" * 70)
        
        for i, msg in enumerate(self.conversation_history, 1):
            print(f"\n[{msg['timestamp']}] Message #{i}")
            print(f"👤 You: {msg['user']}")
            # Truncate long responses
            agent_msg = msg['agent']
            if len(agent_msg) > 200:
                agent_msg = agent_msg[:200] + "..."
            print(f"🤖 Agent: {agent_msg}")
        print()
    
    def show_info(self):
        """Display session information"""
        print(f"\n🔗 Session Information")
        print("=" * 40)
        print(f"Session ID: {self.session_id}")
        print(f"Messages: {len(self.conversation_history)}")
        print(f"Debug Mode: {'ON' if self.debug else 'OFF'}")
        print()
    
    def _show_welcome(self):
        """Display welcome message with examples"""
        print("╔" + "═" * 68 + "╗")
        print("║" + " " * 15 + "🤖 Intelligent Patch Automation Agent" + " " * 28 + "║")
        print("╚" + "═" * 68 + "╝")
        print()
        print("💡 Ask me anything about vulnerabilities, patching, or compliance:")
        print()
        print("   • What vulnerabilities do we have?")
        print("   • Handle the critical CVE from Inspector")
        print("   • Show patch compliance status for production")
        print("   • Create an emergency response plan for CVE-2025-38527")
        print()
        print("📝 Commands:")
        print("   history  - Show conversation history")
        print("   info     - Show session information")
        print("   export   - Save conversation to file")
        print("   debug    - Toggle debug mode")
        print("   reset    - Start new session")
        print("   quit     - Exit (auto-exports if messages sent)")
        print("─" * 70)
        print()
    
    def chat_loop(self):
        """Interactive chat with rich UX"""
        self._show_welcome()
        
        message_count = 0
        
        while True:
            try:
                # Get user input
                user_input = input("You: ").strip()
                
                # Handle empty input
                if not user_input:
                    continue
                
                # Handle commands
                if user_input.lower() in ['quit', 'exit', 'q']:
                    print()
                    print("─" * 70)
                    if message_count > 0:
                        filename = self.export_conversation()
                        print(f"👋 Session ended. {message_count} messages exchanged.")
                        print(f"💾 Saved to {filename}")
                    else:
                        print(f"👋 Session ended. No messages exchanged.")
                    print("─" * 70)
                    break
                
                if user_input.lower() == 'export':
                    filename = self.export_conversation()
                    print(f"✅ Saved to {filename}\n")
                    continue
                
                if user_input.lower() == 'debug':
                    self.debug = not self.debug
                    print(f"🔧 Debug mode: {'ON' if self.debug else 'OFF'}\n")
                    continue
                
                if user_input.lower() == 'reset':
                    old_session = self.session_id
                    self.session_id = f"patch_{uuid.uuid4().hex[:12]}"
                    self.conversation_history = []
                    print(f"🔄 Session reset: {old_session} → {self.session_id}\n")
                    continue
                
                if user_input.lower() == 'history':
                    self.show_history()
                    continue
                
                if user_input.lower() == 'info':
                    self.show_info()
                    continue
                
                if user_input.lower() in ['help', '?']:
                    self._show_welcome()
                    continue
                
                # Send to agent
                response = self.chat(user_input)
                
                # Only print response if not already streamed
                if not response.startswith("[STREAMED]"):
                    print(f"\n🤖 Agent:\n{response}")
                
                print("\n" + "─" * 70 + "\n")
                
                message_count += 1
                
            except KeyboardInterrupt:
                print("\n")
                print("─" * 70)
                print("👋 Interrupted. Goodbye!")
                print("─" * 70)
                break
            except Exception as e:
                print(f"\n❌ Error: {e}\n")


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Patch Automation Chat Client - Trust the agent's intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Interactive chat
  %(prog)s -m "What vulnerabilities exist?"   # Single message
  %(prog)s -m "Handle CVE-2025-38527" -d      # Debug mode
  %(prog)s -p ri25-demo                       # Use AWS profile
  %(prog)s -m "Show compliance" -e report.json # Export to file
        """
    )
    
    parser.add_argument("-m", "--message", help="Send single message")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("-p", "--profile", help="AWS profile to use")
    parser.add_argument("-e", "--export", metavar="FILE", help="Export conversation to file")
    
    args = parser.parse_args()
    
    # Create client
    client = PatchAutomationClient(debug=args.debug, profile=args.profile)
    
    if args.message:
        # Single message mode
        print("🤖 Patch Automation Agent\n")
        response = client.chat(args.message, stream=True)
        
        # Only print if not already streamed
        if not response.startswith("[STREAMED]"):
            print(f"{response}")
        print()
        
        if args.export:
            filename = client.export_conversation(args.export)
            print(f"💾 Saved to {filename}")
    else:
        # Interactive chat mode
        client.chat_loop()


if __name__ == "__main__":
    main()

