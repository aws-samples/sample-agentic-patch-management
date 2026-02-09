"""
Decision Logger - Audit trail for agent decisions

Provides transparency by logging every decision with full context and reasoning.
Stores logs in CloudWatch (real-time) and S3 (long-term audit).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class DecisionLogger:
    """Log agent decisions with full context for transparency and auditability."""
    
    def __init__(self, agent_name: str, s3_bucket: Optional[str] = None):
        """
        Initialize decision logger.
        
        Args:
            agent_name: Name of the agent making decisions
            s3_bucket: S3 bucket for audit trail (optional, will use compliance reports bucket)
        """
        self.agent_name = agent_name
        self.s3_client = boto3.client('s3', region_name='us-east-1')
        
        # Use provided bucket or discover the compliance reports bucket
        if s3_bucket:
            self.s3_bucket = s3_bucket
        else:
            self.s3_bucket = self._discover_compliance_bucket()
    
    def _discover_compliance_bucket(self) -> Optional[str]:
        """
        Discover the compliance reports bucket using the same logic as tools.py.
        
        Returns:
            Bucket name or None if not found
        """
        try:
            sts_client = boto3.client('sts', region_name='us-east-1')
            account_id = sts_client.get_caller_identity()['Account']
            bucket_name = f'patch-compliance-reports-{account_id}'
            
            # Verify bucket exists
            self.s3_client.head_bucket(Bucket=bucket_name)
            logger.info(f"Using compliance reports bucket for decisions: {bucket_name}")
            return bucket_name
            
        except Exception as e:
            logger.warning(f"Could not find compliance reports bucket: {e}. S3 decision logging disabled.")
            return None
    
    def log_decision(
        self,
        decision_type: str,
        context: Dict[str, Any],
        decision: str,
        reasoning: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Log an agent decision with full context.
        
        Args:
            decision_type: Type of decision (sla_calculation, routing, error_recovery, etc.)
            context: Full context that informed the decision
            decision: The actual decision made
            reasoning: Natural language explanation of why this decision was made
            metadata: Additional metadata (optional)
        
        Returns:
            Decision ID (UUID) for reference
        """
        decision_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        
        log_entry = {
            "decision_id": decision_id,
            "timestamp": timestamp,
            "agent": self.agent_name,
            "decision_type": decision_type,
            "context": context,
            "decision": decision,
            "reasoning": reasoning,
            "metadata": metadata or {}
        }
        
        # Log to CloudWatch (structured JSON for querying)
        logger.info(f"DECISION: {json.dumps(log_entry)}")
        
        # Store in S3 for long-term audit trail
        if self.s3_bucket:
            self._store_in_s3(log_entry, timestamp)
        
        return decision_id
    
    def _store_in_s3(self, log_entry: Dict[str, Any], timestamp: str) -> None:
        """
        Store decision log in S3 with date partitioning.
        
        Args:
            log_entry: The decision log entry
            timestamp: ISO format timestamp
        """
        try:
            # Parse timestamp for date partitioning
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            date_path = dt.strftime('%Y/%m/%d')
            
            # S3 key: decisions/YYYY/MM/DD/{agent}/{decision_id}.json
            s3_key = f"decisions/{date_path}/{log_entry['agent']}/{log_entry['decision_id']}.json"
            
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=json.dumps(log_entry, indent=2),
                ContentType='application/json',
                Metadata={
                    'agent': log_entry['agent'],
                    'decision_type': log_entry['decision_type'],
                    'timestamp': timestamp
                }
            )
            
            logger.debug(f"Decision logged to S3: s3://{self.s3_bucket}/{s3_key}")
            
        except Exception as e:
            logger.error(f"Failed to store decision in S3: {e}")
            # Don't fail the decision if S3 storage fails
    
    def log_routing_decision(
        self,
        user_query: str,
        specialist: str,
        keywords_matched: list,
        confidence: Optional[str] = None
    ) -> str:
        """
        Convenience method for logging routing decisions.
        
        Args:
            user_query: The user's query
            specialist: Which specialist was selected
            keywords_matched: Keywords that triggered this routing
            confidence: Confidence level (high/medium/low)
        
        Returns:
            Decision ID
        """
        return self.log_decision(
            decision_type="routing",
            context={
                "user_query": user_query,
                "keywords_matched": keywords_matched
            },
            decision=specialist,
            reasoning=f"Query matched {len(keywords_matched)} keywords for {specialist}: {', '.join(keywords_matched)}",
            metadata={"confidence": confidence or "high"}
        )
    
    def log_sla_decision(
        self,
        cve_id: str,
        severity: str,
        environment: str,
        sla_hours: int,
        window_hours: Optional[float],
        decision: str,
        compliance_framework: str
    ) -> str:
        """
        Convenience method for logging SLA-based patching decisions.
        
        Args:
            cve_id: CVE identifier
            severity: Vulnerability severity
            environment: Target environment
            sla_hours: SLA deadline in hours
            window_hours: Hours until next maintenance window (None if no window)
            decision: EMERGENCY or SCHEDULED
            compliance_framework: Which framework drove the SLA
        
        Returns:
            Decision ID
        """
        if window_hours is None:
            reasoning = (
                f"No maintenance window configured for {environment}. "
                f"SLA: {sla_hours}hr ({compliance_framework}). "
                f"Decision: {decision} patching required."
            )
        elif window_hours > sla_hours:
            reasoning = (
                f"Window ({window_hours:.1f}hr) exceeds SLA deadline ({sla_hours}hr) "
                f"by {window_hours - sla_hours:.1f} hours. "
                f"Delay unacceptable per {compliance_framework} compliance. "
                f"Decision: {decision} patching required."
            )
        else:
            reasoning = (
                f"Window ({window_hours:.1f}hr) is within SLA deadline ({sla_hours}hr). "
                f"Safe to wait for scheduled maintenance per {compliance_framework} compliance. "
                f"Decision: {decision} patching."
            )
        
        return self.log_decision(
            decision_type="sla_calculation",
            context={
                "cve_id": cve_id,
                "severity": severity,
                "environment": environment,
                "sla_hours": sla_hours,
                "window_hours": window_hours,
                "compliance_framework": compliance_framework
            },
            decision=decision,
            reasoning=reasoning
        )
    
    def log_error_recovery(
        self,
        error_type: str,
        error_details: Dict[str, Any],
        recovery_action: str,
        recovery_reasoning: str
    ) -> str:
        """
        Convenience method for logging error recovery decisions.
        
        Args:
            error_type: Type of error encountered
            error_details: Details about the error
            recovery_action: Action taken to recover
            recovery_reasoning: Why this recovery action was chosen
        
        Returns:
            Decision ID
        """
        return self.log_decision(
            decision_type="error_recovery",
            context={
                "error_type": error_type,
                "error_details": error_details
            },
            decision=recovery_action,
            reasoning=recovery_reasoning
        )


# Global decision logger instance (initialized per agent)
_decision_logger: Optional[DecisionLogger] = None


def get_decision_logger(agent_name: str) -> DecisionLogger:
    """
    Get or create the global decision logger instance.
    
    Args:
        agent_name: Name of the agent
    
    Returns:
        DecisionLogger instance
    """
    global _decision_logger
    if _decision_logger is None or _decision_logger.agent_name != agent_name:
        _decision_logger = DecisionLogger(agent_name)
    return _decision_logger
