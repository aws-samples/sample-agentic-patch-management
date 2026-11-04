import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * The organisation ID that cross-account statements are scoped to.
 * Required in multi-account mode; throws with setup instructions otherwise.
 */
export function requireOrgId(purpose: string): string {
  const orgId = process.env.AWS_ORG_ID;
  if (!orgId) {
    throw new Error(
      `AWS_ORG_ID is required when MULTI_ACCOUNT_ENABLED=true. Set it in .env so ${purpose} ` +
      'is scoped to your organization. Get it via: ' +
      "aws organizations describe-organization --query 'Organization.Id' --output text"
    );
  }
  return orgId;
}

/**
 * sts:AssumeRole on a spoke role name in accounts of this organisation.
 * aws:ResourceOrgID checks the target role's organisation; aws:PrincipalOrgID
 * would check the caller's own, which is always true here.
 */
export function assumeSpokeRoleStatement(roleName: string, purpose: string): iam.PolicyStatement {
  return new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['sts:AssumeRole'],
    resources: [`arn:aws:iam::*:role/${roleName}`],
    conditions: { StringEquals: { 'aws:ResourceOrgID': requireOrgId(purpose) } },
  });
}

/** SendCommand condition: the instance must carry the configured scope tag. */
export function scopeTagCondition(): Record<string, Record<string, string>> {
  return {
    StringEquals: {
      [`ssm:resourceTag/${process.env.SSM_SCOPE_TAG_KEY || 'PatchAutomation'}`]:
        process.env.SSM_SCOPE_TAG_VALUE || 'enabled',
    },
  };
}
