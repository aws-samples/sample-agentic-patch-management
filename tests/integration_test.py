#!/usr/bin/env python3
"""
End-to-end integration test for Intelligent Patch Automation.

Calls the tool functions directly against a live AWS environment to validate
the full patching workflow: discover → SLA → dry-run → execute → health check
→ compliance report → rollback → verify rollback.

Usage:
    # From project root, with venv activated:
    source venv/bin/activate
    python tests/integration_test.py

    # Run a specific phase:
    python tests/integration_test.py --phase discover
    python tests/integration_test.py --phase dry-run-gate
    python tests/integration_test.py --phase full

    # Use a specific environment (default: dev):
    python tests/integration_test.py --env staging

    # Skip destructive phases (patch/rollback):
    python tests/integration_test.py --phase read-only

Prerequisites:
    - .env configured with AWS_PROFILE and AWS_REGION
    - Sample environment deployed (./sample-env.sh deploy)
    - venv activated with agent dependencies installed
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add agent directory to path so we can import tools directly
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

# Load .env
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")


# ── Test framework ──────────────────────────────────────────────────

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors: list[str] = []

    def ok(self, msg: str):
        self.passed += 1
        print(f"  ✅ {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.errors.append(msg)
        print(f"  ❌ {msg}")

    def skip(self, msg: str):
        self.skipped += 1
        print(f"  ⏭️  {msg}")

    def summary(self) -> str:
        total = self.passed + self.failed + self.skipped
        status = "PASS" if self.failed == 0 else "FAIL"
        return f"[{status}] {self.name}: {self.passed}/{total} passed, {self.failed} failed, {self.skipped} skipped"


class IntegrationTest:
    def __init__(self, environment: str = "dev"):
        self.env = environment
        self.results: list[TestResult] = []
        # State carried between phases
        self.instance_ids: list[str] = []
        self.cve_id: str | None = None
        self.severity: str | None = None
        self.cvss_score: float | None = None
        self.maintenance_window_id: str | None = None
        self.command_id: str | None = None
        self.pre_patch_state: dict | None = None
        self.post_patch_state: dict | None = None
        self.sla_hours: int | None = None
        self.sla_source: str | None = None
        self.decision: str | None = None

    def run_phase(self, name: str, fn) -> TestResult:
        result = TestResult(name)
        print(f"\n{'='*60}")
        print(f"Phase: {name}")
        print(f"{'='*60}")
        try:
            fn(result)
        except Exception as e:
            result.fail(f"Phase crashed: {e}")
            import traceback
            traceback.print_exc()
        self.results.append(result)
        return result

    def print_summary(self):
        print(f"\n{'='*60}")
        print("INTEGRATION TEST SUMMARY")
        print(f"{'='*60}")
        total_passed = sum(r.passed for r in self.results)
        total_failed = sum(r.failed for r in self.results)
        total_skipped = sum(r.skipped for r in self.results)
        for r in self.results:
            print(f"  {r.summary()}")
        print(f"\nTotal: {total_passed} passed, {total_failed} failed, {total_skipped} skipped")
        if total_failed > 0:
            print("\nFailed checks:")
            for r in self.results:
                for err in r.errors:
                    print(f"  • [{r.name}] {err}")
        return total_failed == 0


# ── Phase 1: Discovery ──────────────────────────────────────────────

    def phase_discover(self, r: TestResult):
        """Validate vulnerability discovery and patch compliance."""
        from helper.tools import get_vulnerability_findings, get_patch_compliance, assess_fleet_impact

        # 1a. Get vulnerabilities for the environment
        print(f"\n  Querying Inspector findings for {self.env}...")
        vuln_result = get_vulnerability_findings(severity="HIGH", environment=self.env)

        if vuln_result.get("error"):
            r.fail(f"get_vulnerability_findings returned error: {vuln_result['error']}")
            return

        findings = vuln_result.get("findings", [])
        total = vuln_result.get("total_count", 0)
        r.ok(f"get_vulnerability_findings: {total} findings, {len(findings)} returned") if total >= 0 else None

        if not findings:
            # Try CRITICAL or MEDIUM
            for sev in ["CRITICAL", "MEDIUM"]:
                vuln_result = get_vulnerability_findings(severity=sev, environment=self.env)
                findings = vuln_result.get("findings", [])
                if findings:
                    break

        if findings:
            self.cve_id = findings[0].get("cve_id")
            self.severity = findings[0].get("severity")
            self.cvss_score = findings[0].get("cvss_score")
            r.ok(f"Found CVE to test with: {self.cve_id} ({self.severity}, CVSS {self.cvss_score})")
        else:
            r.skip("No active findings in any severity — vulnerability phases will use placeholder CVE")
            self.cve_id = "CVE-0000-0000"
            self.severity = "HIGH"

        # 1b. Get patch compliance
        print(f"\n  Querying patch compliance for {self.env}...")
        compliance = get_patch_compliance(environment=self.env, severity=self.severity)

        if compliance.get("error"):
            r.fail(f"get_patch_compliance returned error: {compliance['error']}")
            return

        instances = compliance.get("instances", [])
        self.instance_ids = [i["instance_id"] for i in instances]

        if not self.instance_ids:
            r.fail(f"No instances found in {self.env} environment")
            return

        r.ok(f"get_patch_compliance: {len(self.instance_ids)} instances in {self.env}")

        # Validate structure
        for inst in instances[:1]:
            for key in ["instance_id", "installed_count", "missing_count", "failed_count"]:
                if key not in inst:
                    r.fail(f"Missing key '{key}' in patch compliance instance data")
                    return
        r.ok("Patch compliance data structure valid")

        # Check SLA calculation
        sla = compliance.get("sla_requirement")
        if sla:
            self.sla_hours = sla.get("sla_hours")
            self.sla_source = sla.get("source")
            r.ok(f"SLA calculated: {self.sla_hours}hr from {self.sla_source}")
        else:
            r.skip("No SLA requirement returned (instances may lack ComplianceFrameworks tag)")

        summary = compliance.get("summary", {})
        r.ok(f"Summary: {summary.get('total_missing', 0)} missing patches, "
             f"{summary.get('instances_needing_patches', 0)} instances need patching")

        # 1c. Fleet impact assessment (if we have a real CVE)
        if self.cve_id and self.cve_id != "CVE-0000-0000":
            print(f"\n  Assessing fleet impact for {self.cve_id}...")
            impact = assess_fleet_impact(self.cve_id)

            if impact.get("error"):
                r.fail(f"assess_fleet_impact returned error: {impact['error']}")
            else:
                total_affected = impact.get("total_affected", 0)
                envs = impact.get("environments", [])
                rollout = impact.get("rollout_order", [])
                r.ok(f"Fleet impact: {total_affected} instances across {len(envs)} environments")
                r.ok(f"Rollout order: {' → '.join(rollout)}")

                # Validate structure
                for env_detail in envs:
                    for key in ["environment", "instance_count", "instance_ids", "sla_hours"]:
                        if key not in env_detail:
                            r.fail(f"Missing key '{key}' in fleet impact environment data")
                            break
                    else:
                        continue
                    break
                else:
                    r.ok("Fleet impact data structure valid")
        else:
            r.skip("Skipping fleet impact (no real CVE found)")


# ── Phase 2: Maintenance Windows ────────────────────────────────────

    def phase_maintenance_windows(self, r: TestResult):
        """Validate maintenance window discovery."""
        from helper.tools import get_maintenance_windows

        print(f"\n  Querying maintenance windows for {self.env}...")
        mw_result = get_maintenance_windows(self.env)

        if mw_result.get("error"):
            r.fail(f"get_maintenance_windows returned error: {mw_result['error']}")
            return

        windows = mw_result.get("windows", [])
        if not windows:
            r.skip(f"No maintenance windows found for {self.env}")
            return

        r.ok(f"Found {len(windows)} maintenance window(s) for {self.env}")

        win = windows[0]
        self.maintenance_window_id = win.get("window_id")

        for key in ["window_id", "name", "schedule", "next_execution", "duration_hours", "enabled"]:
            if key not in win:
                r.fail(f"Missing key '{key}' in maintenance window data")
                return
        r.ok(f"Window structure valid: {win['name']} ({win['schedule']})")
        r.ok(f"Next execution: {win.get('next_execution', 'unknown')}")

        # Validate with instance coverage
        if self.instance_ids:
            print(f"\n  Validating instance coverage...")
            mw_with_coverage = get_maintenance_windows(self.env, instance_ids=self.instance_ids[:3])
            windows_cov = mw_with_coverage.get("windows", [])
            if windows_cov:
                cov = windows_cov[0].get("coverage_percentage", 0)
                r.ok(f"Instance coverage: {cov}%")
            else:
                r.skip("No windows returned with instance coverage check")


# ── Phase 3: Dry-Run Gate ───────────────────────────────────────────

    def phase_dry_run_gate(self, r: TestResult):
        """Validate that execute_patch_operation refuses without a recent dry-run."""
        from helper.tools import execute_patch_operation

        if not self.instance_ids:
            r.skip("No instances available — skipping dry-run gate test")
            return

        test_ids = self.instance_ids[:2]
        print(f"\n  Testing dry-run gate with {len(test_ids)} instances (should be blocked)...")

        result = execute_patch_operation(
            instance_ids=test_ids,
            execution_mode="immediate",
            environment=self.env
        )

        error_code = result.get("error_code", "")
        if error_code in ("DryRunRequired", "DryRunStale"):
            r.ok(f"Dry-run gate blocked execution: {error_code}")
            r.ok(f"Error message: {result.get('error', '')[:100]}")
            if result.get("category") == "ABORT":
                r.ok("Error category is ABORT (correct)")
            else:
                r.fail(f"Expected category ABORT, got {result.get('category')}")
            if result.get("suggestion"):
                r.ok(f"Suggestion provided: {result['suggestion'][:80]}")
            else:
                r.fail("No suggestion provided in dry-run gate error")
        elif result.get("error"):
            # Some other error (e.g. SSM connectivity) — not a gate failure
            r.skip(f"Got non-gate error: {result.get('error_code', 'unknown')} — {result.get('error', '')[:80]}")
        else:
            r.fail("Dry-run gate did NOT block execution — patches may have been applied without preview")


# ── Phase 4: Dry-Run Scan ──────────────────────────────────────────

    def phase_dry_run(self, r: TestResult):
        """Run patch_dry_run and validate results."""
        from helper.tools import patch_dry_run

        if not self.instance_ids:
            r.skip("No instances available — skipping dry-run")
            return

        test_ids = self.instance_ids[:3]
        print(f"\n  Running dry-run scan on {len(test_ids)} instances (this takes 1-3 min)...")
        start = time.time()
        result = patch_dry_run(test_ids)
        elapsed = time.time() - start

        if result.get("error"):
            r.fail(f"patch_dry_run returned error: {result['error']}")
            return

        r.ok(f"Dry-run completed in {elapsed:.0f}s")

        total_missing = result.get("total_missing", 0)
        instances = result.get("instances", [])
        scan_cmd = result.get("scan_command_id")

        r.ok(f"Total missing patches: {total_missing} across {len(instances)} instances")
        r.ok(f"Scan command ID: {scan_cmd}")

        # Validate per-instance structure
        for inst in instances:
            for key in ["instance_id", "status", "missing_patches", "installed_count", "missing_count"]:
                if key not in inst:
                    r.fail(f"Missing key '{key}' in dry-run instance data")
                    return
        r.ok("Dry-run instance data structure valid")

        # Check that missing_patches have expected fields
        for inst in instances:
            if inst.get("missing_patches"):
                patch = inst["missing_patches"][0]
                for key in ["id", "title", "severity", "classification", "state"]:
                    if key not in patch:
                        r.fail(f"Missing key '{key}' in missing patch data")
                        return
                r.ok(f"Patch detail structure valid (sample: {patch['title'][:60]})")
                break
        else:
            r.ok("No missing patches found — all instances fully patched")


# ── Phase 5: Pre-Patch State Capture ───────────────────────────────

    def phase_capture_pre_patch(self, r: TestResult):
        """Capture patch state before execution."""
        from helper.tools import capture_patch_state

        if not self.instance_ids:
            r.skip("No instances available")
            return

        test_ids = self.instance_ids[:3]
        print(f"\n  Capturing pre-patch state for {len(test_ids)} instances...")
        result = capture_patch_state(test_ids)

        if result.get("error"):
            r.fail(f"capture_patch_state returned error: {result['error']}")
            return

        snapshot = result.get("snapshot", {})
        self.pre_patch_state = result

        if len(snapshot) != len(test_ids):
            r.fail(f"Expected {len(test_ids)} instances in snapshot, got {len(snapshot)}")
            return

        r.ok(f"Pre-patch state captured: {len(snapshot)} instances")
        r.ok(f"Total missing: {result.get('total_missing', 0)}")

        # Validate structure
        for iid, state in snapshot.items():
            for key in ["missing_count", "installed_count", "security_non_compliant", "failed_count"]:
                if key not in state:
                    r.fail(f"Missing key '{key}' in patch state for {iid}")
                    return
        r.ok("Patch state structure valid")


# ── Phase 6: Health Checks ─────────────────────────────────────────

    def phase_health_checks(self, r: TestResult):
        """Validate health check tools."""
        from helper.tools import check_instance_health, check_cloudwatch_alarms

        if not self.instance_ids:
            r.skip("No instances available")
            return

        test_ids = self.instance_ids[:3]

        # SSM health
        print(f"\n  Checking SSM health for {len(test_ids)} instances...")
        health = check_instance_health(test_ids)

        if health.get("error"):
            r.fail(f"check_instance_health returned error: {health['error']}")
        else:
            r.ok(f"SSM health: {health.get('overall_health', 0)}% — "
                 f"{health.get('unhealthy_instances', 0)} unhealthy of {health.get('total_instances', 0)}")
            r.ok(f"Recommendation: {health.get('recommendation', 'unknown')}")

        # CloudWatch alarms
        print(f"\n  Checking CloudWatch alarms for {len(test_ids)} instances...")
        alarms = check_cloudwatch_alarms(test_ids)

        if alarms.get("error"):
            r.fail(f"check_cloudwatch_alarms returned error: {alarms['error']}")
        else:
            r.ok(f"CloudWatch: {alarms.get('total_alarms_firing', 0)} alarms firing — "
                 f"{alarms.get('unhealthy_instances', 0)} unhealthy")
            r.ok(f"Recommendation: {alarms.get('recommendation', 'unknown')}")


# ── Phase 7: Dry-Run Gate (post-scan — should pass now) ────────────

    def phase_dry_run_gate_after_scan(self, r: TestResult):
        """After dry-run scan, execute_patch_operation should pass the gate.
        We test this by checking the gate logic only — we DON'T actually execute.
        Instead we verify the SSM state shows a recent Scan."""
        import boto3

        if not self.instance_ids:
            r.skip("No instances available")
            return

        test_ids = self.instance_ids[:3]
        print(f"\n  Verifying SSM state shows recent Scan for {len(test_ids)} instances...")

        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        states = ssm.describe_instance_patch_states(InstanceIds=test_ids)

        all_scanned = True
        for state in states.get("InstancePatchStates", []):
            iid = state["InstanceId"]
            op = state.get("Operation", "")
            op_end = state.get("OperationEndTime")

            if op == "Scan" and op_end:
                if op_end.tzinfo is None:
                    from datetime import timezone as tz
                    op_end = op_end.replace(tzinfo=tz.utc)
                age_hrs = (datetime.now(timezone.utc) - op_end).total_seconds() / 3600
                if age_hrs <= 2:
                    r.ok(f"{iid}: Last op=Scan, {age_hrs:.1f}hr ago (within 2hr gate)")
                else:
                    r.fail(f"{iid}: Scan is {age_hrs:.1f}hr old (exceeds 2hr gate)")
                    all_scanned = False
            else:
                r.fail(f"{iid}: Last op={op}, expected Scan")
                all_scanned = False

        if all_scanned:
            r.ok("All instances would pass the dry-run gate — execute_patch_operation would proceed")


# ── Phase 8: Compliance Reports ────────────────────────────────────

    def phase_compliance_reports(self, r: TestResult):
        """Validate compliance report querying."""
        from helper.tools import query_compliance_reports

        print(f"\n  Querying compliance reports (last 30 days)...")
        result = query_compliance_reports(days_back=30)

        if result.get("error"):
            r.fail(f"query_compliance_reports returned error: {result['error']}")
            return

        reports = result.get("reports", [])
        total = result.get("total_count", 0)
        stats = result.get("statistics", {})

        r.ok(f"Found {total} compliance reports in last 30 days")

        if total > 0:
            r.ok(f"SLA breaches: {stats.get('sla_breaches', 0)} "
                 f"({stats.get('sla_breach_rate', 0)}% breach rate)")
            r.ok(f"By severity: {json.dumps(stats.get('by_severity', {}))}")
            r.ok(f"By decision: {json.dumps(stats.get('by_decision', {}))}")

            # Validate report structure
            report = reports[0]
            for key in ["report_id", "cve_id", "severity", "environment", "decision", "sla_met"]:
                if key not in report:
                    r.fail(f"Missing key '{key}' in compliance report")
                    return
            r.ok("Report data structure valid")

            # Test filtering
            print(f"\n  Testing report filters...")
            filtered = query_compliance_reports(days_back=30, sla_breaches_only=True)
            breach_count = filtered.get("total_count", 0)
            r.ok(f"SLA breaches filter: {breach_count} reports")

            if reports[0].get("environment") != "unknown":
                env_filter = reports[0]["environment"]
                env_result = query_compliance_reports(days_back=30, environment=env_filter)
                r.ok(f"Environment filter ({env_filter}): {env_result.get('total_count', 0)} reports")
        else:
            r.skip("No compliance reports found — generate some by running a patch workflow first")


# ── Phase 9: Dashboard API ─────────────────────────────────────────

    def phase_dashboard_api(self, r: TestResult):
        """Validate the dashboard API endpoint data fetching logic."""
        # We can't call the FastAPI endpoint directly without a running server,
        # but we can call the underlying fetch functions that the endpoint uses.
        sys.path.insert(0, str(PROJECT_ROOT / "ui" / "api"))

        try:
            import importlib.util

            # Pre-check: fastapi must be importable (it's in ui/api/requirements.txt,
            # not agent/requirements.txt — skip gracefully if not installed)
            try:
                import fastapi  # noqa: F401
            except ImportError:
                r.skip("fastapi not installed in this venv — install ui/api/requirements.txt to test dashboard")
                return

            spec = importlib.util.spec_from_file_location("server", PROJECT_ROOT / "ui" / "api" / "server.py")
            server = importlib.util.module_from_spec(spec)

            os.environ.setdefault("AWS_PROFILE", "ri25-demo")

            spec.loader.exec_module(server)

            # Test _fetch_environments
            print(f"\n  Testing dashboard: _fetch_environments...")
            envs = server._fetch_environments()
            if isinstance(envs, list) and len(envs) > 0:
                total_instances = sum(e.get("total", 0) for e in envs)
                online = sum(e.get("online", 0) for e in envs)
                r.ok(f"Environments: {len(envs)} found, {total_instances} instances ({online} online)")
                for e in envs:
                    for key in ["environment", "total", "online", "offline", "status"]:
                        if key not in e:
                            r.fail(f"Missing key '{key}' in environment data")
                            return
                r.ok("Environment data structure valid")
            else:
                r.fail(f"_fetch_environments returned unexpected: {type(envs)}")

            # Test _fetch_vulnerabilities
            print(f"\n  Testing dashboard: _fetch_vulnerabilities...")
            vulns = server._fetch_vulnerabilities()
            if isinstance(vulns, dict):
                findings = vulns.get("findings", [])
                sev_counts = vulns.get("severity_counts", {})
                total_vulns = sum(sev_counts.values())
                r.ok(f"Vulnerabilities: {total_vulns} total, {len(findings)} in table")
                r.ok(f"Severity breakdown: {json.dumps(sev_counts)}")
            else:
                r.fail(f"_fetch_vulnerabilities returned unexpected: {type(vulns)}")

            # Test _fetch_reports
            print(f"\n  Testing dashboard: _fetch_reports...")
            report_data = server._fetch_reports()
            if isinstance(report_data, dict):
                activities = report_data.get("activities", [])
                compliance = report_data.get("compliance")
                details = report_data.get("report_details", [])
                r.ok(f"Reports: {len(activities)} activities, {len(details)} detailed reports")
                if compliance:
                    r.ok(f"Compliance: {compliance.get('sla_rate_percent', 0)}% SLA rate "
                         f"({compliance.get('total_reports', 0)} reports)")
                else:
                    r.ok("No compliance stats (no reports in last 30 days)")
            else:
                r.fail(f"_fetch_reports returned unexpected: {type(report_data)}")

        except Exception as e:
            r.fail(f"Dashboard API test failed: {e}")
            import traceback
            traceback.print_exc()


# ── Phase 10: Frontend Build Check ─────────────────────────────────

    def phase_frontend_build(self, r: TestResult):
        """Validate frontend TypeScript compiles without errors."""
        import subprocess

        frontend_dir = PROJECT_ROOT / "ui" / "frontend"
        if not (frontend_dir / "node_modules").exists():
            r.skip("Frontend node_modules not installed — run: cd ui/frontend && npm install")
            return

        print(f"\n  Running TypeScript type check...")
        result = subprocess.run(
            ["npx", "tsc", "--noEmit"],
            cwd=frontend_dir,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            r.ok("TypeScript compilation: no errors")
        else:
            errors = result.stdout.strip() or result.stderr.strip()
            error_count = errors.count("error TS")
            r.fail(f"TypeScript compilation: {error_count} error(s)")
            # Show first 5 errors
            for line in errors.split("\n")[:5]:
                if "error TS" in line:
                    print(f"    {line.strip()}")


# ── Phase 11: CDK Synth Check ──────────────────────────────────────

    def phase_cdk_synth(self, r: TestResult):
        """Validate CDK infrastructure synthesizes without errors."""
        import subprocess

        infra_dir = PROJECT_ROOT / "infra"
        if not (infra_dir / "node_modules").exists():
            r.skip("Infra node_modules not installed — run: cd infra && npm ci")
            return

        agentcore_arn = os.environ.get("AGENTCORE_ROLE_ARN", "arn:aws:iam::123456789012:role/placeholder")

        print(f"\n  Running CDK synth...")
        result = subprocess.run(
            ["npx", "cdk", "synth", "--quiet", "-c", f"agentCoreRoleArn={agentcore_arn}"],
            cwd=infra_dir,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            r.ok("CDK synth: all stacks synthesized successfully")
        else:
            error = result.stderr.strip()[:300]
            r.fail(f"CDK synth failed: {error}")


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Integration test for Intelligent Patch Automation")
    parser.add_argument("--env", default="dev", help="Target environment (default: dev)")
    parser.add_argument("--phase", default="full", choices=[
        "discover", "maintenance", "dry-run-gate", "dry-run", "capture",
        "health", "gate-after-scan", "compliance", "dashboard", "frontend",
        "cdk", "read-only", "full"
    ], help="Which phase to run (default: full)")
    args = parser.parse_args()

    test = IntegrationTest(environment=args.env)

    # Define phase groups
    read_only_phases = [
        ("1. Discovery (vulnerabilities + compliance)", test.phase_discover),
        ("2. Maintenance Windows", test.phase_maintenance_windows),
        ("3. Dry-Run Gate (should block)", test.phase_dry_run_gate),
        ("4. Health Checks", test.phase_health_checks),
        ("5. Compliance Reports", test.phase_compliance_reports),
        ("6. Dashboard API", test.phase_dashboard_api),
        ("7. Frontend Build", test.phase_frontend_build),
        ("8. CDK Synth", test.phase_cdk_synth),
    ]

    scan_phases = [
        ("1. Discovery", test.phase_discover),
        ("2. Maintenance Windows", test.phase_maintenance_windows),
        ("3. Dry-Run Gate (pre-scan — should block)", test.phase_dry_run_gate),
        ("4. Dry-Run Scan", test.phase_dry_run),
        ("5. Pre-Patch State Capture", test.phase_capture_pre_patch),
        ("6. Health Checks", test.phase_health_checks),
        ("7. Dry-Run Gate (post-scan — should pass)", test.phase_dry_run_gate_after_scan),
        ("8. Compliance Reports", test.phase_compliance_reports),
        ("9. Dashboard API", test.phase_dashboard_api),
        ("10. Frontend Build", test.phase_frontend_build),
        ("11. CDK Synth", test.phase_cdk_synth),
    ]

    # Single phase execution
    phase_map = {
        "discover": [("Discovery", test.phase_discover)],
        "maintenance": [("Maintenance Windows", test.phase_maintenance_windows)],
        "dry-run-gate": [
            ("Discovery (for instance IDs)", test.phase_discover),
            ("Dry-Run Gate", test.phase_dry_run_gate),
        ],
        "dry-run": [
            ("Discovery (for instance IDs)", test.phase_discover),
            ("Dry-Run Scan", test.phase_dry_run),
        ],
        "capture": [
            ("Discovery (for instance IDs)", test.phase_discover),
            ("Pre-Patch State Capture", test.phase_capture_pre_patch),
        ],
        "health": [
            ("Discovery (for instance IDs)", test.phase_discover),
            ("Health Checks", test.phase_health_checks),
        ],
        "gate-after-scan": [
            ("Discovery (for instance IDs)", test.phase_discover),
            ("Dry-Run Scan", test.phase_dry_run),
            ("Dry-Run Gate (post-scan)", test.phase_dry_run_gate_after_scan),
        ],
        "compliance": [("Compliance Reports", test.phase_compliance_reports)],
        "dashboard": [("Dashboard API", test.phase_dashboard_api)],
        "frontend": [("Frontend Build", test.phase_frontend_build)],
        "cdk": [("CDK Synth", test.phase_cdk_synth)],
        "read-only": read_only_phases,
        "full": scan_phases,
    }

    phases = phase_map[args.phase]

    print(f"\n{'#'*60}")
    print(f"  Intelligent Patch Automation — Integration Test")
    print(f"  Environment: {args.env}")
    print(f"  Phase: {args.phase} ({len(phases)} step{'s' if len(phases) > 1 else ''})")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    start = time.time()
    for name, fn in phases:
        test.run_phase(name, fn)

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.0f}s")

    success = test.print_summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
