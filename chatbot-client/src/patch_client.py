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
from datetime import datetime
from typing import Optional

class PatchAutomationClient:
    """Minimal chat client - trust the agent's intelligence"""
    
    def __init__(self, debug: bool = False):
        """Initialize client"""
        self.debug = debug
        self.conversation_history = []
        self.session_id = f"patch_{uuid.uuid4().hex[:12]}"
        
        if self.debug:
            print(f"🔧 Debug Mode: ON")
            print(f"🔧 Session: {self.session_id}\n")
    
    def chat(self, message: str) -> str:
        """Send message to agent - it decides what to do"""
        payload = {"prompt": message, "session_id": self.session_id}
        
        if self.debug:
            print(f"\n[DEBUG] Payload:\n{json.dumps(payload, indent=2)}")
            print(f"[DEBUG] Invoking agent...")
        
        # Use streaming to show real-time progress
        try:
            process = subprocess.Popen(
                ["agentcore", "invoke", json.dumps(payload)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            # Show progress as agent works
            output_lines = []
            last_status = ""
            
            print("🤔 Starting...", end="", flush=True)
            
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                    
                output_lines.append(line)
                
                # Parse streaming events to show progress
                status = self._parse_progress(line)
                if status and status != last_status:
                    # Clear previous status and show new one
                    print("\r" + " " * 80 + "\r", end="", flush=True)
                    print(f"{status}", end="", flush=True)
                    last_status = status
            
            # Wait for process to complete
            process.wait(timeout=300)
            
            # Clear status line
            print("\r" + " " * 80 + "\r", end="", flush=True)
            
            raw_output = ''.join(output_lines)
            
            if self.debug:
                print(f"[DEBUG] Response received ({len(raw_output)} chars)")
            
            response = self._extract_clean_response(raw_output)
            
            # Store in history
            self.conversation_history.append({
                "timestamp": datetime.now().isoformat(),
                "user": message,
                "agent": response
            })
            
            return response
            
        except subprocess.TimeoutExpired:
            print("\r" + " " * 80 + "\r", end="", flush=True)
            return "⏱️  Agent timeout (5 min). Try a simpler query or check agent status."
        
        except subprocess.CalledProcessError as e:
            print("\r" + " " * 80 + "\r", end="", flush=True)
            error = e.stderr.strip() if e.stderr else "Unknown error"
            
            if self.debug:
                print(f"\n[DEBUG] Error: {error}")
            
            # Friendly error messages
            if "not found" in error.lower():
                return "❌ AgentCore CLI not found. Install: pip install bedrock-agentcore"
            elif "not deployed" in error.lower():
                return "❌ Agent not deployed. Run: ./deploy.sh agent"
            else:
                return f"❌ Agent error: {error[:200]}"
        
        except FileNotFoundError:
            print("\r" + " " * 80 + "\r", end="", flush=True)
            return "❌ AgentCore CLI not found. Install: pip install bedrock-agentcore"
        
        except Exception as e:
            print("\r" + " " * 80 + "\r", end="", flush=True)
            if self.debug:
                print(f"\n[DEBUG] Unexpected error: {e}")
            return f"❌ Unexpected error: {str(e)[:200]}"
    
    def _parse_progress(self, line: str) -> Optional[str]:
        """Parse streaming output to show what agent is doing"""
        try:
            # Skip debug/internal JSON lines
            if line.strip().startswith('{') and any(x in line for x in ['"init_event_loop"', '"start_event_loop"', '"event":', '"data":', "'agent':"]):
                return None
            
            # Look for agent consultations
            if 'consult_vulnerability_analyst' in line:
                return "🔍 Consulting Vulnerability Analyst..."
            elif 'consult_patch_manager' in line:
                return "🛠️  Consulting Patch Manager..."
            
            # Look for tool usage (generic)
            if '"tool":' in line or '"name":' in line:
                return "⚙️  Using tools..."
            
            # Look for response generation
            if '"type": "text"' in line and len(line) > 100:
                return "✍️  Generating response..."
            
            # Look for completion
            if '"stop_reason":' in line or '"finish_reason":' in line:
                return "✅ Complete"
            
        except Exception:
            pass
        
        return None
    
    def _extract_clean_response(self, raw_output: str) -> str:
        """Extract clean response from streaming output"""
        try:
            # Collect all text deltas from streaming output
            collected_text = []
            lines = raw_output.split('\n')
            
            for line in lines:
                if not line.strip():
                    continue
                
                # Look for contentBlockDelta events with text
                if '"contentBlockDelta"' in line and '"text"' in line:
                    try:
                        # Extract text from JSON event
                        import re
                        text_match = re.search(r'"text":\s*"([^"]*(?:\\.[^"]*)*)"', line)
                        if text_match:
                            text = text_match.group(1)
                            # Unescape JSON
                            text = text.replace('\\n', '\n').replace('\\"', '"')
                            collected_text.append(text)
                    except Exception:
                        pass
            
            # Join all collected text
            if collected_text:
                full_response = ''.join(collected_text).strip()
                
                # Check for answer tags
                if '<answer>' in full_response and '</answer>' in full_response:
                    start = full_response.find('<answer>') + 8
                    end = full_response.find('</answer>')
                    return full_response[start:end].strip()
                
                return full_response
            
            # Fallback: return truncated raw output
            if self.debug:
                print(f"\n[DEBUG] Could not parse response")
            return "No response received. Try again or check agent status."
            
        except Exception as e:
            if self.debug:
                print(f"\n[DEBUG] Extraction error: {e}")
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
        print("📝 Commands: 'export' to save, 'quit' to exit")
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
                    print(f"👋 Session ended. {message_count} messages exchanged.")
                    if message_count > 0:
                        export = input("💾 Export conversation? (y/n): ").strip().lower()
                        if export == 'y':
                            filename = self.export_conversation()
                            print(f"✅ Saved to {filename}")
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
                
                if user_input.lower() in ['help', '?']:
                    self._show_welcome()
                    continue
                
                # Send to agent
                response = self.chat(user_input)
                print(f"\n🤖 Agent:\n{response}\n")
                
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
  %(prog)s -m "Show compliance" -e report.json # Export to file
        """
    )
    
    parser.add_argument("-m", "--message", help="Send single message")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("-e", "--export", metavar="FILE", help="Export conversation to file")
    
    args = parser.parse_args()
    
    # Create client
    client = PatchAutomationClient(debug=args.debug)
    
    if args.message:
        # Single message mode
        print("🤖 Patch Automation Agent\n")
        response = client.chat(args.message)
        print(f"{response}\n")
        
        if args.export:
            filename = client.export_conversation(args.export)
            print(f"💾 Saved to {filename}")
    else:
        # Interactive chat mode
        client.chat_loop()


if __name__ == "__main__":
    main()

