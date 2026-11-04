import { apiFetch } from "./http";
import type { Location } from "./view";

/** The change record a plan stores (agent/helper/plan_view.py). */
export interface PlanCheck { key: string; label: string; detail: string; state: "pass" | "warn" | "info" }
export interface PlanSection { key: string; label: string; text: string }
export interface PlanTarget { instance_id: string; name: string; account: string; region: string; pending_sync?: boolean }
/** Whether a window or patch policy patches each location's targets before the SLA deadline (agent/helper/coverage.py). */
export interface CoverageRow {
  account: string; region: string; instances: number; covered: number;
  schedule: string; before_deadline: string; verdict: string;
}
export interface PlanView {
  kind: "patch" | "rollback" | "window";
  headline: string;
  summary: string;
  environment: string;
  checks: PlanCheck[];
  sections: PlanSection[];
  locations: Location[];
  targets: PlanTarget[];
  targets_total: number;
  notes?: string[];
  coverage?: { rows: CoverageRow[]; deadline: string };
}

/** GET /api/plans/{id}: a plan this conversation made. */
export interface PlanDetail {
  plan_id: string;
  plan_hash: string;
  tool: string;
  status: "awaiting_approval" | "approved" | "cancelled" | "expired";
  created_at: string;
  expires_at: string;
  requested_by: string;
  view: PlanView | null;
  decision: { by: string | null; at: string | null } | null;
  execution_ids: string[];
  /** The runtime claimed the plan, so its run started (or tried to). */
  claimed: boolean;
  /** Approved: until when the runtime still accepts the approval (10 minutes, or the plan's expiry if sooner). */
  approval_expires_at?: string | null;
}

export async function fetchPlan(planId: string, sessionId: string): Promise<PlanDetail | null> {
  const res = await apiFetch(`/api/plans/${planId}?session_id=${encodeURIComponent(sessionId)}`);
  return res.ok ? ((await res.json()) as PlanDetail) : null;
}

/** GET /api/changes: every operator's plans from the last week, for auditors. Read-only. */
export interface AuditChange {
  plan_id: string;
  headline: string;
  kind: string | null;
  environment: string | null;
  requested_by: string | null;
  status: "awaiting_approval" | "approved" | "cancelled" | "expired";
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  execution_ids: string[];
}

export async function fetchAllChanges(): Promise<{ changes: AuditChange[]; days: number; truncated: boolean } | null> {
  const res = await apiFetch("/api/changes");
  return res.ok ? await res.json() : null;
}
