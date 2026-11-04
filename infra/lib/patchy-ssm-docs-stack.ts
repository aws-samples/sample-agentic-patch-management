import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/**
 * Patchy-SsmDocs: SSM Automation documents only.
 *
 * SSM documents are regional. This stack must be deployed to every (account,
 * region) the agent fans out into — both the hub and every spoke account,
 * across every value in SPOKE_REGIONS. The deploy script targets this StackSet
 * accordingly.
 *
 * The cross-account IAM role is deployed independently by Patchy-SpokeIam.
 */
export interface PatchySsmDocsStackProps extends cdk.StackProps {
  /** Hub account whose compliance bucket holds the override files. */
  hubAccountId: string;
}

// Parameter patterns. The agent builds these values, but the documents run
// with the spoke role, so they also refuse anything else: override files only
// from the hub's own bucket prefixes, rollout limits in SSM's own forms, and
// tag keys and values in the characters EC2 tags allow (no {{ }}).
const TAG_CHARS = '[A-Za-z0-9 +=._:/@-]';
const PATTERNS = {
  instanceId: '^i-[0-9a-f]{8,17}$',
  environment: `^${TAG_CHARS}{1,256}$`,
  tagKey: `^${TAG_CHARS}{1,128}$`,
  tagValue: `^${TAG_CHARS}{1,256}$`,
  maxConcurrency: '^([1-9][0-9]{0,3}|[1-9][0-9]?%|100%)$',
  maxErrors: '^([0-9]{1,4}|[0-9]{1,2}%|100%)$',
};

export class PatchySsmDocsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PatchySsmDocsStackProps) {
    super(scope, id, props);
    const bucket = `patch-compliance-reports-${props.hubAccountId}`;
    const baselineOverridePattern = `^$|^s3://${bucket}/baseline-overrides/[A-Za-z0-9._-]{1,128}\\.json$`;
    const installOverridePattern = `^$|^s3://${bucket}/install-overrides/[a-f0-9]{64}\\.yaml$`;

