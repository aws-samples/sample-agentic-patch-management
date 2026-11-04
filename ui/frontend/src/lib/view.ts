/**
 * Plain-language helpers for the detail views: plan and run status, the reach
 * grid, run progress, report rows and finding filters. Pure functions: no React
 * and no runtime imports.
 */
import type { ResultCard } from "../hooks/useAgentStream";
import type { DashboardData, VulnerabilityFinding, RunRow } from "./api";
import type { PlanDetail } from "./plans";

export type Tone = "ok" | "watch" | "risk" | "change" | null;

export function minutesLeft(expiresAt: string | null | undefined, now = Date.now()): number {
  if (!expiresAt) return 0;
  return Math.max(0, Math.floor((Date.parse(expiresAt) - now) / 60000));
}

/** A plan's status for the status tag and the chat reference. */
export function planStatus(status: string | undefined, expiresAt: string | null | undefined, now = Date.now()):
    { text: string; tone: Tone; waiting: boolean } {
  switch (status) {
    case "approved": return { text: "Approved", tone: "ok", waiting: false };
    case "cancelled": return { text: "Cancelled. Nothing ran.", tone: null, waiting: false };
    case "expired": return { text: "Expired. Ask for a fresh plan.", tone: null, waiting: false };
    case "submitting": return { text: "Submitting the approval", tone: "change", waiting: false };
    case "unknown": return { text: "Result unknown. Check the run.", tone: "watch", waiting: false };
  }
  if (expiresAt && Date.parse(expiresAt) <= now) return { text: "Expired. Ask for a fresh plan.", tone: null, waiting: false };
  return { text: `Waiting for approval, ${minutesLeft(expiresAt, now)} min left`, tone: "change", waiting: true };
}

const RUN_STATES: Record<string, { text: string; tone: Tone; active: boolean }> = {
  Pending: { text: "Waiting", tone: null, active: true },
  Waiting: { text: "Waiting", tone: null, active: true },
  InProgress: { text: "Running", tone: "change", active: true },
  Cancelling: { text: "Stopping", tone: "watch", active: true },
  Success: { text: "Finished", tone: "ok", active: false },
  Cancelled: { text: "Stopped", tone: "watch", active: false },
  Failed: { text: "Failed", tone: "risk", active: false },
  TimedOut: { text: "Timed out", tone: "risk", active: false },
  CompletedWithFailure: { text: "Finished with failures", tone: "risk", active: false },
  Rejected: { text: "Rejected", tone: "risk", active: false },
  // Not an SSM status: the API's word for a run that ended (stopped) while commands it sent are still going.
  CommandsFinishing: { text: "Stopped; instances already patching are finishing", tone: "watch", active: true },
};

/** An SSM Automation status in operator words. */
export function runState(status: string | undefined): { text: string; tone: Tone; active: boolean } {
  return RUN_STATES[status ?? ""] ?? { text: status || "Unknown", tone: null, active: false };
}

/** What Stop does, for the run view. A location already started has handed its instances to Run Command,
 * which may still reach the ones it hasn't yet. */
export const STOP_TEXT = {
  offer: "Locations not yet started don't start. In a location already started, Run Command may still reach its remaining instances. Instances already patching finish.",
  requested: "Stop requested. Locations not yet started don't start; in one already started, Run Command may still reach its remaining instances. Instances already patching finish.",
  confirm: "Locations not yet started don't start, in any account or Region of this run. In a location already started, Run Command may still reach its remaining instances. Instances already patching finish, and stay patched.",
};

/** A run's overall state across its executions, for the run view's tag. */
export function overallRunState(runs: Array<{ status: string; active: boolean }>, stopping: boolean): { text: string; tone: Tone } {
  if (!runs.length) return { text: stopping ? "Stopping" : "Starting", tone: "change" };
  if (runs.some((r) => r.active && r.status !== "CommandsFinishing")) {
    return stopping ? { text: "Stopping", tone: "watch" } : { text: "Running", tone: "change" };
  }
  if (runs.some((r) => r.active)) return runState("CommandsFinishing");
  const states = runs.map((r) => runState(r.status));
  if (states.some((s) => s.tone === "risk")) return { text: states.find((s) => s.tone === "risk")!.text, tone: "risk" };
  if (runs.some((r) => r.status === "Cancelled")) return { text: "Stopped", tone: "watch" };
  return { text: "Finished", tone: "ok" };
}

