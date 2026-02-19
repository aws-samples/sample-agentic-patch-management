// ============================================================================
// Sample AgentCore Policy — Cedar policies for Intelligent Patch Automation
// ============================================================================
//
// These policies enforce deterministic controls at the AgentCore Gateway
// boundary — outside the agent's code. They complement Bedrock Guardrails
// (which filter content) by controlling what ACTIONS the agent can take.
//
// To use: create an AgentCore Gateway + Policy Engine, then attach these
// policies. See: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html
//
// Cedar uses forbid-overrides-permit semantics:
//   - Default: DENY (no policies = blocked)
//   - Any forbid match = DENY (even if permit also matches)
//   - At least one permit + no forbid = ALLOW
// ============================================================================


// ---------------------------------------------------------------------------
// POLICY 1: Operators can use all tools
// ---------------------------------------------------------------------------
// Operators have full access to vulnerability scanning, patching, and
// compliance reporting. This is the baseline permit for authenticated users
// with the "operator" role.

permit(
  principal is AgentCore::OAuthUser,
  action,
  resource
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "operator"
};


// ---------------------------------------------------------------------------
// POLICY 2: Viewers can only read — no patching or rollback
// ---------------------------------------------------------------------------
// Viewers can query vulnerabilities and compliance reports but cannot
// execute patches, rollbacks, or any write operations.

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"PatchTools__execute_patch_operation",
  resource
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "viewer"
};

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"PatchTools__rollback_patches",
  resource
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "viewer"
};


// ---------------------------------------------------------------------------
// POLICY 3: Block prod patching outside maintenance windows
// ---------------------------------------------------------------------------
// Prod patching requires either a maintenance window or an explicit
// emergency flag. This prevents accidental prod patches during business hours.
//
// NOTE: This policy checks the "environment" input parameter. The agent's
// execute_patch_operation tool passes environment as an input field.

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"PatchTools__execute_patch_operation",
  resource
)
when {
  context.input has environment &&
  context.input.environment == "prod" &&
  !(context.input has emergency && context.input.emergency == true)
};


// ---------------------------------------------------------------------------
// POLICY 4: Emergency shutdown — disable all tool calls
// ---------------------------------------------------------------------------
// Uncomment during incidents to immediately block all agent actions.
// Overrides all permit policies due to forbid-wins semantics.
//
// forbid(
//   principal,
//   action,
//   resource
// );


// ---------------------------------------------------------------------------
// POLICY 5: Block suspended users
// ---------------------------------------------------------------------------
// Immediately revoke access for compromised or suspended accounts.
// Works with JWT claims from Cognito or any OIDC provider.

forbid(
  principal is AgentCore::OAuthUser,
  action,
  resource
)
when {
  principal.hasTag("account_status") &&
  principal.getTag("account_status") == "suspended"
};


// ---------------------------------------------------------------------------
// POLICY 6: Restrict rollback to recent patches only
// ---------------------------------------------------------------------------
// Rollback should only be attempted on patches applied within the last
// 24 hours. Older patches may have dependencies that make rollback unsafe.
//
// NOTE: Cedar doesn't support datetime arithmetic natively. This policy
// checks for a "hours_since_patch" field that the agent would need to
// populate in the tool input. Alternatively, enforce this in application
// code and use this policy as defense-in-depth.
//
// forbid(
//   principal is AgentCore::OAuthUser,
//   action == AgentCore::Action::"PatchTools__rollback_patches",
//   resource
// )
// when {
//   context.input has hours_since_patch &&
//   context.input.hours_since_patch > 24
// };
