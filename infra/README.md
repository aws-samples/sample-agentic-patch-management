# Infrastructure (CDK)

The CDK app for Patchy. Deploy it with `./deploy.sh` from the repository root rather than with `cdk deploy`, because the deploy script supplies what the stacks need: the runtime role and ARN, the spoke trust ARNs, the API-key secret, and the baseline override files. [`docs/deploying.md`](../docs/deploying.md) covers the commands and settings.

| Stack | Holds |
|---|---|
| `Patchy-Network` | VPC across three Availability Zones with public, private and isolated subnets, two NAT gateways and flow logs. Replaced by `Patchy-VpcLookup` when `EXISTING_VPC_ID` is set. |
| `Patchy-Core` | The compliance bucket and its policy; `PatchyAgentCorePolicy`, attached to the AgentCore runtime role |
| `Patchy-UI` | Fargate service (0.25 vCPU, 512 MB, read-only root filesystem), load balancer (internet-facing with Cognito, internal with a bastion without), Cognito user pool, report Lambda with its two EventBridge rules, optional SNS topic and KMS key |
| `Patchy-SpokeIam` | `PatchySpokeRole` and `PatchySpokeReadRole`; a StackSet in multi-account mode, a plain stack in the hub otherwise |
| `Patchy-SsmDocs` | The five Patchy SSM documents, per Region; a StackSet in multi-account mode |
| `Patchy-SampleEnv` | Optional sample fleet: five instances (two dev, one staging, two prod), three maintenance windows, three patch baselines, CloudWatch alarms. Deploy with `./sample-env.sh deploy`. |

The AgentCore runtime and memory are deployed by the AgentCore CLI from `agent/agentcore/`, not by this app.

Stacks are defined in `lib/` and wired together in `bin/app.ts`. Context the deploy script passes: `agentCoreRoleArn`, `hubAccountId`, `uiReaderRoleArns` and `synthForStackSet`.

To check the app compiles and synthesises after a change:

```bash
cd infra && npm ci && npx tsc --noEmit && npx cdk synth Patchy-Core -c agentCoreRoleArn=arn:aws:iam::111111111111:role/example
```
