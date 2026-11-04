import { apiFetch } from "./http";
import { classifyRunResponse, type RunFetch } from "./runPoll";

/** Live status of a run this conversation started (GET /api/runs/{id}). */
export interface RunLocation {
  account: string;
  region: string;
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface RunStatus {
  execution_id: string;
  status: string;
  active: boolean;
  started_at?: string | null;
  ended_at?: string | null;
  children: RunLocation[];
  counts: { total: number; succeeded: number; failed: number; running: number };
  /** With status CommandsFinishing: how the run ended in SSM, and the commands still going. */
  ended_status?: string;
  still_running?: number;
}

/** One read of a run's status; a thrown fetch is a transient error. */
export async function fetchRun(executionId: string, sessionId: string): Promise<RunFetch> {
  try {
    const res = await apiFetch(`/api/runs/${executionId}?session_id=${encodeURIComponent(sessionId)}`);
    return classifyRunResponse(res.status, res.ok ? await res.json() : null);
  } catch {
    return { kind: "error", message: "The run status couldn't be read." };
  }
}
