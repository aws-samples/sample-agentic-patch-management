# Tool Selection Eval

Pre-deploy gate that validates the model selects the correct tool for each scenario.

## How it works

1. Loads system prompts from the actual agent code
2. Sends each scenario to Bedrock with tool schemas (no tool execution)
3. Checks if the model's first tool call matches the expected tool
4. Compares against `baseline.json` and reports accuracy delta
5. Blocks deployment if accuracy drops below threshold

## Usage

```bash
# Run eval (default 80% threshold)
python3 agent/eval/run_eval.py

# Tighten threshold
python3 agent/eval/run_eval.py --threshold 90

# Update baseline after intentional prompt changes
python3 agent/eval/run_eval.py --update-baseline

# Use a different model
python3 agent/eval/run_eval.py --model us.anthropic.claude-haiku-4-5-20251001

# Skip during deploy (fast iteration)
SKIP_EVAL=true ./deploy.sh agent
```

## Files

| File | Purpose |
|------|---------|
| `run_eval.py` | Standalone eval script — the deploy gate |
| `scenarios.json` | Test cases (message + expected tool + description) |
| `baseline.json` | Last-known-good results (git-tracked) |

## Adding scenarios

Edit `scenarios.json`. Each scenario needs:

```json
{
  "message": "What the operator would say",
  "expected_tool": "tool_name_the_model_should_call",
  "description": "Human-readable explanation for the report"
}
```

After adding scenarios, run with `--update-baseline` to save the new baseline.

## Deploy integration

`deploy.sh` calls `run_eval_gate()` before `deploy_agent()`. The gate:
- Skips if `SKIP_EVAL=true`
- Skips if `agent/eval/run_eval.py` doesn't exist
- Blocks deploy if accuracy < `EVAL_THRESHOLD` (default 80%)

## Cost

~20 Bedrock converse calls at ~500 input tokens each. Roughly $0.05-0.10 per run, ~30 seconds total.
