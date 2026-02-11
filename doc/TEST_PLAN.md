# Test Plan — Intelligent Patch Automation

## Prerequisites
- Backend running: `python3 server.py` on :8000
- Frontend running: `npm run dev` on :5173
- AWS credentials configured with access to EC2, SSM, Inspector, CloudWatch, S3, SQS
- AgentCore agent deployed and reachable
- SQS FIFO queue `patch-automation-workflow-events.fifo` exists

---

## 1. Dashboard Data Accuracy

### 1.1 Vulnerability Counts
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 1 | Total matches severity sum | Load dashboard, compare KPI "X total" against HIGH + MEDIUM + CRITICAL + LOW | Total = sum of all severity counts |
| 2 | Severity counts match Inspector | Run `aws inspector2 list-findings --filter-criteria '{"findingStatus":[{"comparison":"EQUALS","value":"ACTIVE"}]}' --max-results 1` and check `nextToken` exists | Dashboard counts match full paginated Inspector data |
| 3 | Tab label matches KPI total | Compare "Vulnerabilities (N)" tab label with KPI total | Same number |
| 4 | Table shows ≤200 findings | If total > 200, table should show 200 rows with accurate severity counts above | Table capped at 200, counts uncapped |

### 1.2 Environment Cards
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 5 | All environments visible | Check environment cards include dev, test, staging, prod | All 4 environments shown |
| 6 | Instance counts match EC2 | Run `aws ec2 describe-instances --filters "Name=tag:Environment,Values=test" "Name=instance-state-name,Values=running" --query 'Reservations[].Instances[].InstanceId' --output text \| wc -w` | Dashboard count matches CLI |
| 7 | Online/offline matches SSM | Run `aws ssm describe-instance-information` and cross-reference | Online count = SSM PingStatus=Online instances |

### 1.3 Compliance & Activity
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 8 | SLA rate calculation | Check compliance tab: sla_rate = sla_met / total_reports × 100 | Math is correct |
| 9 | Activity shows last 7 days | Check activity tab timestamps | No entries older than 7 days |
| 10 | Report details sortable | Click column headers in Decisions tab | Sorting works on all columns |

### 1.4 Cache & Refresh
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 11 | Auto-cache 30s | Load dashboard, wait 10s, reload page | Data returns instantly (cached) |
| 12 | Force refresh | Click refresh button | Timestamp updates, data re-fetched |

---

## 2. Workflow Tracker — Event Flow

