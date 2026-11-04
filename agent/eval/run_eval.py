#!/usr/bin/env python3
"""Tool selection evaluation — pre-deploy gate.

Feeds system prompts + tool schemas to the model, presents test scenarios,
and validates the model selects the expected tool. Compares against a
baseline and reports accuracy with diffs.

Usage:
    python3 agent/eval/run_eval.py                    # run eval, compare to baseline
    python3 agent/eval/run_eval.py --threshold 85     # fail if accuracy < 85%
    python3 agent/eval/run_eval.py --update-baseline  # save current results as new baseline

Exit codes:
    0 = pass (accuracy >= threshold)
    1 = fail (accuracy < threshold or errors)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Add agent dir to path so we can import prompts
EVAL_DIR = Path(__file__).parent
AGENT_DIR = EVAL_DIR.parent
sys.path.insert(0, str(AGENT_DIR))

# ── Configuration ────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 80  # percent
def _runtime_model() -> str:
    from helper.agent_factory import _DEFAULT_MODEL
    return os.environ.get("BEDROCK_MODEL_ID") or _DEFAULT_MODEL


DEFAULT_MODEL = None  # resolved at runtime: BEDROCK_MODEL_ID, else the agent's default
SCENARIOS_FILE = EVAL_DIR / "scenarios.json"
BASELINE_FILE = EVAL_DIR / "baseline.json"

# Colors for terminal output
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


# ── Tool Schemas ─────────────────────────────────────────────────────────

def _unified_agent_tools() -> list:
    """Tool schemas exactly as the deployed agent sees them."""
    from supervisor import ALL_TOOLS
    return [{"toolSpec": t.tool_spec} for t in ALL_TOOLS]


def _unified_system_prompt() -> str:
    """System prompt exactly as the agent factory builds it."""
    from supervisor import SYSTEM_PROMPT
    return SYSTEM_PROMPT


# Write tools: a scenario that expects no tool passes only if none of these is called.
WRITE_TOOLS = {"execute_patch_operation", "multi_account_execute", "rollback_patches",
               "multi_account_rollback", "create_maintenance_window"}


# ── Evaluation Engine ────────────────────────────────────────────────────

def _converse(client, model_id: str, system_prompt: str, tools: list, messages: list) -> tuple[dict | None, dict]:
    """Single Bedrock converse call. Returns (first toolUse block or None, usage metrics).

    The metrics also carry the reply's text, for scenarios that check what the model says.
    """
    from helper.agent_factory import model_request_fields
    kwargs = {}
    fields = model_request_fields(model_id)
    if fields:
        # Same request shape as the runtime: adaptive thinking (and effort), no temperature.
        kwargs["additionalModelRequestFields"] = fields
        kwargs["inferenceConfig"] = {"maxTokens": 8000}
    else:
        kwargs["inferenceConfig"] = {"temperature": 0.0, "maxTokens": 1024}
    response = client.converse(
        modelId=model_id,
        messages=messages,
        system=[{"text": system_prompt}],
        toolConfig={"tools": tools},
        **kwargs,
    )
    usage = response.get("usage", {})
    metrics = {
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
        "latency_ms": response.get("metrics", {}).get("latencyMs", 0),
    }
    content = response.get("output", {}).get("message", {}).get("content", [])
    metrics["text"] = " ".join(b["text"] for b in content if "text" in b)
    for block in content:
        if "toolUse" in block:
            return block["toolUse"], metrics
    return None, metrics


def _check_params(actual_input: dict, expected_params: dict) -> list[str]:
    """Check that expected values appear in tool input. Returns list of failures."""
    failures = []
    for param_name, expected_values in expected_params.items():
        actual_value = actual_input.get(param_name)
        if actual_value is None:
            failures.append(f"missing param '{param_name}'")
            continue
        # Stringify for substring matching (handles arrays, bools, strings)
        actual_str = json.dumps(actual_value).lower() if not isinstance(actual_value, str) else actual_value.lower()
        for expected in expected_values:
            expected_str = str(expected).lower()
            if expected_str not in actual_str:
                failures.append(f"param '{param_name}' missing '{expected}' (got: {actual_value})")
    return failures


def _get_client(region: str):
    """Get a cached Bedrock runtime client."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 3}),
    )


