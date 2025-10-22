#!/usr/bin/env python3
"""Enhanced Patch Automation Chat Client with Rich Terminal UI"""

import json
import uuid
import boto3
import yaml
import os
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

console = Console()

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
        """Send message and get response with rich UI"""
        payload = json.dumps({"prompt": message}).encode()
        
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.agent_arn,
            runtimeSessionId=self.session_id,
            payload=payload
        )
        
        full_text = []
        current_tool = None
        buffer = ""
        first_content = True
        status = console.status("[bold cyan]🤔 Thinking...[/bold cyan]", spinner="dots")
        status.start()
        tool_called = False
        
        for line in response['response'].iter_lines():
            if not line:
                continue
            
            line_str = line.decode('utf-8') if isinstance(line, bytes) else line
            
            if line_str.startswith('data: '):
                line_str = line_str[6:]
            
            try:
                data = json.loads(line_str)
                
                if not isinstance(data, dict):
                    continue
                
                # Tool detection
                if 'event' in data and 'contentBlockStart' in data['event']:
                    start = data['event']['contentBlockStart'].get('start', {})
                    if 'toolUse' in start:
                        tool_name = start['toolUse'].get('name')
                        if tool_name and tool_name != current_tool:
                            status.stop()
                            current_tool = tool_name
                            self._print_tool_indicator(tool_name)
                            tool_called = True
                            # Restart spinner after tool indicator with thinking emoji
                            status = console.status("🤔 Thinking...", spinner="dots")
                            status.start()
                
                # Text content
                if 'event' in data and 'contentBlockDelta' in data['event']:
                    delta = data['event']['contentBlockDelta'].get('delta', {})
                    if 'text' in delta:
                        if first_content:
                            status.stop()
                            console.print("🤖 [bold cyan]Agent:[/bold cyan] ", end='')
                            first_content = False
                        
                        text_content = delta['text']
                        full_text.append(text_content)
                        buffer += text_content
                        
                        # Strip XML tags
                        display = buffer
                        for tag in ['<answer>', '</answer>', '<thinking>', '</thinking>']:
                            display = display.replace(tag, '')
                        
                        # Flush buffer
                        if len(buffer) > 20 or (display.strip() and not any(t in buffer for t in ['<answer', '</answer', '<thinking', '</thinking'])):
                            if display.strip():
                                console.print(display, end='', style="cyan")
                            buffer = ""
            
            except json.JSONDecodeError:
                pass
        
        status.stop()
        
        # Flush remaining
        if buffer.strip():
            display = buffer
            for tag in ['<answer>', '</answer>', '<thinking>', '</thinking>']:
                display = display.replace(tag, '')
            if display.strip():
                console.print(display, end='', style="cyan")
        
        console.print()
        result = ''.join(full_text)
        
        self.conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "user": message,
            "agent": result
        })
        
        return result
    
    def _print_tool_indicator(self, tool_name: str):
        """Print which tool is being used with rich styling"""
        indicators = {
            'consult_vulnerability_analyst': ('🔍 Consulting Vulnerability Analyst', 'red'),
            'consult_patch_manager': ('🛠️  Consulting Patch Manager', 'yellow'),
            'consult_compliance_analyst': ('📊 Consulting Compliance Analyst', 'blue'),
            'use_aws': ('⚙️  Consulting AWS Services', 'bright_yellow')
        }
        
        label, color = indicators.get(tool_name, (f'🔧 Consulting {tool_name}', 'white'))
        
        # Show spinner while thinking
        with console.status(f"[bold {color}]{label}...[/bold {color}]", spinner="dots"):
            import time
            time.sleep(0.5)  # Brief pause to show spinner
        
        console.print(f"[bold {color}]{label}[/bold {color}]\n")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Patch Automation Chat Client")
    parser.add_argument("-m", "--message", help="Send single message")
    parser.add_argument("-p", "--profile", help="AWS profile to use")
    args = parser.parse_args()
    
    client = PatchClient(profile=args.profile)
    
    # Welcome banner - full width with agents and examples
    banner_content = (
        "[bold cyan]🤖 Patch Automation Agent[/bold cyan]\n"
        "[dim]Intelligent vulnerability-driven patch management[/dim]\n\n"
        "[bold yellow]Specialist Agents:[/bold yellow]\n"
        "  [red]🔍 Vulnerability Analyst[/red] - CVE analysis, threat assessment, CVSS scoring\n"
        "  [yellow]🛠️  Patch Manager[/yellow] - Infrastructure discovery, patching, health checks, rollback\n"
        "  [blue]📊 Compliance Analyst[/blue] - SLA tracking, audit trails, executive reporting\n"
        "  [bright_yellow]⚙️  AWS Services[/bright_yellow] - Lambda, S3, CloudWatch, RDS, and more\n\n"
        "[bold yellow]Example Prompts:[/bold yellow]\n"
        "  • What critical vulnerabilities do we have?\n"
        "  • Handle CVE-2025-38527 in dev environment\n"
        "  • Show me patch compliance status\n"
        "  • List EC2 instances in staging\n\n"
        "[bold yellow]Commands:[/bold yellow] [dim]quit, exit[/dim]"
    )
    
    banner = Panel(banner_content, border_style="cyan", expand=True)
    console.print(banner)
    console.print()
    
    if args.message:
        # Single message mode
        console.print(Panel(args.message, title="[bold green]You[/bold green]", border_style="green", expand=True))
        console.print()
        client.chat(args.message)
        console.print()
    else:
        # Interactive mode
        while True:
            try:
                user_input = console.input("[bold green]You:[/bold green] ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ['quit', 'exit']:
                    console.print("\n[bold yellow]👋 Goodbye![/bold yellow]")
                    break
                
                console.print()
                client.chat(user_input)
                console.print()
            
            except KeyboardInterrupt:
                console.print("\n\n[bold yellow]👋 Goodbye![/bold yellow]")
                break
            except Exception as e:
                console.print(f"\n[bold red]❌ Error:[/bold red] {e}\n")


if __name__ == "__main__":
    main()