export interface Location { account: string; region: string; instances: number }

/** Accounts as rows, Regions as columns; count is null where the change doesn't reach. */
export function reachGrid(locations: Location[]) {
  const accounts = [...new Set(locations.map((l) => l.account))].sort();
  const regions = [...new Set(locations.map((l) => l.region))].sort();
  const counts = new Map(locations.map((l) => [`${l.account}|${l.region}`, l.instances]));
  return { accounts, regions, count: (a: string, r: string): number | null => counts.get(`${a}|${r}`) ?? null };
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/**
 * Progress of a run from its per-location status. SSM reports progress by
 * location, so instances count as done when their location has finished.
 */
export function runProgress(children: Array<{ account: string; region: string; status: string }>, planned: Location[]) {
  const size = new Map(planned.map((l) => [`${l.account}|${l.region}`, l.instances]));
  let total = 0, done = 0, doing = 0, failed = 0, locationsDone = 0, failedLocations = 0, known = true;
  for (const c of children) {
    const n = size.get(`${c.account}|${c.region}`);
    if (n === undefined) known = false;
    const k = n ?? 0;
    total += k;
    const s = runState(c.status);
    if (c.status === "Success") { done += k; locationsDone += 1; }
    else if (s.tone === "risk") { failed += k; failedLocations += 1; }
    else if (s.active) doing += k;
  }
  const locations = children.length;
  let text: string;
  if (!known || total === 0) {
    const running = children.filter((c) => runState(c.status).active).length;
    text = running ? `Running in ${plural(running, "location")}.`
      : `Finished in ${locationsDone} of ${plural(locations, "location")}`
        + (failedLocations ? `; ${plural(failedLocations, "location")} failed.` : ".");
  } else {
    text = `Finished in ${locationsDone} of ${plural(locations, "location")} (${done} of ${plural(total, "instance")})`;
    text += failedLocations ? `; ${plural(failedLocations, "location")} failed.` : ".";
  }
  return { total, done, doing, failed, locations, locationsDone, text };
}

type ReportLike = Pick<RunRow, "operation_type" | "cve_id" | "environment" | "instance_count" | "status"
  | "success_count" | "failure_count" | "sla_met">;

export function reportTitle(r: Pick<RunRow, "operation_type" | "cve_id" | "environment"> & { instance_count?: number }): string {
  const known = r.environment && r.environment !== "unknown" && r.environment !== "N/A";
  const n = r.instance_count ?? 0;
  const on = known ? `in ${r.environment}` : `on ${plural(n, "instance")}`;
  if (r.operation_type === "rollback") return known ? `Roll back ${r.environment}` : `Roll back ${plural(n, "instance")}`;
  if (r.cve_id && r.cve_id !== "N/A") return `Install the ${r.cve_id} fix ${on}`;
  return known ? `Patch ${r.environment}` : `Patch ${plural(n, "instance")}`;
}

export function reportResult(r: ReportLike): { text: string; tone: Tone } {
  const total = r.instance_count || (r.success_count ?? 0) + (r.failure_count ?? 0);
  // Reports written before outcomes were counted have neither count.
  const counted = (r.success_count ?? 0) + (r.failure_count ?? 0) > 0;
  const verb = r.operation_type === "rollback" ? "Rolled back" : "Patched";
  const did = r.operation_type === "rollback" ? "rolled back" : "patched";
  const s = runState(r.status);
  if (r.status === "Success" && !r.failure_count) {
    return { text: counted ? `${verb} ${r.success_count} of ${total}` : verb, tone: "ok" };
  }
  if (r.status === "Cancelled") return { text: counted ? `Stopped, ${r.success_count ?? 0} of ${total} ${did}` : "Stopped", tone: "watch" };
  if (s.tone === "risk" || r.failure_count) {
    return { text: counted ? `Failed, ${r.success_count ?? 0} of ${total} ${did}` : s.text, tone: "risk" };
  }
  return { text: s.text, tone: s.tone };
}

export function reportSla(r: Pick<RunRow, "operation_type" | "sla_met"> & Partial<Pick<RunRow, "cve_id">>): { text: string; tone: Tone } {
  if (r.operation_type === "rollback") return { text: "Not applicable", tone: null };
  if (r.sla_met === true) return { text: "Met", tone: "ok" };
  if (r.sla_met === false) return { text: "Breached", tone: "risk" };
  if ("cve_id" in r && !forOneFinding(r)) return { text: "No single result", tone: null };
  return { text: "No result", tone: null };
}

function forOneFinding(r: Partial<Pick<RunRow, "cve_id">>): boolean {
  return !!r.cve_id && r.cve_id !== "N/A";
}

/** Hours and minutes, "3 h 20 min" or "45 min". */
export function hoursText(ms: number): string {
  const min = Math.round(Math.abs(ms) / 60000);
  const h = Math.floor(min / 60), m = min % 60;
  return h ? (m ? `${h} h ${m} min` : `${h} h`) : `${m} min`;
}

type SlaRow = Pick<RunRow, "operation_type" | "cve_id" | "sla_met" | "sla_hours" | "sla_reason" | "timestamp" | "duration_seconds">
  & Partial<Pick<RunRow, "first_observed_at" | "severity_filter" | "sla_source">>;

/**
 * The SLA result explained. A run for one finding is measured from when the
 * finding was first seen: the deadline is first_observed_at + sla_hours, and the
 * run's end is its start plus its duration. A severity run patches many findings,
 * each seen at a different time, so it has no single result.
 */
export function slaDetail(r: SlaRow, fmt: (iso: string) => string = clock): string {
  if (r.operation_type === "rollback") return "Not applicable to a rollback.";
  const hours = r.sla_hours ? `${r.sla_hours} h SLA${r.sla_source ? ` (${r.sla_source})` : ""}` : "";
  if (r.sla_met === null && !forOneFinding(r)) {
    const what = r.severity_filter ? `patched by severity (${r.severity_filter.toLowerCase()} and above)` : "wasn't for one finding";
    return `This run ${what}. Each finding it fixed was first seen at a different time, so the run has no single SLA result.`;
  }
  const first = r.first_observed_at ? Date.parse(r.first_observed_at) : NaN;
  const start = Date.parse(r.timestamp);
  const end = r.duration_seconds != null && !Number.isNaN(start) ? start + r.duration_seconds * 1000 : NaN;
  if (r.sla_met !== null && r.sla_hours && !Number.isNaN(first)) {
    const due = first + r.sla_hours * 3_600_000;
    const iso = (t: number) => new Date(t).toISOString();
    const deadline = `Due ${fmt(iso(due))}: first seen ${fmt(iso(first))}, plus the ${hours}.`;
    if (Number.isNaN(end)) return `${deadline} The run started ${fmt(r.timestamp)}.`;
    const late = end - due;
    return late > 0 ? `${deadline} The run finished ${fmt(iso(end))}, ${hoursText(late)} late.`
      : `${deadline} The run finished ${fmt(iso(end))}, ${hoursText(late)} early.`;
  }
  if (r.sla_met === true) return hours ? `Met the ${hours}.` : "Met.";
  if (r.sla_met === false) return hours ? `Breached the ${hours}.` : "Breached.";
  if (!r.sla_hours) return "No SLA was set for this run.";
  if (r.sla_reason === "SLA or discovery time not recorded") {
    return `${hours}. The finding's first-seen time wasn't recorded, so the deadline can't be worked out.`;
  }
  return `${hours}. No result${r.sla_reason ? `: ${r.sla_reason}` : ""}.`;
}

export function decisionText(decision: string | null | undefined): string {
  if (!decision || decision === "N/A") return "";
  if (decision === "EMERGENCY") return "Patched now";
  if (decision === "SCHEDULED") return "Waited for a window";
  return decision.charAt(0) + decision.slice(1).toLowerCase();
}

const RUN_NOW_WORDS: Record<string, string> = {
  operator_asked: "Patched now: the operator asked to patch now.",
  severity_runs_now: "Patched now: severity plans run now, since no one maintenance window can be shown to meet every finding's SLA.",
  no_sla: "Patched now: no SLA applied, so no schedule was checked.",
  schedules_unchecked: "Patched now: the maintenance windows and patch policies couldn't be checked against the SLA.",
  no_schedule_meets_sla: "Patched now: no maintenance window or patch policy would patch the targets before the SLA deadline.",
  some_targets_uncovered: "Patched now: some targets weren't covered by a maintenance window or patch policy that meets the SLA.",
};

/** A report's "Why then": the recorded reason, or for reports from before it was recorded, the decision alone. */
export function whyThen(r: { decision: string | null; decision_reason?: string | null }): string {
  if (r.decision === "EMERGENCY") {
    return RUN_NOW_WORDS[r.decision_reason ?? ""] ?? "Patched now: no maintenance window met the SLA, or the operator asked to patch now.";
  }
  if (r.decision === "SCHEDULED") return "Waited for a maintenance window that met the SLA.";
  return decisionText(r.decision);
}

export interface ConversationChange {
  /** Unique per row: the plan, the run's first execution, or a run that never started. */
  key: string;
  kind: "plan" | "run";
  planId: string;
  headline: string;
  card: ResultCard;
  /** When it was first seen: the plan's creation or the run's approval when known, else its message's time. */
  at?: number;
}

function cardTime(card: ResultCard, fallback?: number): number | undefined {
  const t = Date.parse(String(card.created_at ?? card.started_at ?? ""));
  return Number.isNaN(t) ? fallback : t;
}

/** Plans and runs in this conversation, newest first; each appears once, in its latest state. */
export function conversationChanges(messages: Array<{ cards?: ResultCard[]; timestamp?: number }>): ConversationChange[] {
  const out: ConversationChange[] = [];
  const index = new Map<string, number>();
  for (let m = messages.length - 1; m >= 0; m--) {
    const cards = messages[m].cards ?? [];
    for (let c = cards.length - 1; c >= 0; c--) {
      const card = cards[c];
      if (card.type !== "plan" && card.type !== "run") continue;
      const planId = String(card.plan_id ?? "");
      const ids = Array.isArray(card.execution_ids) ? (card.execution_ids as string[]) : [];
      // A run is keyed by its first execution, or by its plan when it never started.
      const key = card.type === "plan" ? `plan:${planId}` : ids[0] ? `run:${ids[0]}` : `run-plan:${planId}:${m}`;
      if (!planId && !ids.length) continue;
      const record = card.record as { headline?: string } | null | undefined;
      const headline = String(record?.headline || card.headline || "");
      const at = cardTime(card, messages[m].timestamp);
      const seen = index.get(key);
      if (seen !== undefined) {
        // An older card for the same run (the one that started it) fills in what a Stop card lacks.
        const row = out[seen];
        if (at !== undefined && (row.at === undefined || at < row.at)) row.at = at;
        if (!row.planId && planId) row.planId = planId;
        if (!row.card.headline && card.headline) row.card = { ...row.card, headline: card.headline, plan_id: planId };
        if (headline && row.headline === String(row.card.title ?? "")) row.headline = headline;
        continue;
      }
      index.set(key, out.length);
      out.push({ key, kind: card.type, planId, headline: headline || String(card.title ?? ""), card, ...(at !== undefined ? { at } : {}) });
    }
  }
  return out;
}

/** One row per change: a plan with the run it started, or a run on its own. Newest first. */
export interface ChangeRow {
  key: string;
  planId: string;
  headline: string;
  plan?: ConversationChange;
  run?: ConversationChange;
  at?: number;
}

export function changeRows(changes: ConversationChange[]): ChangeRow[] {
  const rows: ChangeRow[] = [];
  const byPlan = new Map<string, ChangeRow>();
  for (const c of changes) {
    let row = c.planId ? byPlan.get(c.planId) : undefined;
    if (!row) {
      row = { key: c.planId ? `change:${c.planId}` : c.key, planId: c.planId, headline: c.headline };
      rows.push(row);
      if (c.planId) byPlan.set(c.planId, row);
    }
    // Changes come newest first, so the first plan and run seen are the latest.
    if (c.kind === "plan") row.plan ??= c;
    else row.run ??= c;
    if (c.at !== undefined && (row.at === undefined || c.at < row.at)) row.at = c.at;
  }
  for (const row of rows) row.headline = row.plan?.headline || row.run?.headline || row.headline;
  return rows;
}

/** Plan and run cards for the plans a conversation made, read again after a reload. */
export function cardsFromPlans(details: Array<PlanDetail | null>): ResultCard[] {
  const cards: ResultCard[] = [];
  for (const d of details) {
    if (!d) continue;
    const headline = d.view?.headline ?? "Plan";
    cards.push({ type: "plan", plan_id: d.plan_id, plan_hash: d.plan_hash, status: d.status, expires_at: d.expires_at,
                 created_at: d.created_at, title: headline,
                 record: d.view ? { kind: d.view.kind, headline, summary: d.view.summary } : null });
    if (d.execution_ids.length) {
      cards.push({ type: "run", plan_id: d.plan_id, execution_ids: d.execution_ids, headline,
                   approved_by: d.decision?.by, started_at: d.decision?.at ?? undefined, title: "Started" });
    }
  }
  return cards;
}

function reasonOf(card: ResultCard | undefined): string | undefined {
  const fields = (Array.isArray(card?.fields) ? card!.fields : []) as Array<{ label?: string; value?: string }>;
  return fields.find((f) => f.label === "Reason")?.value ?? fields.find((f) => f.label === "Status")?.value;
}

export const APPROVAL_UNKNOWN = "The approval was sent, but its reply was cut off. Check whether the run started before you approve again.";

/**
 * Whether an approval started a run: the stream returns a run card, with execution IDs when it did.
 * interrupted means the request may have reached the runtime but its reply didn't finish: unknown, not failed.
 */
export function approvalOutcome(accepted: boolean, cards: ResultCard[], interrupted = false):
    { ok: boolean; reason?: string; unknown?: boolean } {
  const run = cards.find((c) => c.type === "run");
  const ids = Array.isArray(run?.execution_ids) ? (run!.execution_ids as string[]) : [];
  if (ids.length) return { ok: true };
  if (run) return { ok: false, reason: reasonOf(run) ?? "The plan didn't start." };
  if (interrupted) return { ok: false, unknown: true, reason: APPROVAL_UNKNOWN };
  if (!accepted) return { ok: false, reason: "The approval didn't go through." };
  return { ok: false, reason: "The plan didn't start." };
}

/** The plan status to show after an approval: the outcome's, or for an unknown one, the stored plan's once it says more. */
export function statusAfterApproval(out: { ok: boolean; unknown?: boolean }): string {
  return out.ok ? "approved" : out.unknown ? "unknown" : "awaiting_approval";
}

/**
 * A plan's status from its card and the stored plan. An unknown approval gives
 * way to the stored plan once it shows a decision or a claimed run, or once the
 * operator has checked again and it is still waiting.
 */
export function resolvePlanStatus(cardStatus: string | undefined,
                                  detail: { status: string; claimed?: boolean } | null, checked = false): string | undefined {
  if (cardStatus === "unknown") {
    if (detail && (detail.status !== "awaiting_approval" || detail.claimed || checked)) return detail.status;
    return "unknown";
  }
  return cardStatus && cardStatus !== "awaiting_approval" ? cardStatus : detail?.status ?? cardStatus;
}

/** Whether a stop request was carried out, from the card the runtime returns. */
export function stopOutcome(accepted: boolean, cards: ResultCard[], interrupted = false):
    { ok: boolean; reason?: string; unknown?: boolean } {
  const card = cards.find((c) => c.type === "run");
  if (accepted && (card?.status === "stopping" || card?.status === "finished")) return { ok: true };
  if (!card && interrupted) return { ok: false, unknown: true, reason: "The stop request was sent, but its reply was cut off. Check the run." };
  return { ok: false, reason: reasonOf(card) ?? "The stop request didn't go through." };
}

/** WCAG contrast ratio of two #RRGGBB colours. */
export function contrastRatio(a: string, b: string): number {
  const lum = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return Math.round(((x + 0.05) / (y + 0.05)) * 100) / 100;
}

export function filterFindings(findings: VulnerabilityFinding[],
                               f: { severity: string; fixable: boolean; query: string }): VulnerabilityFinding[] {
  const q = f.query.trim().toLowerCase();
  return findings.filter((x) =>
    (f.severity === "ALL" || x.severity === f.severity)
    && (!f.fixable || x.fix_available === "YES" || x.fix_available === "PARTIAL")
    && (!q || x.cve_id.toLowerCase().includes(q) || (x.title ?? "").toLowerCase().includes(q)));
}

export type Theme = "light" | "dark";

export function initialTheme(stored: string | null, prefersDark: boolean): Theme {
  if (stored === "light" || stored === "dark") return stored;
  return prefersDark ? "dark" : "light";
}

/** A date and time, always with the date: a time alone is ambiguous in a 30-day list or an overnight run. */
export function clock(at: string | number | Date | null | undefined): string {
  if (at === null || at === undefined || at === "") return "";
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** The Rail's refresh line: refreshing, when the data shown was read, and whether the last refresh failed. */
export function refreshText(loading: boolean, refreshedAt: Date | null, failed: boolean): string {
  if (loading) return "Refreshing…";
  if (failed) return refreshedAt ? `Couldn't refresh; showing data from ${clock(refreshedAt)}` : "Couldn't read the dashboard";
  return refreshedAt ? `Read at ${clock(refreshedAt)}` : "";
}

/** A count as a percentage with its fraction, "0% (0 of 2)". Rounded down, so 100% means every one. */
export function percentOf(part: number, whole: number): string {
  if (!whole) return "";
  return `${Math.floor((part / whole) * 100)}% (${part} of ${whole})`;
}

export function since(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

/**
 * The dashboard narrowed to one account. A finding's instance count spans every
 * account it affects, so it counts toward an account only when it is that
 * account's alone; otherwise the instance total is unknown (-1).
 */
export function scopeDashboard(data: DashboardData, account: string): DashboardData {
  if (account === "ALL") return data;
  const findings = data.findings.filter((f) => f.accounts?.includes(account));
  const summary: DashboardData["findingsSummary"] = {};
  for (const f of findings) {
    const row = (summary[f.severity] ??= { ids: 0, instances: 0, fixable_ids: 0 });
    row.ids += 1;
    if (row.instances >= 0) {
      row.instances = f.accounts?.length === 1 ? row.instances + (f.instance_count ?? 0) : -1;
    }
    if (f.fix_available === "YES" || f.fix_available === "PARTIAL") row.fixable_ids += 1;
  }
  const environments = data.environments
    .filter((e) => e.accounts?.includes(account))
    .map((e) => {
      const pa = e.per_account?.[account];
      const vulns: Record<string, number> = {};
      for (const f of findings) {
        const envs = f.environments?.length ? f.environments : [f.environment];
        if (envs.includes(e.environment)) vulns[f.severity] = (vulns[f.severity] ?? 0) + 1;
      }
      const base = pa ? { ...e, total: pa.total, online: pa.online, offline: pa.offline, patch_compliance: pa.patch_compliance } : e;
      return { ...base, vulns, vuln_total: Object.values(vulns).reduce((s, n) => s + n, 0) };
    });
  return {
    ...data,
    environments,
    findings,
    findingsSummary: summary,
    unreadableLocations: data.unreadableLocations.filter((u) => u.account_id === account),
  };
}

/** Until when an approved plan whose run never started can be sent again: the runtime claims an
 * approval only for 10 minutes, and never past the plan's expiry. */
export function startAgainUntil(detail: { expires_at?: string; approval_expires_at?: string | null } | null): string {
  return detail?.approval_expires_at || detail?.expires_at || "";
}

/** The dashboard's warnings about reads that failed, for the view that shows that data. */
export type ReadArea = "findings" | "reports" | "running";
export function readProblems(warnings: DashboardData["warnings"], ...areas: ReadArea[]): DashboardData["warnings"] {
  return warnings.filter((w) => areas.some((a) => w.type === `${a}_unreadable`));
}

/** The Findings lede when the list is empty: no findings, or none that could be read. */
export function noFindingsText(unread: boolean): string {
  return unread ? "No findings to show, but not every Region could be read (see above)."
    : "Amazon Inspector reports no open vulnerabilities on scoped instances.";
}

/** A report tag's value in words: the value, the values of a mixed tag, or not tagged. */
export function tagText(value: string | undefined, values?: string[]): string {
  if (!value || value === "unknown") return "Not tagged";
  if (value === "mixed" && values?.length) return `mixed (${values.join(", ")})`;
  return value;
}

/** How many characters of an ID the UI shows: 8 for a run, 6 for a plan. The full ID is in the tooltip and the copy button. */
export const RUN_ID_CHARS = 8;
export const PLAN_ID_CHARS = 6;

export function shortId(id: string | null | undefined, kind: "run" | "plan"): string {
  return (id ?? "").slice(0, kind === "run" ? RUN_ID_CHARS : PLAN_ID_CHARS);
}

/** True when every word of the query appears in one of the fields, ignoring case. A short ID matches its full ID. */
export function matchesSearch(query: string, ...fields: Array<string | null | undefined>): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const text = fields.filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => text.includes(w));
}

/** Whether a scrolled log is at (or within a few lines of) its end, so new messages should keep it there. */
export function nearBottom(scrollTop: number, clientHeight: number, scrollHeight: number, slack = 48): boolean {
  return scrollHeight - (scrollTop + clientHeight) <= slack;
}

// Lower stages first. A name not listed sits just below prod, so it's never chosen over a known lower stage.
const STAGE_RANK: Record<string, number> = { dev: 0, development: 0, test: 1, qa: 1, staging: 2, stage: 2, uat: 2,
                                             preprod: 3, prod: 5, production: 5 };

/** The lowest environment a finding affects, so a patch plan starts there: dev before staging before prod. */
export function lowestEnvironment(envs: Array<string | null | undefined>): string | undefined {
  const named = envs.filter((e): e is string => !!e && e !== "unknown" && e !== "N/A");
  const rank = (e: string) => STAGE_RANK[e.toLowerCase()] ?? 4;
  return [...named].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))[0];
}

