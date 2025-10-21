#!/usr/bin/env python3
"""Simplified Patch Automation Chat Client"""

import json
import uuid
import boto3
import yaml
import os
from datetime import datetime
from pathlib import Path

class PatchClient:
    def __init__(self, profile: str = 'ri25-demo'):
        self.profile = profile
        self.session_id = f"patch-session-{uuid.uuid4().hex}"  # 45 chars total
        self.conversation_history = []
        
        # Load agent config
        config_path = Path(__file__).parent.parent.parent / "agent" / ".bedrock_agentcore.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)
        
        default_agent = config['default_agent']
        agent_config = config['agents'][default_agent]
        
        self.agent_arn = agent_config['bedrock_agentcore']['agent_arn']
        self.region = agent_config['aws']['region']
        
        # Create boto3 session
        if profile:
            session = boto3.Session(profile_name=profile, region_name=self.region)
        else:
            session = boto3.Session(region_name=self.region)
        
        self.client = session.client('bedrock-agentcore')
    
    def chat(self, message: str) -> str:
        """Send message and get response"""
        payload = json.dumps({"prompt": message}).encode()
        
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.agent_arn,
            runtimeSessionId=self.session_id,
            payload=payload
        )
        
        # Parse streaming response - response['response'] is a StreamingBody
        full_text = []
        current_tool = None
        buffer = ""  # Buffer to handle XML tags at start
        
        # Read lines from streaming body (SSE format)
        for line in response['response'].iter_lines():
            if not line:
                continue
            
            line_str = line.decode('utf-8') if isinstance(line, bytes) else line
            
            # Strip SSE "data: " prefix
            if line_str.startswith('data: '):
                line_str = line_str[6:]
            
            try:
                data = json.loads(line_str)
                
                # Skip if not a dict
                if not isinstance(data, dict):
                    continue
                
                # Tool detection
                if 'event' in data and 'contentBlockStart' in data['event']:
                    start = data['event']['contentBlockStart'].get('start', {})
                    if 'toolUse' in start:
                        tool_name = start['toolUse'].get('name')
                        if tool_name and tool_name != current_tool:
                            current_tool = tool_name
                            self._print_tool_indicator(tool_name)
                
                # Text content
                if 'event' in data and 'contentBlockDelta' in data['event']:
                    delta = data['event']['contentBlockDelta'].get('delta', {})
                    if 'text' in delta:
                        text_content = delta['text']
                        full_text.append(text_content)
                        
                        # Add to buffer
                        buffer += text_content
                        
                        # Strip XML tags from buffer
                        display = buffer
                        for tag in ['<answer>', '</answer>', '<thinking>', '</thinking>']:
                            display = display.replace(tag, '')
                        
                        # Print if: buffer is large enough, or we have content and no pending tags
                        should_flush = (
                            len(buffer) > 20 or  # Buffer is large
                            (display.strip() and not any(tag in buffer for tag in ['<answer', '</answer', '<thinking', '</thinking']))  # Content ready, no partial tags
                        )
                        
                        if should_flush:
                            if display.strip():
                                print(display, end='', flush=True)
                            buffer = ""
            
            except json.JSONDecodeError:
                pass
        
        # Flush any remaining buffer
        if buffer.strip():
            display = buffer
            for tag in ['<answer>', '</answer>', '<thinking>', '</thinking>']:
                display = display.replace(tag, '')
            if display.strip():
                print(display, end='', flush=True)
        
        print()  # Newline after response
        result = ''.join(full_text)
        
        # Store in history
        self.conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "user": message,
            "agent": result
        })
        
        return result
    
    def _print_tool_indicator(self, tool_name: str):
        """Print which tool is being used"""
        indicators = {
            'consult_vulnerability_analyst': '\n🔍 Consulting Vulnerability Analyst...\n\n🤖 Agent: ',
            'consult_patch_manager': '\n🛠️  Consulting Patch Manager...\n\n🤖 Agent: ',
            'consult_compliance_analyst': '\n📊 Consulting Compliance Analyst...\n\n🤖 Agent: ',
            'use_aws': '\n⚙️  Querying AWS...\n\n🤖 Agent: '
        }
        print(indicators.get(tool_name, f'\n🔧 Using {tool_name}...\n\n🤖 Agent: '), end='', flush=True)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Patch Automation Chat Client")
    parser.add_argument("-m", "--message", help="Send single message")
    parser.add_argument("-p", "--profile", help="AWS profile to use")
    args = parser.parse_args()
    
    client = PatchClient(profile=args.profile)
    
    if args.message:
        print("🤖 Patch Automation Agent\n")
        client.chat(args.message)
        print()
    else:
        print("🤖 Patch Automation Agent - Interactive Mode\n")
        print("Type 'quit' to exit\n")
        
        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ['quit', 'exit']:
                    print("\n👋 Goodbye!")
                    break
                
                client.chat(user_input)
                print()
            
            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}\n")


if __name__ == "__main__":
    main()
