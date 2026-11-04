"""Report Lambda: write compliance reports when Patchy runs finish, and notify.

EventBridge invokes this for SSM's "EC2 Automation Execution Status-change
Notification" when a Patchy-* runbook reaches a final state. It runs the
same reconciliation the dashboard runs on load (idempotent: a report that
already exists is left alone), so reports no longer wait for someone to open
the dashboard. With NOTIFY_TOPIC_ARN set, it also publishes one message for
each approved run (one with a run-executions/ record) to that SNS topic.
"""

import json
import logging
import os
import time

from botocore.exceptions import ClientError

import reconcile
from reconcile import _reconcile_pending_reports

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_FINAL = {"Success", "Failed", "TimedOut", "Cancelled", "CompletedWithFailure"}


def _run_record(s3, bucket: str, execution_id: str):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=f"run-executions/{execution_id}.json")["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "AccessDenied"):
            return None
        raise


def _message(execution_id: str, record: dict, summary: dict) -> tuple:
    counts = summary["counts"]
    failed = [c for c in summary["children"] if c["status"] not in ("Success",)]
    outcome = "with failures" if counts["failed"] or summary["status"] != "Success" else "successfully"
    subject = f"Patchy run finished {outcome} ({summary['status']})"
    lines = [
        f"Run {execution_id} finished: {summary['status']}.",
        f"{counts['succeeded']} of {counts['total']} locations succeeded.",
        f"Approved by {record.get('approver') or 'unknown'} (plan {str(record.get('plan_id', ''))[:6]}, "
        f"{record.get('tool') or 'unknown tool'}).",
    ]
    if failed:
        lines.append("Locations that didn't succeed:")
        lines += [f"  {c['account']} {c['region']}: {c['status']}" for c in failed[:20]]
    ui = os.environ.get("UI_URL")
    if ui:
        lines.append(f"Open Patchy: {ui}")
    return subject[:100], "\n".join(lines)


def _deadline(context) -> "float | None":
    """Stop starting new reports with 20 seconds of the Lambda's time left."""
    try:
        return time.time() + context.get_remaining_time_in_millis() / 1000 - 20
    except Exception:
        return None


def handler(event, context):
    # The scheduled sweep reports runs whose final event arrived before their
    # pending context was written; it sends no notifications.
    if (event or {}).get("detail-type") == "Scheduled Event":
        session = reconcile._aws_session()
        bucket = f"patch-compliance-reports-{reconcile._get_hub_account_id()}"
        reconciled, _ = _reconcile_pending_reports(session.client("s3"), bucket, session,
                                                   deadline=_deadline(context))
        logger.info(f"[REPORT] sweep: reconciled {reconciled}")
        return {"reconciled": reconciled, "notified": False, "sweep": True}
    detail = (event or {}).get("detail") or {}
    execution_id = detail.get("ExecutionId", "")
    if not str(detail.get("Definition", "")).startswith("Patchy-") or detail.get("Status") not in _FINAL:
        return {"skipped": True}

    session = reconcile._aws_session()
    s3 = session.client("s3")
    bucket = f"patch-compliance-reports-{reconcile._get_hub_account_id()}"
    # Just this run: other runs are handled by their own events and the sweep.
    reconciled, _ = _reconcile_pending_reports(s3, bucket, session, only={execution_id},
                                               deadline=_deadline(context))
    logger.info(f"[REPORT] {execution_id} {detail.get('Status')}: reconciled {reconciled}")

    notified = False
    topic = os.environ.get("NOTIFY_TOPIC_ARN")
    if topic:
        record = _run_record(s3, bucket, execution_id)
        summary = reconcile.run_summary(session.client("ssm"), execution_id) if record else None
        if record and summary:
            subject, message = _message(execution_id, record, summary)
            session.client("sns").publish(TopicArn=topic, Subject=subject, Message=message)
            notified = True
    return {"reconciled": reconciled, "notified": notified}
