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

class PatchClient:
    """Client for interacting with AWS Bedrock AgentCore patch automation agent.
    
    Manages conversation sessions, handles streaming responses, and provides
    rich terminal UI for agent interactions including tool usage indicators
    and formatted markdown output.
    """
    
    def __init__(self, profile: str = 'ri25-demo'):
        """Initialize patch automation client.
        
        Args:
            profile: AWS profile name for authentication
        """
        self.profile = profile
        self.session_id = f"patch-session-{uuid.uuid4().hex}"
        self.conversation_history = []
        self.console = Console()
        
        config_path = Path(__file__).parent.parent.parent / "agent" / ".bedrock_agentcore.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)
        
        default_agent = config['default_agent']
        agent_config = config['agents'][default_agent]
        
        self.agent_arn = agent_config['bedrock_agentcore']['agent_arn']
        self.region = agent_config['aws']['region']
        self.profile = profile
    
    def _get_client(self):
        """Create boto3 bedrock-agentcore client with configured timeouts.
        
        Returns:
            boto3 client configured for bedrock-agentcore service
        """
        from botocore.config import Config
        
        config = Config(
            read_timeout=300,
            connect_timeout=60
        )
        
        if self.profile:
            session = boto3.Session(profile_name=self.profile, region_name=self.region)
        else:
            session = boto3.Session(region_name=self.region)
        
        return session.client('bedrock-agentcore', config=config)
    
    def chat(self, message: str) -> str:
        """Send message to agent and display formatted response.
        
        Creates a fresh boto3 client with max_pool_connections=1 to prevent
        HTTP connection pooling corruption that causes truncated responses
        after multiple requests in the same session.
        
        Args:
            message: User prompt to send to the agent
            
        Returns:
            Complete agent response text including XML tags
        """
        import time
        start_time = time.time()
        
        payload = json.dumps({"prompt": message}).encode()
        
        from botocore.config import Config
        config = Config(
            read_timeout=300,
            connect_timeout=60,
            max_pool_connections=1
        )
        
        if self.profile:
            session = boto3.Session(profile_name=self.profile, region_name=self.region)
        else:
            session = boto3.Session(region_name=self.region)
        
        client = session.client('bedrock-agentcore', config=config)
        
        response = client.invoke_agent_runtime(
            agentRuntimeArn=self.agent_arn,
            runtimeSessionId=self.session_id,
            payload=payload
        )
        
        full_text = []
        current_tool = None
        first_content = True
        status = self.console.status("[bold cyan]🤔 Thinking...[/bold cyan]", spinner="dots")
        status.start()
        
        event_stream = response['response']
        
        try:
            buffer = b""
            text_buffer = ""
            chunk_size = 4096
            
            while True:
                chunk = event_stream.read(chunk_size)
                if not chunk:
                    break
                
                buffer += chunk
                
                try:
                    decoded = buffer.decode('utf-8')
                    text_buffer += decoded
                    buffer = b""
                except UnicodeDecodeError:
                    continue
                
                while '\n' in text_buffer:
                    line, text_buffer = text_buffer.split('\n', 1)
                    line = line.strip()
                    
                    if not line or not line.startswith('data: '):
                        continue
                    
                    json_str = line[6:]
                    
                    try:
                        data = json.loads(json_str)
                        
                        if not isinstance(data, dict):
                            continue
                        
                        if 'event' in data and 'contentBlockStart' in data['event']:
                            start = data['event']['contentBlockStart'].get('start', {})
                            if 'toolUse' in start:
                                tool_name = start['toolUse'].get('name')
                                if tool_name and tool_name != current_tool:
                                    status.stop()
                                    current_tool = tool_name
                                    self._print_tool_indicator(tool_name)
                                    status = self.console.status("🤔 Thinking...", spinner="dots")
                                    status.start()
                                    first_content = True
                        
                        if 'event' in data and 'contentBlockDelta' in data['event']:
                            delta = data['event']['contentBlockDelta'].get('delta', {})
                            if 'text' in delta:
                                text_content = delta['text']
                                full_text.append(text_content)
                    
                    except json.JSONDecodeError:
                        pass
        
        except Exception as e:
            import sys
            print(f"\n[Error: {e}]", file=sys.stderr)
        finally:
            try:
                event_stream.close()
            except:
                pass
        
        status.stop()
        
        result = ''.join(full_text)
        
        import re
        answer_match = re.search(r'<answer>(.*?)</answer>', result, re.DOTALL)
        if answer_match:
            display_text = answer_match.group(1).strip()
        else:
            display_text = result
            for tag in ['<answer>', '</answer>', '<thinking>', '</thinking>']:
                display_text = display_text.replace(tag, '')
        
        display_text = self._highlight_critical_info(display_text)
        
        if display_text.strip():
            self.console.print("🤖 [bold cyan]Agent:[/bold cyan]")
            self.console.print(Markdown(display_text.strip()))
        
        elapsed = time.time() - start_time
        self.console.print(f"[dim]⏱️  Response time: {elapsed:.1f}s[/dim]")
        self.console.print()
        
        self.conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "user": message,
            "agent": result
        })
        
        return result
    
    def _highlight_critical_info(self, text: str) -> str:
        """Apply syntax highlighting to critical information in response text.
        
        Highlights CVE IDs, AWS instance IDs, and severity levels for better
        visual scanning during demos and presentations.
        
        Args:
            text: Raw markdown text from agent response
            
        Returns:
            Text with Rich markup tags for highlighted elements
        """
        import re
        
        text = re.sub(r'\b(CVE-\d{4}-\d+)\b', r'**`\1`**', text)
        text = re.sub(r'\b(i-[a-f0-9]{17})\b', r'`\1`', text)
        text = re.sub(r'\b(CRITICAL|HIGH|MEDIUM|LOW)\b', r'**\1**', text)
        text = re.sub(r'\b(prod-[a-z0-9-]+)\b', r'`\1`', text)
        text = re.sub(r'\b(staging-[a-z0-9-]+)\b', r'`\1`', text)
        text = re.sub(r'\b(dev-[a-z0-9-]+)\b', r'`\1`', text)
        
        return text
    
    def _print_tool_indicator(self, tool_name: str):
        """Display styled indicator when agent invokes a specialist tool.
        
        Shows a brief spinner animation followed by a colored label indicating
        which specialist agent or service is being consulted.
        
        Args:
            tool_name: Name of the tool being invoked by the agent
        """
        indicators = {
            'consult_vulnerability_analyst': ('🔍 Consulting Vulnerability Analyst', 'red'),
            'consult_patch_manager': ('🛠️  Consulting Patch Manager', 'yellow'),
            'consult_compliance_analyst': ('📊 Consulting Compliance Analyst', 'blue'),
            'use_aws': ('⚙️  Consulting AWS Services', 'bright_yellow')
        }
        
        label, color = indicators.get(tool_name, (f'🔧 Consulting {tool_name}...', 'white'))
        
        with self.console.status(f"[bold {color}]{label}[/bold {color}]", spinner="dots"):
            import time
            time.sleep(0.5)
        
        self.console.print(f"[bold {color}]{label}[/bold {color}]\n")


def main():
    """Entry point for patch automation chat client.
    
    Supports both interactive mode (default) and single-message mode (-m flag).
    Displays welcome banner with available specialist agents and example prompts.
    """
    import argparse
    
    parser = argparse.ArgumentParser(description="Patch Automation Chat Client")
    parser.add_argument("-m", "--message", help="Send single message")
    parser.add_argument("-p", "--profile", help="AWS profile to use")
    args = parser.parse_args()
    
    client = PatchClient(profile=args.profile)
    console = client.console
    banner_content = (
        "[bold cyan]🤖 Patch Automation Agent[/bold cyan]\n"
        "[dim]Intelligent vulnerability-driven patch management[/dim]\n\n"
        "[bold yellow]Specialist Agents:[/bold yellow]\n"
        "  [red]� VulnePrability Analyst[/red] - CVE analysis, threat assessment, CVSS scoring\n"
        "  [yellow]�️  Piatch Manager[/yellow] - Infrastructure discovery, patching, health checks, rollback\n"
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
        console.print(Panel(args.message, title="[bold green]You[/bold green]", border_style="green", expand=True))
        console.print()
        client.chat(args.message)
        console.print()
    else:
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