    // ── Tag-based fleet patching ──────────────────────────────────────
    // Targets instances by Environment + ScopeTagKey/Value tags. Used when
    // the operator says "patch all prod" rather than naming instance IDs.
    new ssm.CfnDocument(this, 'PatchAutomationDocV2', {
      name: 'Patchy-RunPatchBaseline',
      documentType: 'Automation',
      // Fixed names: update in place as a new version, not by replacement.
      updateMethod: 'NewVersion',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Run AWS-RunPatchBaseline via Automation for cross-account execution',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          Operation: { type: 'String', allowedValues: ['Scan', 'Install'] },
          Environment: { type: 'String', allowedPattern: PATTERNS.environment },
          ScopeTagKey: { type: 'String', default: 'PatchAutomation', allowedPattern: PATTERNS.tagKey },
          ScopeTagValue: { type: 'String', default: 'enabled', allowedPattern: PATTERNS.tagValue },
          RebootOption: { type: 'String', default: 'NoReboot', allowedValues: ['NoReboot', 'RebootIfNeeded'] },
          BaselineOverride: { type: 'String', default: '', allowedPattern: baselineOverridePattern },
          // CVE-scoped installs: s3:// URL of the package list (see agent/helper/cve_scope.py).
          InstallOverrideList: { type: 'String', default: '', allowedPattern: installOverridePattern },
          // Customer-tunable concurrency for the inner SendCommand fan-out.
          // Driven from agent/helper/cross_account.py:EXECUTION_DEFAULTS.
          MaxConcurrency: { type: 'String', default: '50%', allowedPattern: PATTERNS.maxConcurrency },
          MaxErrors: { type: 'String', default: '100%', allowedPattern: PATTERNS.maxErrors },
        },
        mainSteps: [{
          name: 'RunPatchBaseline',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'AWS-RunPatchBaseline',
            Targets: [
              { Key: 'tag:Environment', Values: ['{{ Environment }}'] },
              { Key: 'tag:{{ ScopeTagKey }}', Values: ['{{ ScopeTagValue }}'] },
            ],
            Parameters: {
              Operation: ['{{ Operation }}'],
              RebootOption: ['{{ RebootOption }}'],
              BaselineOverride: ['{{ BaselineOverride }}'],
              InstallOverrideList: ['{{ InstallOverrideList }}'],
            },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });

    // ── Instance-ID based patching (for specific instance targeting via MAMR) ──
    // Unlike Patchy-RunPatchBaseline which targets by tag, this doc accepts an
    // InstanceId parameter and targets that specific instance. Used when the
    // operator names specific instance IDs rather than "all prod".
    new ssm.CfnDocument(this, 'PatchByIdAutomationDocV2', {
      name: 'Patchy-RunPatchBaselineById',
      documentType: 'Automation',
      // Fixed names: update in place as a new version, not by replacement.
      updateMethod: 'NewVersion',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Run AWS-RunPatchBaseline on specific instance IDs via Automation',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          InstanceId: { type: 'String', description: 'Target EC2 instance ID', allowedPattern: PATTERNS.instanceId },
          Operation: { type: 'String', allowedValues: ['Scan', 'Install'] },
          RebootOption: { type: 'String', default: 'NoReboot', allowedValues: ['NoReboot', 'RebootIfNeeded'] },
          BaselineOverride: { type: 'String', default: '', allowedPattern: baselineOverridePattern },
          // CVE-scoped installs: s3:// URL of the package list (see agent/helper/cve_scope.py).
          InstallOverrideList: { type: 'String', default: '', allowedPattern: installOverridePattern },
          // Customer-tunable concurrency for the inner SendCommand.
          // Single-instance runs only need '1', but the parent Automation's
          // top-level MaxConcurrency is what fans out across multiple instances.
          MaxConcurrency: { type: 'String', default: '1', allowedPattern: PATTERNS.maxConcurrency },
          MaxErrors: { type: 'String', default: '100%', allowedPattern: PATTERNS.maxErrors },
        },
        mainSteps: [{
          name: 'RunPatchBaseline',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'AWS-RunPatchBaseline',
            InstanceIds: ['{{ InstanceId }}'],
            Parameters: {
              Operation: ['{{ Operation }}'],
              RebootOption: ['{{ RebootOption }}'],
              BaselineOverride: ['{{ BaselineOverride }}'],
              InstallOverrideList: ['{{ InstallOverrideList }}'],
            },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });

    // ── Rollback command ──
    // The rollback runbooks run this fixed script instead of AWS-RunShellScript,
    // so neither the hub nor the spoke role needs a general shell document.
    const yumUndo = new ssm.CfnDocument(this, 'YumUndoCommandDoc', {
      name: 'Patchy-YumUndo',
      documentType: 'Command',
      updateMethod: 'NewVersion',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '2.2',
        description: 'Undo the yum transactions that began within a patch run\'s time window',
        // Since and Until are epoch seconds from the patch run's report. The
        // pattern keeps them to digits, so they're safe inside the script.
        // There is no "undo the last transaction" fallback: rolling back a
        // change Patchy didn't make is what this window prevents.
        parameters: {
          Since: { type: 'String', default: '0', allowedPattern: '^[0-9]{1,12}$' },
          Until: { type: 'String', default: '0', allowedPattern: '^[0-9]{1,12}$' },
        },
        mainSteps: [{
          action: 'aws:runShellScript',
          name: 'YumUndo',
          inputs: {
            runCommand: [
              '#!/bin/bash',
              'SINCE="{{ Since }}"',
              'UNTIL="{{ Until }}"',
              'if [ "$SINCE" = "0" ] || [ "$UNTIL" = "0" ]; then echo "ERROR: Since and Until are required"; exit 1; fi',
              'IDS=""',
              '# yum history list is newest first, so IDS ends up newest first',
              'for ID in $(yum history list 2>/dev/null | grep -E "^[[:space:]]*[0-9]+" | awk \'{print $1}\' | head -50); do',
              '  BEGIN=$(yum history info "$ID" 2>/dev/null | grep -m1 -i "^Begin time" | cut -d: -f2- | sed "s/^ *//")',
              '  TS=$(date -d "$BEGIN" +%s 2>/dev/null) || continue',
              '  if [ "$TS" -ge "$SINCE" ] && [ "$TS" -le "$UNTIL" ]; then IDS="$IDS $ID"; fi',
              'done',
              '# An instance the run did not change (an environment rollback targets every',
              '# tagged instance) completes cleanly instead of failing the rollout.',
              'if [ -z "$IDS" ]; then echo "Nothing from this run on this instance: no yum transaction began between $SINCE and $UNTIL"; exit 0; fi',
              'for ID in $IDS; do',
              '  echo "Rolling back transaction ID: $ID"',
              '  yum history undo "$ID" -y || { echo "ERROR: Rollback of transaction $ID failed"; exit 1; }',
              'done',
              'echo "SUCCESS: Rolled back transactions:$IDS"',
            ],
          },
        }],
      },
    });

    // ── Instance-ID based rollback ──
    const rollbackById = new ssm.CfnDocument(this, 'RollbackByIdAutomationDocV2', {
      name: 'Patchy-RunRollbackById',
      documentType: 'Automation',
      // Fixed names: update in place as a new version, not by replacement.
      updateMethod: 'NewVersion',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Undo a patch run\'s yum transactions on a specific instance via Automation',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          InstanceId: { type: 'String', description: 'Target EC2 instance ID', allowedPattern: PATTERNS.instanceId },
          Since: { type: 'String', allowedPattern: '^[0-9]{1,12}$' },
          Until: { type: 'String', allowedPattern: '^[0-9]{1,12}$' },
          // Customer-tunable concurrency for the inner SendCommand.
          MaxConcurrency: { type: 'String', default: '1', allowedPattern: PATTERNS.maxConcurrency },
          MaxErrors: { type: 'String', default: '100%', allowedPattern: PATTERNS.maxErrors },
        },
        mainSteps: [{
          name: 'RunRollback',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'Patchy-YumUndo',
            InstanceIds: ['{{ InstanceId }}'],
            Parameters: { Since: ['{{ Since }}'], Until: ['{{ Until }}'] },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });

    // ── Tag-based fleet rollback ──
    const rollbackByTag = new ssm.CfnDocument(this, 'RollbackAutomationDocV2', {
      name: 'Patchy-RunRollback',
      documentType: 'Automation',
      // Fixed names: update in place as a new version, not by replacement.
      updateMethod: 'NewVersion',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Undo a patch run\'s yum transactions via Automation for cross-account execution',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          Environment: { type: 'String', allowedPattern: PATTERNS.environment },
          ScopeTagKey: { type: 'String', default: 'PatchAutomation', allowedPattern: PATTERNS.tagKey },
          ScopeTagValue: { type: 'String', default: 'enabled', allowedPattern: PATTERNS.tagValue },
          Since: { type: 'String', allowedPattern: '^[0-9]{1,12}$' },
          Until: { type: 'String', allowedPattern: '^[0-9]{1,12}$' },
          // Customer-tunable concurrency for the inner SendCommand fan-out.
          MaxConcurrency: { type: 'String', default: '50%', allowedPattern: PATTERNS.maxConcurrency },
          MaxErrors: { type: 'String', default: '100%', allowedPattern: PATTERNS.maxErrors },
        },
        mainSteps: [{
          name: 'RunRollback',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'Patchy-YumUndo',
            Targets: [
              { Key: 'tag:Environment', Values: ['{{ Environment }}'] },
              { Key: 'tag:{{ ScopeTagKey }}', Values: ['{{ ScopeTagValue }}'] },
            ],
            Parameters: { Since: ['{{ Since }}'], Until: ['{{ Until }}'] },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });
    rollbackById.addDependency(yumUndo);
    rollbackByTag.addDependency(yumUndo);
  }
}