/** The prompts behind a finding's buttons: Plan patch (a change, so it waits for you) and Instances (a read). */
export function findingPrompts(f: Pick<VulnerabilityFinding, "cve_id" | "environment" | "environments">) {
  const env = lowestEnvironment(f.environments?.length ? f.environments : [f.environment]) ?? f.environment;
  return {
    plan: `Handle ${f.cve_id} in ${env} environment`,
    instances: `Which instances have ${f.cve_id}? List each one with its account and Region.`,
  };
}

/**
 * A change record check in the console's words. The dry-run scan check is
 * optional, so not having scanned reads as a note, not a warning.
 */
export function checkWords<C extends { key: string; label: string; detail: string; state: "pass" | "warn" | "info";
                                         last_scan_at?: string; first_scan_at?: string; scanned?: number; targets?: number;
                                         scan_at?: string }>(c: C): C {
  if (c.key !== "scan") return c;
  if (c.state === "pass") {
    const id = /Scan (\S+) ran first/.exec(c.detail)?.[1];
    // The plan checked the scan when it was made: it succeeded, recently, on these targets.
    return { ...c, label: "Dry-run scan first", detail: `A dry-run scan${id ? ` (${id})` : ""} listed the exact package versions before this plan was made. The plan checked that it succeeded on these targets${c.scan_at ? `, at ${clock(c.scan_at)}` : ""}.` };
  }
  // A scan the plan named but couldn't confirm for every target: the plan's own words.
  if (c.state === "warn" && c.label !== "No dry-run scan") return c;
  // No dry-run for this plan. Systems Manager's own scans still happen; say when the last was,
  // so this doesn't read as "never scanned".
  // Each instance's latest scan; a spread of more than an hour shows as a range (M22).
  const spread = c.first_scan_at ? Date.parse(c.last_scan_at ?? "") - Date.parse(c.first_scan_at) : 0;
  const when = spread > 3_600_000 ? `between ${clock(c.first_scan_at)} and ${clock(c.last_scan_at)}` : `at ${clock(c.last_scan_at)}`;
  const last = c.last_scan_at
    ? ` Systems Manager last scanned ${c.scanned === c.targets ? (c.targets === 1 ? "this instance" : `all ${c.targets} instances`)
        : `${c.scanned} of ${c.targets} instances`} ${when}.` : "";
  return { ...c, label: "No dry-run for this plan", state: "info",
           detail: `Optional.${last} A dry-run lists the exact package versions before you approve; ask Patchy to run one first if you want them.` };
}

