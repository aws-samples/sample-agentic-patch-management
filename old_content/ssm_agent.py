#!/usr/bin/env python3
import os
import json
from strands import Agent
from strands_tools import use_aws
from dotenv import load_dotenv
from strands.models.bedrock import BedrockModel
from ssm_queries import COMMON_QUERIES, get_query_suggestions, get_query_by_key

class SSMAgent:
    def __init__(self):
        load_dotenv()
        
        # Defaults
        self.config = {
            "aws_region": "us-east-1",
            "aws_profile": None,
            "max_tokens": 2048,
            "model_id": "amazon.nova-lite-v1",
            "temperature": 0.3
        }
        
        # Override with config.json if exists
        if os.path.exists("config.json"):
            with open("config.json", 'r') as f:
                self.config.update(json.load(f))
        
        self.agent = Agent(
            model=BedrockModel(
                max_tokens=self.config["max_tokens"],
                model_id=self.config["model_id"],
                temperature=self.config["temperature"]
            ),
            tools=[use_aws]
        )
    
    def execute_query(self, query: str) -> str:
        try:
            context = f"AWS region {self.config['aws_region']}"
            if self.config["aws_profile"]:
                context += f", profile {self.config['aws_profile']}"
            context += ". Be concise, no suggestions."
            
            return str(self.agent(f"{context} {query}"))
        except Exception as e:
            return f"Error: {e}"
    
    def interactive_mode(self):
        print("SSM Agent")
        print(f"Region: {self.config['aws_region']}")
        if self.config["aws_profile"]:
            print(f"Profile: {self.config['aws_profile']}")
        print("Commands: help, list, use <key>, dashboard, config, quit")
        
        while True:
            try:
                cmd = input("\nssm_agent> ").strip()
                if not cmd:
                    continue
                
                if cmd in ['quit', 'q', 'exit']:
                    break
                elif cmd == 'help':
                    print("Templates:\n" + get_query_suggestions())
                elif cmd == 'list':
                    print("Templates:\n" + get_query_suggestions())
                elif cmd == 'config':
                    for k, v in self.config.items():
                        print(f"  {k}: {v}")
                elif cmd == 'dashboard':
                    query = "Get comprehensive patch compliance dashboard showing overall compliance percentage, breakdown by severity, non-compliant instances, and security posture for all managed instances"
                    print(self.execute_query(query))
                elif cmd.startswith('use '):
                    key = cmd[4:].strip()
                    query = get_query_by_key(key)
                    if query.startswith("Unknown"):
                        print(f"Unknown key: {key}")
                    else:
                        print(self.execute_query(query))
                else:
                    print(self.execute_query(cmd))
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    SSMAgent().interactive_mode()
