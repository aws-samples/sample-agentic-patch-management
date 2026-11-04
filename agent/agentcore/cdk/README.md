# AgentCore CDK project

This project is generated and managed by the AgentCore CLI. It deploys the Patchy runtime and memory with the `@aws/agentcore-cdk` constructs, reading its configuration from `agent/agentcore/`.

Deploy it with `./deploy.sh agent` from the repository root, not with `agentcore deploy` directly. The deploy script writes the settings from `.env` into `agent/agentcore/agentcore.json`, pins the CLI version, sets up the JWT authoriser when `RUNTIME_AUTH=jwt`, and restores the committed file afterwards. A direct `agentcore deploy` deploys the committed single-account defaults.

- `bin/cdk.ts` creates a stack per deployment target from the project configuration.
- `lib/cdk-stack.ts` defines the stack around the `AgentCoreApplication` construct.
