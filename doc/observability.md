# Observability Guide

**Purpose**: How to monitor and query agent behavior  
**Audience**: Operations teams

---

## Decision Logs

The agent logs every decision to CloudWatch + S3 in structured JSON format.

### Log Format
```json
{
  "decision_id": "abc-123",
  "timestamp": "2026-02-04T10:30:00Z",
  "agent": "patch-manager",
  "decision_type": "sla_calculation",
  "decision": "EMERGENCY",
  "reasoning": "Window (36hr) exceeds SLA (12hr) by 24 hours",
  "context": {"cve_id": "CVE-2024-1234", "severity": "CRITICAL", ...}
}
```

### Decision Types
- `sla_calculation` - Patching urgency
- `routing` - Supervisor routing
- `error_recovery` - Error handling

---

## CloudWatch Insights Queries

### All Decisions (Last 24h)
```
fields @timestamp, agent, decision_type, decision, reasoning
| filter @message like /DECISION:/
| sort @timestamp desc
```

### Emergency Patches
```
fields @timestamp, decision, reasoning
| filter @message like /DECISION:/
| filter decision_type = "sla_calculation" and decision = "EMERGENCY"
```

### Routing Decisions
```
fields @timestamp, decision as specialist, reasoning
| filter @message like /DECISION:/
| filter decision_type = "routing"
```

---

## Athena Queries

### Create Table
```sql
CREATE EXTERNAL TABLE agent_decisions (
  decision_id string,
  timestamp string,
  agent string,
  decision_type string,
  decision string,
  reasoning string,
  context struct<cve_id:string, severity:string, environment:string>
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://your-bucket/decisions/';
```

### SLA Compliance Rate
```sql
SELECT 
  context.environment,
  COUNT(*) as total,
  SUM(CASE WHEN decision = 'EMERGENCY' THEN 1 ELSE 0 END) as emergency,
  ROUND(100.0 * SUM(CASE WHEN decision = 'SCHEDULED' THEN 1 ELSE 0 END) / COUNT(*), 2) as sla_compliance_pct
FROM agent_decisions
WHERE decision_type = 'sla_calculation'
GROUP BY context.environment;
```

---

## Third-Party Tools

### Splunk
```spl
index=aws sourcetype="aws:cloudwatch:json" DECISION
| spath input=message path=decision_type
| table _time, decision_type, decision, reasoning
```

### Datadog
```
service:intelligent-patch-automation @message:DECISION
```

---

## Cost
- CloudWatch: ~$0.03/month (1000 decisions/day)
- S3: ~$0.001/month
- Athena: ~$0.0003/query

**Total**: <$1/month

---

See `agent/helper/decision_logger.py` for implementation details.