def run_agent_eval(agent_name: str, system_prompt: str, tools: list, scenarios: list,
                   model_id: str, region: str) -> list[dict]:
    """Run all scenarios for one agent. Returns list of result dicts."""
    client = _get_client(region)
    results = []

    for scenario in scenarios:
        msg = scenario["message"]
        expected_tool = scenario["expected_tool"]
        expected_params = scenario.get("expected_params", {})
        assertions = scenario.get("assertions", [])
        desc = scenario["description"]

        try:
            messages = [{"role": "user", "content": [{"text": msg}]}]
            tool_use, metrics = _converse(client, model_id, system_prompt, tools, messages)
            metrics.pop("text", None)

            actual_tool = tool_use["name"] if tool_use else None
            actual_input = tool_use.get("input", {}) if tool_use else {}

            # Check 1: correct tool
            tool_correct = (actual_tool not in WRITE_TOOLS) if expected_tool is None else actual_tool == expected_tool

            # Check 2: correct parameters (only if tool is correct)
            param_failures = []
            if tool_correct and expected_params:
                param_failures = _check_params(actual_input, expected_params)

            # Check 3: response quality assertions (content-based)
            assertion_failures = []
            if tool_correct and assertions:
                actual_str = json.dumps(actual_input).lower()
                for assertion in assertions:
                    if assertion.get("contains"):
                        if assertion["contains"].lower() not in actual_str:
                            assertion_failures.append(f"missing: '{assertion['contains']}'")
                    if assertion.get("not_contains"):
                        if assertion["not_contains"].lower() in actual_str:
                            assertion_failures.append(f"should not contain: '{assertion['not_contains']}'")

            passed = tool_correct and not param_failures and not assertion_failures

            result = {
                "agent": agent_name,
                "message": msg,
                "expected_tool": expected_tool,
                "actual_tool": actual_tool,
                "tool_correct": tool_correct,
                "expected_params": expected_params,
                "actual_params": actual_input,
                "param_failures": param_failures,
                "assertion_failures": assertion_failures,
                "passed": passed,
                "description": desc,
                "metrics": metrics,
            }
            results.append(result)

        except Exception as e:
            results.append({
                "agent": agent_name,
                "message": msg,
                "expected_tool": expected_tool,
                "actual_tool": f"ERROR: {e}",
                "tool_correct": False,
                "param_failures": [],
                "assertion_failures": [],
                "passed": False,
                "description": desc,
            })

    return results


def run_multi_turn_eval(scenarios: list, system_prompt: str, tools: list,
                        model_id: str, region: str) -> list[dict]:
    """Run multi-turn confirmation scenarios. Returns list of result dicts."""
    client = _get_client(region)
    results = []

    for scenario in scenarios:
        desc = scenario["description"]
        expected_tool = scenario["expected_tool"]
        expected_params = scenario.get("expected_params", {})

        try:
            # Build Converse-API-compliant conversation history.
            # Rules: user/assistant must alternate, tool_use must be followed
            # by tool_result in the next user message.
            #
            # Expected scenario format:
            #   user("Patch i-xxx") → assistant(tool_use) → assistant(plan text) → user("yes")
            #
            # We transform this into valid Converse messages:
            #   user("Patch i-xxx")
            #   assistant(tool_use)
            #   user(tool_result with confirmation_required)
            #   assistant(plan text)
            #   user("yes")
            messages = []
            tool_use_counter = 0

            for i, turn in enumerate(scenario["messages"]):
                role = turn["role"]
                if role == "user":
                    text = turn.get("content", "")
                    if text:
                        messages.append({"role": "user", "content": [{"text": text}]})
                elif role == "assistant":
                    if turn.get("tool_use"):
                        tu = turn["tool_use"]
                        tool_use_counter += 1
                        tool_use_id = f"eval-tu-{tool_use_counter}"
                        # Assistant message with tool_use
                        content = []
                        if turn.get("content"):
                            content.append({"text": turn["content"]})
                        content.append({"toolUse": {
                            "toolUseId": tool_use_id,
                            "name": tu["name"],
                            "input": tu.get("input", {}),
                        }})
                        messages.append({"role": "assistant", "content": content})
                        # Immediately add the tool result: the scenario's own, else a plan card.
                        messages.append({"role": "user", "content": [
                            {"toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"json": turn.get("tool_result") or {
                                    "status": "plan_ready",
                                    "result_type": "plan_created",
                                    "plan_id": "0" * 32,
                                    "shown_as_card": True,
                                    "next_action": ("Nothing has run. The plan is shown to the operator as a card "
                                                    "with an Approve button. Do not ask them to type yes; approval "
                                                    "only happens on the card."),
                                }}],
                            }}
                        ]})
                    elif turn.get("content"):
                        # Plain text assistant message (presenting the plan)
                        messages.append({"role": "assistant", "content": [{"text": turn["content"]}]})

            # Get model's response to the final state
            tool_use, metrics = _converse(client, model_id, system_prompt, tools, messages)

            actual_tool = tool_use["name"] if tool_use else None
            actual_input = tool_use.get("input", {}) if tool_use else {}

            tool_correct = (actual_tool not in WRITE_TOOLS) if expected_tool is None else actual_tool == expected_tool
            param_failures = []
            if tool_correct and expected_params:
                param_failures = _check_params(actual_input, expected_params)

            # Prose checks on the reply, case-insensitive, after the tool check. A reply that
            # calls a read tool first hasn't answered yet, so there's nothing to check.
            text = metrics.pop("text", "").lower()
            assertion_failures = []
            for a in (scenario.get("answer_assertions", []) if not tool_use else []):
                if "contains" in a and a["contains"].lower() not in text:
                    assertion_failures.append(f"reply missing '{a['contains']}'")
                if "not_contains" in a and a["not_contains"].lower() in text:
                    assertion_failures.append(f"reply says '{a['not_contains']}'")
            if assertion_failures:
                assertion_failures.append(f"reply: {text[:600]}")

            passed = tool_correct and not param_failures and not assertion_failures

            results.append({
                "agent": "multi_turn",
                "message": scenario["messages"][-1].get("content", "(last turn)"),
                "expected_tool": expected_tool,
                "actual_tool": actual_tool,
                "tool_correct": tool_correct,
                "expected_params": expected_params,
                "actual_params": actual_input,
                "param_failures": param_failures,
                "assertion_failures": assertion_failures,
                "passed": passed,
                "description": desc,
                "metrics": metrics,
            })

        except Exception as e:
            results.append({
                "agent": "multi_turn",
                "message": desc,
                "expected_tool": expected_tool,
                "actual_tool": f"ERROR: {e}",
                "tool_correct": False,
                "param_failures": [],
                "passed": False,
                "description": desc,
            })

    return results