const DRY_RUN_TOOLS = ["patch_dry_run", "multi_account_dry_run"];
const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;

/**
 * A reply that started a dry-run scan: its execution IDs, from the reply text
 * (the scan tool returns no card), and a read prompt that asks for the results.
 */
export function scanFollowUp(m: { role: string; tools: string[]; content: string }): { ids: string[]; prompt: string } | null {
  if (m.role !== "assistant" || !m.tools.some((t) => DRY_RUN_TOOLS.includes(t))) return null;
  const ids = [...new Set((m.content.match(UUID) ?? []).map((x) => x.toLowerCase()))];
  if (!ids.length) return null;
  return { ids, prompt: `Show the scan results for execution ${ids.join(", ")}` };
}

/** What an audit row's status reads as, and its tone. */
export function auditStatus(c: { status: string; execution_ids: string[]; decided_by: string | null }):
  { text: string; tone: "change" | "watch" | "risk" | "ok" | null } {
  if (c.status === "approved") {
    return c.execution_ids.length ? { text: `Run started, approved by ${c.decided_by ?? "unknown"}`, tone: "ok" }
      : { text: `Approved by ${c.decided_by ?? "unknown"}; no run recorded`, tone: "risk" };
  }
  if (c.status === "cancelled") return { text: `Cancelled by ${c.decided_by ?? "unknown"}`, tone: null };
  if (c.status === "expired") return { text: "Expired without a decision", tone: null };
  return { text: "Waiting for approval", tone: "watch" };
}

/** The line under the coverage table: how many targets a schedule patches in time, and what waiting means. */
export function coverageNote(covered: number, total: number, deadline: string): string {
  const by = deadline ? ` before the deadline (${deadline})` : " before the deadline";
  if (covered >= total) return `All ${total} are scheduled to be patched${by}.`;
  if (covered === 0) return `None of the ${total} is scheduled to be patched${by}. Cancelling this plan means waiting, and all ${total} breach their SLA.`;
  return `${covered} of ${total} are scheduled to be patched${by}. Cancelling this plan means waiting, and the other ${total - covered} breach their SLA.`;
}

/**
 * The agent's reply without em dashes: the prompt asks for none, but the model still uses
 * them, so each becomes a comma (one at the start of a line is dropped). En dashes stay.
 */
export function noEmDash(text: string): string {
  return text
    // An empty table cell keeps a dash, as an en dash.
    .replace(/\|([ \t]*)\u2014([ \t]*)(?=\|)/g, "|$1\u2013$2")
    .replace(/(^|\n)([ \t]*)\u2014[ \t]*/g, "$1$2")
    .replace(/[ \t]*,?[ \t]*\u2014[ \t]*/g, ", ");
}

