## Disclaimer

> This application is not designed for production use. Treat it as a proof-of-concept for exploring AI agent use cases.

> Be mindful of what you type into it. Your inputs get processed and stored across several AWS services (Amazon Bedrock, Bedrock AgentCore Memory, Amazon S3, Amazon CloudWatch Logs, Amazon Cognito, and others), and handling that information appropriately is on you as the deployer.

---

# Intelligent Patch Automation for AWS

Patching is death by a thousand small decisions. Is this CVE actually urgent? Which instances does it touch? Is there a maintenance window that still lands inside the SLA? What order do the environments roll out in? Did the patch even work? Where does the compliance report go? You can script every one of those answers into Lambdas and runbooks, but that logic is brittle and painful to change the moment your fleet or your policies shift.

This repo takes a different approach. It's a ready-to-deploy AI agent that manages EC2 patching from a natural-language prompt. You describe the outcome you want ("Handle CVE-2025-38477 in dev") and the agent works out the plan: it triages the CVE, figures out which instances are affected, weighs the SLA against the next maintenance window, previews the change, waits for your approval, patches, verifies, and files the compliance report.

Under the hood it leans on the [Strands Agents SDK](https://github.com/strands-agents/sdk-python) for the runtime and tool orchestration, and [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core.html) for hosting, session memory, and observability. The interesting parts are the patterns you can't easily lift from a docs page: deterministic steering handlers that inspect tool calls before they run, `next_action` hints that keep the prompt lean, dry-run/execute confirmation loops, and per-user memory isolation tied to Cognito identity.

![Demo](docs/images/demo.gif)

## What it does

- Handles patch workflows in plain English. Ask about a CVE, an SLA impact, or fleet coverage, and the agent decides whether to patch now or defer to a maintenance window.
- Runs as a single agent. One agent picks from 25 tools directly, steered by `Decision:` docstrings, `next_action` hints, and workflow handlers rather than a routing layer.
- Reaches across accounts and regions. It fans out through SSM Automation `TargetLocations` — one control plane driving N spoke accounts.
- Keeps a paper trail. Every operation writes a compliance report to S3 with framework-aware SLA tracking (PCI-DSS, HIPAA, or your own).
- Puts safety gates in the path. Dry-run preview, per-environment approval, and rollback with a three-check verification.
- Ships with a UI. A Cognito-authenticated dashboard for fleet visibility, plus a chat panel for driving the agent.

---

## Architecture at a glance

![Architecture diagram](docs/images/architecture.jpg)

### How a request flows through it

1. You open the UI URL. The request hits an Application Load Balancer, which authenticates you through Amazon Cognito.
2. The ALB forwards to an ECS Fargate service running the React frontend and a FastAPI proxy.
3. Fargate calls `invoke_agent_runtime` on Amazon Bedrock AgentCore and streams the response back to your browser.
4. Inside AgentCore, the Strands framework runs a single agent. It reads your message and picks from 25 tools to get the job done. Those tools cover the whole workflow:
   - Vulnerability analysis — pulls CVE findings from Amazon Inspector, sizes up fleet-wide impact, and maps which instances are affected across environments.
   - Patching — checks maintenance windows and existing patch policies, runs dry-run scans, executes through SSM Automation (with cross-account `TargetLocations` fan-out), verifies health, and rolls back when needed.
   - Compliance — reads the S3 compliance reports for SLA status, breach history, and trends.
5. The agent runs an agentic loop. It hands context to the LLM (Amazon Bedrock), gets back reasoning and a tool choice, runs that tool (hitting Amazon Inspector, AWS Systems Manager, Amazon S3, or Amazon EC2 through boto3), feeds the result back, and repeats until the task is done or a decision needs your approval. Tool selection stays on track thanks to `Decision:` docstrings, `next_action` hints, and steering handlers instead of a separate routing component.
6. Results stream back through AgentCore to Fargate to your browser in real time. Compliance reports land in Amazon S3, and conversation memory persists in AgentCore Memory.

The full write-up, including the design decisions behind these choices, lives in [`docs/architecture.md`](docs/architecture.md).

## Prerequisites

Local tooling:
- Python 3.11+
- Node.js 18+
- Docker or [Finch](https://github.com/runfinch/finch)
- AWS CLI v2

AWS account setup, in the target region, before you run `deploy.sh`:

1. [Enable Amazon Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access-modify.html) for Claude Sonnet.
2. Run AWS Systems Manager Quick Setup:
   - [Default Host Management Configuration](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-default-host-management-configuration.html)
   - [Host Management](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-host-management.html) (entire org, all regions)
   - [Config Recording](https://docs.aws.amazon.com/systems-manager/latest/userguide/quick-setup-config.html) (entire org, all regions)
3. Enable [SSM Explorer](https://docs.aws.amazon.com/systems-manager/latest/userguide/Explorer-setup.html).
4. Enable [Amazon Inspector](https://docs.aws.amazon.com/inspector/latest/user/getting_started_tutorial.html) for EC2 scanning.
5. Tag each managed EC2 instance:
   - `PatchAutomation` = `enabled` — required. Instances without this tag are invisible to the agent.
   - `Environment` = `dev` / `staging` / `prod` — required. Drives environment routing and staged rollout.
   - `SLA-CRITICAL`, `SLA-HIGH`, `SLA-MEDIUM`, `SLA-LOW` = hours — optional. Drives the EMERGENCY vs SCHEDULED decision; defaults to 24/72/168/720h when missing.
   - `Team`, `Product`, `Owner` = your values — optional. Populates compliance reports for audit breakdowns.

   The full tagging guide with examples is in [`docs/operations.md`](docs/operations.md#tagging-your-fleet).

> Heads up: the first time you activate AWS Config, it can take up to 6 hours to propagate to Explorer.
>
> For multi-account setups, deploying from a Delegated Administrator, or non-Amazon-Linux workloads, see [`docs/operations.md`](docs/operations.md) and [`docs/extending.md`](docs/extending.md).

## Quick start

A full deploy runs 25–30 minutes on the first pass.

Configuration lives entirely in `.env`. For a single-account deploy you really only need `AWS_PROFILE` and `AWS_REGION` — everything else has a working default. Before you run anything, it's worth skimming the [environment-variable reference](docs/operations.md#environment-configuration-env), which walks through every option (instance scope, authentication, TLS, multi-account, observability, guardrails) and its default. The ones most people touch on day one:

- `AWS_PROFILE` / `AWS_REGION` — where it deploys (required).
- `SSM_SCOPE_TAG_KEY` / `SSM_SCOPE_TAG_VALUE` — which instances the agent is even allowed to see (defaults to `PatchAutomation=enabled`).
- `MULTI_ACCOUNT_ENABLED` and the `SPOKE_*` vars — only if you're patching across more than one account.

```bash
# 1. Configure
cp .env.example .env
# Edit .env — at minimum, set AWS_PROFILE and AWS_REGION
# See docs/operations.md#environment-configuration-env for every option

# 2. Deploy
./deploy.sh

# 3. Create a Cognito user (interactive)
./deploy.sh create-user

# 4. Open the UI
aws cloudformation describe-stacks --stack-name Patchy-UI \
  --query 'Stacks[0].Outputs[?OutputKey==`UIUrl`].OutputValue' --output text
```

Want a fleet to play with? This spins up 5 sample EC2 instances split across dev/staging/prod:

```bash
./sample-env.sh deploy
```

Individual deploy commands, teardown, and the environment-variable reference are in [`docs/operations.md`](docs/operations.md).

## Multi-account setup

You can patch a fleet that's spread across several AWS accounts. The setup follows a hub-and-spoke model:

- The hub account is where the solution runs — the agent, the UI, and the compliance bucket all live here. It's your Organizations management account or a delegated administrator account.
- A spoke account is any other account holding EC2 instances you want to patch. Each one gets a lightweight IAM role (`PatchySpokeRole`) and the SSM Automation documents, deployed via StackSet. The hub assumes that role to scan and patch instances in the spoke — no agent or UI runs there.

The hub drives all cross-account work through [SSM Automation with TargetLocations](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html): one control plane reaching into N spoke accounts.

On top of the single-account prerequisites, you'll need:

- [Organizations trusted access](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-orgs-activate-trusted-access.html) enabled for SSM and CloudFormation StackSets
- Quick Setup (Host Management, Config Recording) targeting the whole org, or the OUs you plan to patch
- [Amazon Inspector multi-account setup](https://docs.aws.amazon.com/inspector/latest/user/managing-multiple-accounts.html) enabled in each spoke account, or delegated admin for org-wide coverage

Then configure `.env`:

```bash
MULTI_ACCOUNT_ENABLED=true
AWS_ORG_ID=o-xxxxxxxxxx
SPOKE_EXECUTION_ROLE=PatchySpokeRole

# Pick one of the two targeting modes:
SPOKE_ACCOUNT_IDS=111111111111,222222222222   # Explicit account list
# SPOKE_OU_IDS=ou-abc123,ou-def456            # OU-based (auto-enrols new accounts)

# Optional: fan out across multiple regions
SPOKE_REGIONS=us-east-1,eu-west-1
```

Here's what `./deploy.sh` lays down:

| Resource | Stack / Method | Scope | Purpose |
|---|---|---|---|
| AgentCore Runtime + Memory | `agentcore deploy` (npm CLI) | Hub account, hub region | Hosts the agent app, persists conversation state |
| S3 compliance bucket | `Patchy-Core` (CDK) | Hub account, hub region | Compliance reports, baseline overrides, decision audit trail |
| AgentCore IAM policy | `Patchy-Core` (CDK) | Hub account, hub region | Permissions for the agent (SSM, Inspector, EC2, S3, Organizations) |
| VPC + subnets + NAT | `Patchy-Network` (CDK) | Hub account, hub region | Network for UI Fargate tasks (skipped if `EXISTING_VPC_ID` set) |
| ECS Fargate + ALB + Cognito | `Patchy-UI` (CDK) | Hub account, hub region | Web dashboard and chat, operator authentication |
| SSM Resource Data Sync | `aws ssm create-resource-data-sync` | Hub account, hub region | Cross-account fleet discovery via SSM Explorer |
| Cross-account IAM role | `Patchy-SpokeIam` (StackSet) | (Hub + spokes) × primary region | `PatchySpokeRole` — assumed by the agent for cross-account operations |
| SSM Automation documents | `Patchy-SsmDocs` (StackSet) | (Hub + spokes) × all `SPOKE_REGIONS` | `Patchy-RunPatchBaseline`, `Patchy-RunPatchBaselineById`, `Patchy-RunRollback`, `Patchy-RunRollbackById` |

You can also deploy the StackSets on their own:

```bash
./deploy.sh spoke   # IAM role only
./deploy.sh docs    # SSM documents only
```

Adding accounts later: with `SPOKE_OU_IDS` (OU-based targeting), any account that joins the OU gets onboarded automatically. With `SPOKE_ACCOUNT_IDS` (explicit list), update `.env` and re-run `./deploy.sh spoke && ./deploy.sh docs`.

For the delegated administrator model and troubleshooting, see [`docs/operations.md`](docs/operations.md) and [`docs/extending.md`](docs/extending.md).

## What you can ask it

```
What critical vulnerabilities do we have in dev?
How widespread is CVE-2025-38477 across our fleet?
Patch all critical CVEs in staging
Run a dry-run scan on prod
Show SLA breaches this week
Roll back dev patches
Who patched prod last week?
Generate a compliance report for PCI-DSS in staging
```

## Known limitations

| Limitation | Detail |
|---|---|
| Rollback is Amazon Linux 2 only | It uses `yum history undo`. Patching itself works on any OS that Patch Manager supports. |
| Severity-scoped, not per-CVE | AWS Systems Manager Patch Manager can't install a single CVE's patch in isolation, so the agent scopes to severity level using `BaselineOverride` files. |
| Manual approval between environments | This is a safety gate by design, not an oversight. |
| Self-signed TLS by default | Swap in an ACM public certificate for production. |
| Health checks need your CloudWatch Alarms | After patching, the agent reads your existing CloudWatch Alarms to verify health. It doesn't create them — set them up on your fleet first. |
| PoC-grade | See the disclaimer up top. |

## Documentation

| Document | What's in it |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | System design, agent design, tools, patch execution flow, safety mechanisms, fleet discovery, memory model, infrastructure stacks |
| [`docs/operations.md`](docs/operations.md) | Operator command reference, tagging, SLA configuration, user management, guardrails |
| [`docs/security.md`](docs/security.md) | IAM model, network architecture, authentication, data flow, safety mechanisms |
| [`docs/observability.md`](docs/observability.md) | Debugging with traces, tool summaries, full conversation logs, and performance metrics |
| [`docs/auditing.md`](docs/auditing.md) | Enterprise audit and compliance: identity chain, compliance reports, CloudTrail correlation, hardening for regulated environments |
| [`docs/extending.md`](docs/extending.md) | Adding new OS support, custom tools, alternative scanners, delegated administrator deploy |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common issues and how to resolve them |

## Repository layout

```
├── agent/          # Agent runtime (entrypoint, tools, steering, memory)
├── infra/          # AWS CDK stacks (Network, Core, UI, SpokeIam, SsmDocs, SampleEnv)
├── ui/             # FastAPI backend + Vite/React frontend
├── docs/           # Architecture, operations, security, observability
├── tests/          # Unit tests + integration tests
├── deploy.sh       # Full-solution deploy CLI
└── sample-env.sh   # Optional demo EC2 fleet lifecycle
```

## Contributing

Issues and pull requests are welcome. Keep in mind this is a PoC, so anything non-trivial may need a design discussion before implementation. Run the local test suite before you submit:

```bash
pytest tests/                    # Unit + integration
python3 agent/eval/run_eval.py   # Tool-selection eval (requires Bedrock)
```

## Security

This asset is a proof-of-value for the services it uses and is not a production-ready solution. You'll need to work out how the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/) applies to your situation and put the right controls in place to hit your security goals. AWS gives you a broad set of security tools and configurations to do that. This repository is an experimental sample and may change without regard for backward compatibility.

At the end of the day, keeping a full-stack application secure is your responsibility as its developer. We document security best practices and ship a secure baseline, but Amazon holds no responsibility for the security of applications built from this tool.

For the complete security model — IAM permissions, network architecture, authentication, data flow, and safety mechanisms — see [`docs/security.md`](docs/security.md).

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