# ── Reporting ────────────────────────────────────────────────────────────

def print_results(results: list[dict], baseline: dict | None = None):
    """Print formatted results with baseline comparison."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    accuracy = (passed / total * 100) if total > 0 else 0

    # Breakdown stats
    tool_correct = sum(1 for r in results if r.get("tool_correct", r.get("passed")))
    param_issues = sum(1 for r in results if r.get("tool_correct") and r.get("param_failures"))

    # Baseline comparison
    baseline_accuracy = None
    if baseline:
        baseline_total = baseline.get("total", 0)
        baseline_passed = baseline.get("passed", 0)
        baseline_accuracy = (baseline_passed / baseline_total * 100) if baseline_total > 0 else 0

    print(f"\n{'='*60}")
    print("  Tool Selection & Parameter Eval Results")
    print(f"{'='*60}\n")

    # Per-agent breakdown
    agents = sorted(set(r["agent"] for r in results))
    for agent in agents:
        agent_results = [r for r in results if r["agent"] == agent]
        agent_passed = sum(1 for r in agent_results if r["passed"])
        agent_total = len(agent_results)
        status = f"{GREEN}PASS{NC}" if agent_passed == agent_total else f"{YELLOW}PARTIAL{NC}"
        print(f"  {agent}: {agent_passed}/{agent_total} {status}")

        for r in agent_results:
            if r["passed"]:
                print(f"    {GREEN}+{NC} {r['description']}")
            else:
                print(f"    {RED}x{NC} {r['description']}")
                if not r.get("tool_correct", True):
                    print(f"      tool: expected={r.get('expected_tool', r.get('expected'))}, got={r.get('actual_tool', r.get('actual'))}")
                if r.get("param_failures"):
                    for pf in r["param_failures"]:
                        print(f"      param: {pf}")
                if r.get("assertion_failures"):
                    for af in r["assertion_failures"]:
                        print(f"      assert: {af}")

    # Summary
    print(f"\n{'─'*60}")
    accuracy_color = GREEN if accuracy >= 85 else (YELLOW if accuracy >= 70 else RED)
    print(f"  Overall:    {accuracy_color}{passed}/{total} ({accuracy:.0f}%){NC}")
    print(f"  Tool route: {tool_correct}/{total} correct")
    if param_issues:
        print(f"  Params:     {YELLOW}{param_issues} scenario(s) with missing/wrong params{NC}")
    assertion_issues = sum(1 for r in results if r.get("assertion_failures"))
    if assertion_issues:
        print(f"  Assertions: {YELLOW}{assertion_issues} scenario(s) with content assertion failures{NC}")

    # Cost summary
    total_input = sum(r.get("metrics", {}).get("input_tokens", 0) for r in results)
    total_output = sum(r.get("metrics", {}).get("output_tokens", 0) for r in results)
    total_latency = sum(r.get("metrics", {}).get("latency_ms", 0) for r in results)
    if total_input > 0:
        print(f"\n  {'─'*40}")
        print(f"  Cost:   {total_input:,} in + {total_output:,} out = {total_input + total_output:,} tokens")
        print(f"  Latency: {total_latency/1000:.1f}s total ({total_latency/max(total,1)/1000:.1f}s avg/scenario)")

    if baseline_accuracy is not None:
        baseline_passed_n = baseline.get("passed", 0)
        baseline_total_n = baseline.get("total", 0)
        delta = accuracy - baseline_accuracy
        if delta > 0:
            print(f"  Baseline:   {baseline_passed_n}/{baseline_total_n} ({baseline_accuracy:.0f}%) — {GREEN}+{delta:.0f}% improvement{NC}")
        elif delta < 0:
            print(f"  Baseline:   {baseline_passed_n}/{baseline_total_n} ({baseline_accuracy:.0f}%) — {RED}{delta:.0f}% regression{NC}")
        else:
            print(f"  Baseline:   {baseline_passed_n}/{baseline_total_n} ({baseline_accuracy:.0f}%) — no change")

    print(f"{'='*60}\n")
    return accuracy


def load_baseline() -> dict | None:
    """Load baseline results if they exist."""
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    return None


def save_baseline(results: list[dict]):
    """Save current results as the new baseline."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    baseline = {
        "total": total,
        "passed": passed,
        "accuracy": (passed / total * 100) if total > 0 else 0,
        "results": results,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2))
    print(f"  {GREEN}Baseline saved:{NC} {BASELINE_FILE}")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tool selection eval — pre-deploy gate")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"Minimum accuracy %% to pass (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Save current results as new baseline")
    parser.add_argument("--model", default=os.environ.get("EVAL_MODEL_ID"),
                        help="Model ID for evaluation (default: the runtime's model)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"),
                        help="AWS region (default: us-east-1)")
    args = parser.parse_args()

    # Load scenarios
    scenarios = json.loads(SCENARIOS_FILE.read_text())

    # Prompt, tools and model exactly as the agent uses them
    args.model = args.model or _runtime_model()
    UNIFIED_PROMPT = _unified_system_prompt()

    total_scenarios = sum(len(v) for v in scenarios.values())
    print(f"\n{BLUE}Tool Selection & Parameter Eval{NC}")
    print(f"  Model:     {args.model}")
    print(f"  Region:    {args.region}")
    print(f"  Threshold: {args.threshold}%")
    print(f"  Scenarios: {total_scenarios} (tool selection + params + multi-turn)")

    # Run evals
    all_results = []

    print("\n  Running unified agent scenarios...", end="", flush=True)
    unified_results = run_agent_eval(
        "unified", UNIFIED_PROMPT, _unified_agent_tools(),
        scenarios["unified"], args.model, args.region,
    )
    all_results.extend(unified_results)
    print(f" done ({sum(1 for r in unified_results if r['passed'])}/{len(unified_results)})")

    # Multi-turn confirmation scenarios
    if scenarios.get("multi_turn"):
        print("  Running multi-turn scenarios...", end="", flush=True)
        mt_results = run_multi_turn_eval(
            scenarios["multi_turn"], UNIFIED_PROMPT,
            _unified_agent_tools(), args.model, args.region,
        )
        all_results.extend(mt_results)
        print(f" done ({sum(1 for r in mt_results if r['passed'])}/{len(mt_results)})")

    # Report
    baseline = load_baseline()
    accuracy = print_results(all_results, baseline)

    # Update baseline if requested
    if args.update_baseline:
        save_baseline(all_results)

    # Gate
    if accuracy < args.threshold:
        print(f"  {RED}FAIL:{NC} Accuracy {accuracy:.0f}% < threshold {args.threshold}%")
        print("  Deploy blocked. Fix regressions or run with --update-baseline if intentional.\n")
        sys.exit(1)
    else:
        print(f"  {GREEN}PASS:{NC} Accuracy {accuracy:.0f}% >= threshold {args.threshold}%\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
