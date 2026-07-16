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
export class PatchySsmDocsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Tag-based fleet patching ──────────────────────────────────────
    // Targets instances by Environment + ScopeTagKey/Value tags. Used when
    // the operator says "patch all prod" rather than naming instance IDs.
    new ssm.CfnDocument(this, 'PatchAutomationDocV2', {
      name: 'Patchy-RunPatchBaseline',
      documentType: 'Automation',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Run AWS-RunPatchBaseline via Automation for cross-account execution',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          Operation: { type: 'String', allowedValues: ['Scan', 'Install'] },
          Environment: { type: 'String' },
          ScopeTagKey: { type: 'String', default: 'PatchAutomation' },
          ScopeTagValue: { type: 'String', default: 'enabled' },
          RebootOption: { type: 'String', default: 'NoReboot', allowedValues: ['NoReboot', 'RebootIfNeeded'] },
          BaselineOverride: { type: 'String', default: '' },
          // Customer-tunable concurrency for the inner SendCommand fan-out.
          // Driven from agent/helper/cross_account.py:EXECUTION_DEFAULTS.
          MaxConcurrency: { type: 'String', default: '50%' },
          MaxErrors: { type: 'String', default: '100%' },
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
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Run AWS-RunPatchBaseline on specific instance IDs via Automation',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          InstanceId: { type: 'String', description: 'Target EC2 instance ID' },
          Operation: { type: 'String', allowedValues: ['Scan', 'Install'] },
          RebootOption: { type: 'String', default: 'NoReboot', allowedValues: ['NoReboot', 'RebootIfNeeded'] },
          BaselineOverride: { type: 'String', default: '' },
          // Customer-tunable concurrency for the inner SendCommand.
          // Single-instance runs only need '1', but the parent Automation's
          // top-level MaxConcurrency is what fans out across multiple instances.
          MaxConcurrency: { type: 'String', default: '1' },
          MaxErrors: { type: 'String', default: '100%' },
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
            },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });

    // ── Instance-ID based rollback ──
    new ssm.CfnDocument(this, 'RollbackByIdAutomationDocV2', {
      name: 'Patchy-RunRollbackById',
      documentType: 'Automation',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Rollback last yum transaction on a specific instance via Automation',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          InstanceId: { type: 'String', description: 'Target EC2 instance ID' },
          // Customer-tunable concurrency for the inner SendCommand.
          MaxConcurrency: { type: 'String', default: '1' },
          MaxErrors: { type: 'String', default: '100%' },
        },
        mainSteps: [{
          name: 'RunRollback',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'AWS-RunShellScript',
            InstanceIds: ['{{ InstanceId }}'],
            Parameters: {
              commands: [
                '#!/bin/bash',
                'LAST_TRANSACTION=$(yum history list | grep -E "^[[:space:]]*[0-9]+" | head -1 | awk \'{print $1}\')',
                'if [ -z "$LAST_TRANSACTION" ]; then echo "ERROR: No yum history found"; exit 1; fi',
                'echo "Rolling back transaction ID: $LAST_TRANSACTION"',
                'yum history undo $LAST_TRANSACTION -y',
                'if [ $? -eq 0 ]; then echo "SUCCESS: Rollback completed for transaction $LAST_TRANSACTION"; else echo "ERROR: Rollback failed"; exit 1; fi',
              ],
            },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });

    // ── Tag-based fleet rollback ──
    new ssm.CfnDocument(this, 'RollbackAutomationDocV2', {
      name: 'Patchy-RunRollback',
      documentType: 'Automation',
      documentFormat: 'YAML',
      content: {
        schemaVersion: '0.3',
        description: 'Rollback last yum transaction via Automation for cross-account execution',
        assumeRole: '{{ AutomationAssumeRole }}',
        parameters: {
          AutomationAssumeRole: { type: 'String', default: '' },
          Environment: { type: 'String' },
          ScopeTagKey: { type: 'String', default: 'PatchAutomation' },
          ScopeTagValue: { type: 'String', default: 'enabled' },
          // Customer-tunable concurrency for the inner SendCommand fan-out.
          MaxConcurrency: { type: 'String', default: '50%' },
          MaxErrors: { type: 'String', default: '100%' },
        },
        mainSteps: [{
          name: 'RunRollback',
          action: 'aws:runCommand',
          inputs: {
            DocumentName: 'AWS-RunShellScript',
            Targets: [
              { Key: 'tag:Environment', Values: ['{{ Environment }}'] },
              { Key: 'tag:{{ ScopeTagKey }}', Values: ['{{ ScopeTagValue }}'] },
            ],
            Parameters: {
              commands: [
                '#!/bin/bash',
                'LAST_TRANSACTION=$(yum history list | grep -E "^[[:space:]]*[0-9]+" | head -1 | awk \'{print $1}\')',
                'if [ -z "$LAST_TRANSACTION" ]; then echo "ERROR: No yum history found"; exit 1; fi',
                'echo "Rolling back transaction ID: $LAST_TRANSACTION"',
                'yum history undo $LAST_TRANSACTION -y',
                'if [ $? -eq 0 ]; then echo "SUCCESS: Rollback completed for transaction $LAST_TRANSACTION"; else echo "ERROR: Rollback failed"; exit 1; fi',
              ],
            },
            MaxConcurrency: '{{ MaxConcurrency }}',
            MaxErrors: '{{ MaxErrors }}',
          },
        }],
      },
    });
  }
}