### 2.1 Workflow Lifecycle
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 13 | Tracker hidden initially | Load page fresh, no chat sent | No workflow tracker visible |
| 14 | Tracker appears on workflow_start | Send "patch test environment for high vulnerabilities" | Workflow tracker appears with "🔄 Workflow Tracker" header |
| 15 | CVE/severity populated | Observe header during workflow | CVE ID and severity shown (from agent's first tool call) |
| 16 | Tracker collapses on completion | Wait for workflow to finish | Tracker collapses with ✅ summary line showing duration |
| 17 | Tracker re-expandable | Click collapsed tracker | Expands to show full workflow details |
| 18 | Reset on clear chat | Click "Clear" in chat panel | Workflow tracker disappears |

### 2.2 Environment Pipeline
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 19 | Environments shown in order | Start a multi-env workflow | Pipeline shows environments in order (e.g., test → staging → prod) |
| 20 | Active env highlighted | During workflow execution | Current environment has "active" visual state |
| 21 | Completed env marked | After first env finishes | Shows "complete" state, pipeline advances |

### 2.3 Step Progress
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 22 | Steps start as pending | When env becomes active | All 7 steps show "pending" initially |
| 23 | Step activates on tool call | When agent calls get_patch_compliance | "Discovery" step becomes "active" |
| 24 | Step completes on result | When tool returns | Step becomes "complete" |
| 25 | Steps follow correct order | Watch full workflow | Discovery → Window Check → SLA Decision → Patch Execute → Health Check → Alarm Check → Report |
| 26 | Failed step shown | If a tool call fails | Step shows "failed" state with error info |

### 2.4 Instance Status Table
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 27 | Instances populated from discovery | After get_patch_compliance returns | Instance IDs appear in table with "queued" status |
| 28 | Instances update during patching | During execute_patch_operation | Status changes to "running" |
| 29 | Final status shown | After get_command_status | Each instance shows "success" or "failed" |

### 2.5 SQS Event Transport
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 30 | Events arrive via SQS | Check SQS queue during workflow (should be empty — consumed) | Events consumed by SSE endpoint, queue stays near-empty |
| 31 | No orphan messages | After workflow completes, check queue | Queue is empty (cleanup ran) |
| 32 | Queue missing gracefully | Temporarily rename queue, start workflow | Agent still works, workflow tracker just doesn't update (no crash) |

---

## 3. Agent Workflow — Chat Interactions

### 3.1 Vulnerability Analysis
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 33 | Severity query | "Show high severity vulnerabilities" | Returns findings with accurate count matching dashboard |
| 34 | Environment filter | "Show vulnerabilities in test environment" | Only test env findings returned |
| 35 | CVE-specific query | "Tell me about CVE-2024-XXXX" (use real CVE from dashboard) | Returns specific CVE details |
| 36 | Cross-reference check | Compare agent's "affected instances" count with dashboard vuln table | Numbers should align |

### 3.2 Patch Operations
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 37 | Environment normalization | "patch the testing environment" | Agent normalizes "testing" → "test" and finds instances |
| 38 | Instance count consistency | Agent reports "N instances need patches" | N matches get_patch_compliance result, cross-referenced against vulnerability findings |
| 39 | SLA decision displayed | During patching workflow | Agent shows "SLA: Xhr (FRAMEWORK) \| Window: Yhr \| Decision: SCHEDULED/EMERGENCY" |
| 40 | Scheduled patching | When window is within SLA | Agent registers task with maintenance window, reports window_task_id |
| 41 | Emergency patching | When window exceeds SLA | Agent executes immediately, reports command_id |
| 42 | Health check after patch | After emergency patching completes | Agent runs check_instance_health + check_cloudwatch_alarms |
| 43 | Compliance report generated | After patching completes | Agent calls generate_compliance_report with correct params |
| 44 | Cross-reference gap flagged | When Inspector finds 3 instances but SSM shows 2 need patches | Agent explicitly calls out the discrepancy |

### 3.3 Multi-Environment Rollout
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 45 | Sequential environments | "Patch all environments for CVE-XXXX" | Agent patches test → staging → prod in order |
| 46 | Failure stops rollout | If test env patching fails | Agent does NOT proceed to staging, offers rollback |
| 47 | Proceed command | After test env success, say "proceed" | Agent moves to next environment |

### 3.4 Error Handling
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 48 | Missing environment | "patch the banana environment" | Agent asks for clarification or reports no instances found |
| 49 | Vague query | "patch" | Agent asks for environment and/or CVE |
| 50 | Tool failure recovery | (Simulate by revoking a permission temporarily) | Agent reports error with category, doesn't crash |

---

## 4. Dashboard ↔ Agent Consistency

| # | Test | Steps | Expected |
|---|------|-------|----------|
| 51 | Vuln counts match | Compare dashboard KPI with agent's "Show all vulnerabilities" response | Same severity distribution |
| 52 | Instance counts match | Compare dashboard env cards with agent's get_patch_compliance | Same instance counts per environment |
| 53 | Post-patch dashboard update | After patching, force-refresh dashboard | Compliance tab shows new report, activity tab shows new entry |
| 54 | SLA rate updates | After generating compliance report | SLA compliance percentage recalculates |

---

## 5. UI/UX

### 5.1 Layout
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 55 | Dashboard/chat split | Load page | Dashboard ~72% left, chat ~28% right |
| 56 | Collapse dashboard | Click collapse arrow | Dashboard collapses smoothly, chat expands to full width |
| 57 | Expand dashboard | Click expand arrow | Dashboard returns with smooth transition |
| 58 | Chat never goes to bottom | Resize window to various sizes | Chat always stays on right side, never wraps below |

### 5.2 Font Consistency
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 59 | Inter everywhere | Inspect various elements (KPI, tables, chat, buttons) | All use Inter font family |
| 60 | Code blocks monospace | Check agent responses with code | Code blocks use monospace font |
| 61 | No font mixing | Inspect computed styles across components | Only Inter + monospace (for code), no system fonts |

### 5.3 Chat Panel
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 62 | Action buttons render | Agent suggests "Review patching plan for test environment" | Clickable action button appears |
| 63 | Action button sends message | Click an action button | Message sent to chat, agent responds |
| 64 | Tool labels shown | During agent processing | Shows "🔍 Consulting Vulnerability Analyst" etc. |
| 65 | Duration shown | After response completes | "Completed in X.Xs" shown |
| 66 | Error displayed | If agent errors | Error message shown in red |
| 67 | Stop streaming | Click stop button during response | Stream stops, partial response preserved |
| 68 | Clear chat | Click clear button | All messages cleared, new session ID generated |

### 5.4 Error Boundaries
| # | Test | Steps | Expected |
|---|------|-------|----------|
| 69 | Dashboard error boundary | (Simulate by returning malformed API data) | Error boundary catches, shows fallback UI |
| 70 | Chat error boundary | (Simulate by breaking SSE stream) | Chat panel shows error, doesn't crash whole app |

---

## 6. Infrastructure & Deployment

| # | Test | Steps | Expected |
|---|------|-------|----------|
| 71 | Health endpoint | `curl http://localhost:8000/api/health` | Returns `{"status": "ok", "agent_arn": "...", "region": "..."}` |
| 72 | CORS configured | Check browser console for CORS errors | No CORS errors from localhost:5173 → localhost:8000 |
| 73 | SQS queue exists | `aws sqs get-queue-url --queue-name patch-automation-workflow-events.fifo` | Returns queue URL |
| 74 | S3 compliance bucket | `aws s3 ls s3://patch-compliance-reports-{ACCOUNT_ID}/` | Bucket exists and accessible |

---

## Quick Smoke Test (5 minutes)

For a fast validation run, execute these in order:

1. Load `http://localhost:5173` — dashboard loads with environment cards and vulnerability counts
2. Verify KPI total = sum of severity counts (not 200)
3. Verify "test" environment appears in environment cards
4. Send: "Show high severity vulnerabilities in test environment"
5. Verify agent returns findings, workflow tracker does NOT appear (no patching)
6. Send: "Patch test environment for high vulnerabilities"
7. Verify workflow tracker appears, steps progress, instances populate
8. After completion, verify tracker collapses with ✅ summary
9. Force-refresh dashboard — new compliance report appears in Activity tab
10. Click collapse arrow — dashboard collapses smoothly, chat expands
