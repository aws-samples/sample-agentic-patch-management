# Architecture Gap Analysis — Patch Automation Pilot

Cross-referenced against:
- [Agentic AI Accelerator (Solutions Library)](https://github.com/aws-solutions-library-samples/guidance-for-no-code-multi-agent-ai-orchestration-on-aws)
- [AgentCore Memory: Building context-aware agents](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-memory-building-context-aware-agents)
- [AgentCore long-term memory deep dive](https://aws.amazon.com/blogs/machine-learning/building-smarter-ai-agents-agentcore-long-term-memory-deep-dive)
- [AI agents in enterprises: Best practices](https://aws.amazon.com/blogs/machine-learning/ai-agents-in-enterprises-best-practices-with-amazon-bedrock-agentcore/)
- [AgentCore Memory strategies docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html)
- [AI Agent Accelerator reference implementation](https://johnritsema.com/posts/agentcore-ai-agent-accelerator/)

---

## MEMORY GAPS

### GAP-1: No long-term memory strategies configured ✅ IMPLEMENTED
**Severity:** HIGH — directly impacts enterprise value  
**Status:** DONE — `setup_memory_strategies.py` adds SEMANTIC + SUMMARY strategies  
**Current state:** Memory resource is `mode: STM_ONLY` with `event_expiry_days: 7`. Only short-term memory (raw conversation events) is stored. No memory strategies are defined.  
**What the sources say:** AgentCore Memory supports three built-in strategies — semantic (facts/knowledge), summary (running conversation summary), and user preferences. These run asynchronously on raw events to extract persistent insights. The long-term memory deep dive blog shows 89-95% compression rates with strong correctness, meaning agents get relevant context without loading full conversation history.  
**Impact:** Every session starts cold. An operator who patched dev yesterday and returns today to patch staging has zero recall. The agent can't learn from past patching outcomes (e.g., "last time we patched prod with CVE-2024-6387, 2 instances needed rollback").  
**Fix:** Call `UpdateMemory` API to add semantic + summary strategies to the existing memory resource. Update `memory_hooks.py` to call `retrieve_memory_records` with semantic search on agent init, injecting long-term memories as context.  
**Phase:** 1 (Pilot)

### GAP-2: Flat actor_id — no operator-scoped memory isolation ✅ IMPLEMENTED
**Severity:** HIGH — data isolation issue for multi-user  
**Status:** DONE — `actor_id` is now `patch-automation/{operator_identity}` in `agent_factory.py`  
**Current state:** All agents use `actor_id = "patch-automation-supervisor_agent"` regardless of which operator is making the request.  
**What the sources say:** The context-aware agents blog emphasizes hierarchical namespaces (`/org_id/user_id/preferences`) for multi-tenant isolation. The Accelerator uses `user_id` as part of the memory hook initialization to scope memory per user.  
**Impact:** If long-term memory strategies are enabled with the current flat actor_id, Operator A's extracted facts would be visible to Operator B's semantic search. This is a data leakage risk.  
**Fix:** Change `actor_id` to include operator identity: `patch-automation/{operator_identity}`. Thread operator identity from supervisor payload through context into `agent_factory.py`. Short-term memory is already isolated by `session_id`, but long-term memory retrieval needs operator-scoped namespaces.  
**Phase:** 1 (Pilot)

### GAP-3: No cross-session context retrieval ✅ IMPLEMENTED
**Severity:** MEDIUM — impacts continuity across sessions  
**Status:** DONE — Supervisor uses `AgentCoreMemorySessionManager` with `RetrievalConfig` for automatic LTM retrieval on every user message  
**Current state:** `on_agent_initialized` calls `get_last_k_turns` which only loads the current session's history. New sessions start empty.  
**What the sources say:** The whole point of long-term memory is cross-session continuity. The Accelerator's reference implementation explicitly tests "start a new session and reference previous conversations — verify the agent remembers context."  
**Impact:** Operators lose all context when starting a new conversation. They have to re-explain their fleet state, which CVEs they're tracking, and what was already patched.  
**Fix:** After loading short-term turns (current session), call `retrieve_memory_records` with a semantic search query derived from the user's current message. Inject top-k results as context messages. This gives the agent cross-session recall.  
**Implementation note:** Aligned with the Agentic AI Accelerator reference architecture — uses `AgentCoreMemorySessionManager` (from `bedrock_agentcore.memory.integrations.strands.session_manager`) instead of custom `HookProvider`. The session manager handles STM persistence, session rehydration, monotonic timestamps, and LTM retrieval via its `retrieve_customer_context` hook on every `MessageAddedEvent`. Specialists use a lightweight `ReadOnlyMemoryHook` that loads the supervisor's conversation context without writing to memory.  
**Phase:** 1 (Pilot) — depends on GAP-1

### GAP-4: Specialists load full conversation history instead of targeted context
**Severity:** MEDIUM — token waste and noise-induced hallucination  
**Current state:** Specialists load the last 10 turns of supervisor history via `get_last_k_turns`. Most of this is irrelevant to the specialist's task.  
**What the sources say:** The enterprise best practices blog says "the key challenge in multi-agent systems is maintaining context across handoffs." The Accelerator uses A2A protocol where each specialist gets only the query routed to it, not the full conversation. The blog recommends targeted context over full history.  
**Impact:** The patch manager sees the compliance analyst's previous output, the vulnerability analyst's findings, and unrelated user chitchat. This wastes tokens and increases hallucination risk.  
**Fix:** Replace the 10-turn history load for specialists with targeted `retrieve_memory_records` semantic search scoped to the specialist's domain. The supervisor already enriches the query before routing — combine that with targeted memory retrieval. Alternatively, reduce `k` to 3-5 turns and filter to only relevant message types.  
**Phase:** 2 (Production hardening)

### GAP-5: No workflow checkpointing for multi-step operations
**Severity:** MEDIUM — resilience gap for long-running workflows  
**Current state:** No state persistence between workflow steps. If the agent crashes or session disconnects mid-workflow (e.g., after dry-run but before execute), everything is lost.  
**What the sources say:** The context-aware agents blog describes checkpointing — saving agent state at specific points using blob events. The Accelerator's AgentCore Runtime provides stateful containers per session, but our solution runs on shared AgentCore Runtime where sessions can be interrupted.  
**Impact:** A prod patching workflow that fails at step 5 requires re-running steps 1-4. For enterprise customers, this is unacceptable for critical operations.  
**Fix:** After each major workflow step, save state (instance_ids, command_ids, pre_patch_state, SLA decision) as a blob event via `create_event` with blob payload type. On reconnection, check for existing checkpoint and offer to resume.  
**Phase:** 2 (Production hardening)

---

## OBSERVABILITY GAPS

### GAP-6: No OpenTelemetry auto-instrumentation for deep tracing
**Severity:** MEDIUM — nice-to-have for deep debugging  
**Current state:** AgentCore Observability is already enabled (`observability: enabled: true`) and provides session-level visibility in CloudWatch GenAI Observability. However, we don't have OTEL auto-instrumentation for Strands-level detail (per-tool latency, token counts per LLM call, trace propagation across specialist handoffs).  
**What the sources say:** The Accelerator uses `aws-opentelemetry-distro` with `opentelemetry-instrument` as the CMD entrypoint. This adds fine-grained traces on top of AgentCore's native observability.  
**Impact:** AgentCore Observability covers the pilot's needs. The OTEL layer adds deeper debugging capability (which LLM call is slow, how many tokens each specialist burns) that becomes valuable at production scale.  
**Fix:** Add `aws-opentelemetry-distro` to agent requirements. Change Dockerfile CMD to `opentelemetry-instrument python -u supervisor.py`.  
**Phase:** 2 (Production hardening) — AgentCore Observability is sufficient for pilot

### GAP-7: No AgentCore Evaluations integration
**Severity:** LOW — important for production, not blocking pilot  
**Current state:** We have a manual `test_agent.sh` and `integration_test.py`. No automated evaluation pipeline.  
**What the sources say:** The enterprise best practices blog dedicates an entire section to automated evaluation: "Run this evaluation suite against your ground truth dataset. Before your first change, your baseline might show 92% tool selection accuracy." AgentCore Evaluations provides on-demand and online evaluation modes with LLM-as-Judge scoring.  
**Impact:** We can't quantify whether prompt changes improve or degrade agent quality. No regression detection.  
**Fix:** Build a ground truth dataset of 30-50 test queries with expected tool calls and responses. Integrate with AgentCore Evaluations for on-demand runs before deployment.  
**Phase:** 2 (Production hardening)

---

## SECURITY GAPS

### GAP-8: No Bedrock Guardrails integration (sample implementation) ✅ IMPLEMENTED
**Severity:** LOW — sample pattern for enterprise customers  
**Status:** DONE — `setup_guardrail.py` creates sample guardrail, `agent_factory.py` applies it via env vars  
**Current state:** We handle out-of-scope queries via prompt engineering only. No content filtering on inputs or outputs.  
**What the sources say:** The Accelerator passes user questions and final responses through Amazon Bedrock Guardrails when enabled. Guardrails can redact PII, block harmful content, and enforce topic boundaries. The enterprise best practices blog emphasizes "security and access control must be enforced before tools execute."  
**Impact:** Prompt-based guardrails work well for the pilot's scope. Enterprise customers in regulated industries will want Bedrock Guardrails for defense-in-depth.  
**Fix:** Create a minimal sample Bedrock Guardrail with: (1) one denied topic (non-patching queries) and (2) basic content filter. Apply it to the Strands Agent model configuration. Document in README that customers can extend with PII redaction, additional topic policies, and custom word filters for their compliance requirements. Ship as opt-in configuration.  
**Phase:** 1 (Pilot) — lightweight sample implementation, documented as extensible

### GAP-9: No Cognito/JWT authentication — API key only
**Severity:** MEDIUM — acceptable for pilot, not for production  
**Current state:** Auth is via API key header or X-Role header (pilot mode). No user identity beyond the role.  
**What the sources say:** The Accelerator uses AWS Cognito with user pools, user groups (admin, agent-creator, supervisor-user, readonly-user), and JWT Bearer token authentication. The enterprise best practices blog describes AgentCore Identity for OAuth claims that flow through to tool authorization.  
**Impact:** We can't tie actions to real user identities. The `operator` field in audit logs is `operator@web-abc123` — not a real person. For enterprise audit requirements, this needs to be a real identity.  
**Fix:** Add Cognito user pool to the CDK stack. Frontend authenticates via Cognito hosted UI, sends JWT to backend. Backend validates JWT and extracts user identity for the `operator` field. The Accelerator's CDK code is a good reference for this.  
**Phase:** 2 (Production hardening) — pilot can use API keys with documented operator names

### GAP-10: No WAF protection on the UI ALB
**Severity:** LOW — internal ALB mitigates risk  
**Current state:** UI is on an internal ALB accessed via SSM port forwarding. No WAF.  
**What the sources say:** The Accelerator deploys AWS WAF with OWASP Top 10 managed rules on the ALB. Even for internal-only access, WAF provides defense-in-depth.  
**Impact:** Low risk since the ALB is internal-only. But if the solution is exposed to a broader network (e.g., corporate VPN), WAF becomes important.  
**Fix:** Add WAF WebACL with AWS Managed Rules Common Rule Set to the UI ALB in CDK.  
**Phase:** 2 (Production hardening)

### GAP-11: No KMS customer-managed encryption for memory/config
**Severity:** LOW — AWS managed keys are acceptable for pilot  
**Current state:** Memory resource uses default AWS managed encryption. Config stored in plaintext `config.json` in the container.  
**What the sources say:** The Accelerator uses SecureString Parameter Store with customer-managed KMS keys for all configuration. Memory resources support customer-managed KMS keys via `encryptionKeyArn`.  
**Impact:** Acceptable for pilot. Enterprise customers in regulated industries may require CMK.  
**Fix:** Create a KMS key, pass `encryptionKeyArn` when creating/updating the memory resource. Move sensitive config to SSM Parameter Store SecureString.  
**Phase:** 2 (Production hardening)

---

## ARCHITECTURE GAPS

### GAP-12: No A2A protocol for specialist communication
**Severity:** LOW — current approach works, A2A is a scaling concern  
**Current state:** Specialists are created as in-process Strands agents. The supervisor calls them as Python functions within the same AgentCore Runtime container.  
**What the sources say:** The Accelerator uses A2A (Agent-to-Agent) protocol with VPC Lattice for service discovery. Each specialist runs in its own ECS Fargate container. This enables independent scaling, deployment, and monitoring of each specialist.  
**Impact:** Our approach is simpler and lower-latency (no network hop). But it means all specialists share the supervisor's compute resources. If the patch manager runs a 10-minute `verify_and_proceed`, it blocks the supervisor container.  
**Fix:** For the pilot, the current in-process approach is fine. For production at scale, consider deploying specialists as separate AgentCore Runtime agents and using A2A protocol for communication. This is a significant architectural change.  
**Phase:** 3 (Scale)

### GAP-13: No agent-as-code deterministic SLA decision
**Severity:** LOW — prompt-based SLA works but is fragile  
**Current state:** The SLA comparison (SCHEDULED vs EMERGENCY) is in the patch_manager prompt. The agent reasons about `hours_until_window` vs `sla_hours`.  
**What the sources say:** The enterprise best practices blog says "reserve agents for tasks that require reasoning over ambiguous inputs. Use traditional code for calculations." They show a concrete example where passing current date as an attribute (code) vs. making the agent call a tool (agentic) saves 25% latency and tokens.  
**Impact:** The agent occasionally gets the SLA comparison wrong or uses conditional language ("we might need emergency patching"). A deterministic function would always get it right.  
**Fix:** Add a `calculate_sla_decision` tool that takes `sla_hours` and `hours_until_window` and returns `{"decision": "EMERGENCY", "reasoning": "Window (72hr) exceeds SLA (12hr) by 60hr"}`. The agent calls this instead of reasoning about it.  
**Phase:** 2 (Production hardening)

### GAP-14: No CloudWatch Logs Data Protection for sensitive data masking
**Severity:** LOW — defense-in-depth  
**Current state:** Agent logs may contain instance IDs, CVE details, operator names, and AWS account IDs in plaintext.  
**What the sources say:** The Accelerator uses CloudWatch Logs Data Protection with AWS managed identifiers and custom patterns to automatically detect and mask sensitive data in logs.  
**Impact:** Low risk for internal use. Higher risk if logs are shared with third parties or if compliance requires data masking.  
**Fix:** Enable CloudWatch Logs Data Protection on the agent's log group with managed identifiers for AWS account IDs and custom patterns for instance IDs.  
**Phase:** 2 (Production hardening)

### GAP-15: No IAM Permissions Boundaries on compute roles
**Severity:** LOW — defense-in-depth  
**Current state:** AgentCore execution role has the permissions it needs, but no permissions boundary to cap maximum privileges.  
**What the sources say:** The Accelerator applies service-specific permissions boundaries that limit maximum privileges and prevent privilege escalation across all compute resources.  
**Impact:** If the execution role is modified (accidentally or maliciously), there's no upper bound on what it can do.  
**Fix:** Create a permissions boundary policy that caps the execution role to only the services it needs (SSM, Inspector, EC2, S3, CloudWatch). Attach as a permissions boundary.  
**Phase:** 2 (Production hardening)

---

## SUMMARY

| # | Gap | Severity | Phase | Effort |
|---|-----|----------|-------|--------|
| 1 | No long-term memory strategies | HIGH | 1 | Medium |
| 2 | Flat actor_id — no operator isolation | HIGH | 1 | Small |
| 3 | No cross-session context retrieval | MEDIUM | 1 | Medium |
| 4 | Specialists load full history | MEDIUM | 2 | Medium |
| 5 | No workflow checkpointing | MEDIUM | 2 | Medium |
| 6 | No OTEL auto-instrumentation | MEDIUM | 2 | Small |
| 7 | No AgentCore Evaluations | LOW | 2 | Medium |
| 8 | Bedrock Guardrails (sample) | LOW | 1 | Small |
| 9 | No Cognito/JWT auth | MEDIUM | 2 | Large |
| 10 | No WAF on UI ALB | LOW | 2 | Small |
| 11 | No KMS CMK encryption | LOW | 2 | Small |
| 12 | No A2A protocol for specialists | LOW | 3 | Large |
| 13 | No deterministic SLA decision tool | LOW | 2 | Small |
| 14 | No CloudWatch Logs Data Protection | LOW | 2 | Small |
| 15 | No IAM Permissions Boundaries | LOW | 2 | Small |

**Phase 1 (Pilot — do now):** GAP-1, GAP-2, GAP-3, GAP-8 (sample)  
**Phase 2 (Production hardening):** GAP-4, GAP-5, GAP-6, GAP-7, GAP-9, GAP-10, GAP-11, GAP-13, GAP-14, GAP-15  
**Phase 3 (Scale):** GAP-12
