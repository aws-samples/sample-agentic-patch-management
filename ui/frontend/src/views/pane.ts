/** What the detail pane shows. */
export type Pane =
  | { kind: "overview" }
  | { kind: "changes" }
  | { kind: "findings" }
  /** query: the search to start with, such as a run's execution ID. */
  | { kind: "reports"; query?: string }
  | { kind: "change"; planId: string }
  | { kind: "run"; executionIds: string[]; planId?: string };

export const PANE_TITLES: Record<Pane["kind"], string> = {
  overview: "Overview", changes: "Changes", findings: "Findings", reports: "Reports", change: "Change record", run: "Run",
};

export function samePane(a: Pane, b: Pane): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "change" && b.kind === "change") return a.planId === b.planId;
  if (a.kind === "run" && b.kind === "run") {
    return (a.executionIds[0] ?? `plan:${a.planId}`) === (b.executionIds[0] ?? `plan:${b.planId}`);
  }
  if (a.kind === "reports" && b.kind === "reports") return (a.query ?? "") === (b.query ?? "");
  return true;
}

/** A key that changes when the pane shows something else, so its view starts fresh. */
export function paneKey(p: Pane): string {
  if (p.kind === "change") return `change:${p.planId}`;
  if (p.kind === "run") return `run:${p.executionIds.join(",") || p.planId}`;
  if (p.kind === "reports") return `reports:${p.query ?? ""}`;
  return p.kind;
}

/** The pane kept in a browser history entry (history.state.patchyPane), or null when there is none or it isn't one. */
export function paneFromState(state: unknown): Pane | null {
  const p = (state as { patchyPane?: Record<string, unknown> } | null)?.patchyPane;
  if (!p || typeof p !== "object") return null;
  switch (p.kind) {
    case "overview": case "changes": case "findings":
      return { kind: p.kind };
    case "reports":
      return typeof p.query === "string" && p.query ? { kind: "reports", query: p.query } : { kind: "reports" };
    case "change":
      return typeof p.planId === "string" && p.planId ? { kind: "change", planId: p.planId } : null;
    case "run": {
      const ids = Array.isArray(p.executionIds) ? p.executionIds.filter((x): x is string => typeof x === "string") : [];
      const planId = typeof p.planId === "string" && p.planId ? p.planId : undefined;
      return ids.length || planId ? { kind: "run", executionIds: ids, ...(planId ? { planId } : {}) } : null;
    }
  }
  return null;
}
