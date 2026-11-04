# Tool-selection eval

Checks that the model picks the right tool for typical requests. `./deploy.sh agent` runs it before deploying the agent and stops below the threshold; a full `./deploy.sh` doesn't run it.

Tool descriptions are the agent's routing instructions, so editing a docstring, the system prompt or the tool list can change which tool the model picks. Run the eval after any of those changes.

## How it works

`run_eval.py` loads the system prompt, the tool specs and the model the agent uses, from this checkout. It then sends each scenario to Bedrock with those tools, without running any of them, and checks the model's response:

- the first tool call matches `expected_tool`. With `"expected_tool": null`, the scenario passes only if no write tool is called. The multi-turn scenarios use this to check that a typed "yes" after a plan does nothing;
- the call's arguments contain each substring in `expected_params`;
- the call's arguments, as text, meet each `assertions` entry (`contains`, `not_contains`).
- in a conversation, the model's reply meets each `answer_assertions` entry (`contains`, `not_contains`, case-insensitive), unless it calls a read tool first. A tool turn can carry its own `tool_result`; without one it gets a plan card. The prose scenarios use this to check that the reply follows the tool result: a plan that runs now isn't called an emergency, a refusal is relayed with its reason, and a stopped run isn't called failed.

It prints each result and the overall accuracy, compares them with `baseline.json`, and exits non-zero below the threshold.

## Running it

It needs AWS credentials with Bedrock access to the model. In a fresh clone, create the virtual environment first; `./deploy.sh` creates the same one.

```bash
python3 -m venv venv && venv/bin/pip install -r agent/requirements.txt

venv/bin/python agent/eval/run_eval.py                    # default threshold 80%
venv/bin/python agent/eval/run_eval.py --threshold 90
venv/bin/python agent/eval/run_eval.py --update-baseline  # after an intended change
venv/bin/python agent/eval/run_eval.py --model <model-id> --region us-west-2
```

The model is `--model`, else `EVAL_MODEL_ID`, else `BEDROCK_MODEL_ID`, else the agent's default. Run on its own, the script doesn't read `.env`, so export those settings first. `SKIP_EVAL=true ./deploy.sh agent` skips the gate, and `EVAL_THRESHOLD` sets it.

There are 20 scenarios: 15 single requests in `unified` and 5 conversations in `multi_turn`. A run takes about 40 seconds and sends about 155,000 input tokens in total, with adaptive thinking on.

## Adding a scenario

Add an entry to `unified` in `scenarios.json`:

```json
{
  "message": "What vulnerabilities are in staging?",
  "expected_tool": "get_vulnerability_findings",
  "expected_params": {"environment": ["staging"]},
  "assertions": [{"not_contains": "rollback"}],
  "description": "Vulnerability query routes to get_vulnerability_findings"
}
```

`expected_params` and `assertions` are optional. Then run with `--update-baseline` and commit `baseline.json` with the change.
