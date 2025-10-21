#!/usr/bin/env python3
"""View performance metrics from CloudWatch Logs."""

import boto3
import json
import sys
from datetime import datetime, timedelta
from collections import defaultdict

def get_logs(log_group, hours=1, profile='ri25-demo'):
    """Fetch logs from CloudWatch."""
    session = boto3.Session(profile_name=profile)
    logs = session.client('logs', region_name='us-east-1')
    
    start_time = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    
    try:
        response = logs.filter_log_events(
            logGroupName=log_group,
            startTime=start_time,
            filterPattern='metric_type'
        )
        return response.get('events', [])
    except Exception as e:
        print(f"Error fetching logs: {e}")
        return []

def parse_metrics(events):
    """Parse metrics from log events."""
    metrics = {
        'cache_hits': 0,
        'cache_misses': 0,
        'aws_calls': [],
        'queries': [],
        'tools': defaultdict(lambda: {'count': 0, 'total_ms': 0})
    }
    
    for event in events:
        message = event['message']
        
        # Try to parse structured log
        if 'metric_type' in message:
            try:
                # Extract JSON from message
                if '{' in message:
                    json_start = message.index('{')
                    data = json.loads(message[json_start:])
                    
                    metric_type = data.get('metric_type')
                    
                    if metric_type == 'cache':
                        if data.get('result') == 'hit':
                            metrics['cache_hits'] += 1
                        else:
                            metrics['cache_misses'] += 1
                    
                    elif metric_type == 'aws_api':
                        metrics['aws_calls'].append({
                            'service': data.get('service'),
                            'operation': data.get('operation'),
                            'duration_ms': data.get('duration_ms')
                        })
                    
                    elif metric_type == 'query':
                        metrics['queries'].append({
                            'query': data.get('query'),
                            'duration_ms': data.get('duration_ms')
                        })
                    
                    elif metric_type == 'tool':
                        tool_name = data.get('tool_name')
                        metrics['tools'][tool_name]['count'] += 1
                        metrics['tools'][tool_name]['total_ms'] += data.get('duration_ms', 0)
            
            except (json.JSONDecodeError, ValueError):
                pass
    
    return metrics

def display_metrics(metrics):
    """Display metrics in a readable format."""
    print("\n" + "="*60)
    print("PERFORMANCE METRICS")
    print("="*60)
    
    # Cache metrics
    total_cache = metrics['cache_hits'] + metrics['cache_misses']
    if total_cache > 0:
        hit_rate = (metrics['cache_hits'] / total_cache) * 100
        print(f"\n📦 CACHE PERFORMANCE")
        print(f"   Hits: {metrics['cache_hits']}")
        print(f"   Misses: {metrics['cache_misses']}")
        print(f"   Hit Rate: {hit_rate:.1f}%")
    
    # AWS API calls
    if metrics['aws_calls']:
        print(f"\n☁️  AWS API CALLS ({len(metrics['aws_calls'])} total)")
        by_service = defaultdict(lambda: {'count': 0, 'total_ms': 0})
        for call in metrics['aws_calls']:
            service = call['service']
            by_service[service]['count'] += 1
            by_service[service]['total_ms'] += call['duration_ms']
        
        for service, stats in sorted(by_service.items()):
            avg_ms = stats['total_ms'] / stats['count']
            print(f"   {service}: {stats['count']} calls, avg {avg_ms:.0f}ms")
    
    # Tool usage
    if metrics['tools']:
        print(f"\n🔧 TOOL USAGE")
        for tool, stats in sorted(metrics['tools'].items()):
            avg_ms = stats['total_ms'] / stats['count'] if stats['count'] > 0 else 0
            print(f"   {tool}: {stats['count']} calls, avg {avg_ms:.0f}ms")
    
    # Query performance
    if metrics['queries']:
        print(f"\n⏱️  QUERY PERFORMANCE ({len(metrics['queries'])} queries)")
        durations = [q['duration_ms'] for q in metrics['queries']]
        print(f"   Min: {min(durations):.0f}ms")
        print(f"   Max: {max(durations):.0f}ms")
        print(f"   Avg: {sum(durations)/len(durations):.0f}ms")
        
        # Show recent queries
        print(f"\n   Recent queries:")
        for q in metrics['queries'][-5:]:
            query_text = q['query'][:50] + "..." if len(q['query']) > 50 else q['query']
            print(f"   - {query_text} ({q['duration_ms']:.0f}ms)")
    
    print("\n" + "="*60 + "\n")

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='View agent performance metrics')
    parser.add_argument('--log-group', default='/aws/bedrock-agentcore/runtimes/intelligent_patch_automation_praveen-90LFV03r8h-DEFAULT',
                       help='CloudWatch log group name')
    parser.add_argument('--hours', type=int, default=1,
                       help='Hours of logs to fetch (default: 1)')
    parser.add_argument('--profile', default='ri25-demo',
                       help='AWS profile name')
    
    args = parser.parse_args()
    
    print(f"Fetching metrics from last {args.hours} hour(s)...")
    events = get_logs(args.log_group, args.hours, args.profile)
    
    if not events:
        print("No metrics found. Make sure:")
        print("1. Agent has been invoked recently")
        print("2. Observability is enabled")
        print("3. Log group name is correct")
        return
    
    print(f"Found {len(events)} log events")
    metrics = parse_metrics(events)
    display_metrics(metrics)

if __name__ == '__main__':
    main()
