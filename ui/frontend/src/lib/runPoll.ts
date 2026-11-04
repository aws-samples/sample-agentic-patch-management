/**
 * Following a run's status across polls. A failed read keeps the last good
 * status, so the run view (and its Stop button) doesn't empty on a network
 * blip. Transient errors (a thrown fetch, a 5xx) back off and keep trying;
 * 404 and 403 mean the run isn't ours or isn't recorded yet, and count as gone
 * after a few in a row. Polling stops once every run has ended or is gone.
 */
import type { RunStatus } from "./runs";

export type RunFetch =
  | { kind: "ok"; run: RunStatus }
  | { kind: "gone"; status: number }
  | { kind: "error"; message: string };

export interface RunTrack {
  /** The last status read, kept through errors. */
  last: RunStatus | null;
  gone: boolean;
  /** 404s or 403s in a row. */
  misses: number;
  /** The last read's error, cleared by a good read. */
  error: string;
}

// A new run's record can take a poll or two to appear, so one 404 isn't final.
export const MAX_MISSES = 3;
export const POLL_MS = 10_000;
export const MAX_POLL_MS = 60_000;

export function classifyRunResponse(status: number, body: unknown): RunFetch {
  if (status >= 200 && status < 300 && body) return { kind: "ok", run: body as RunStatus };
  if (status === 404 || status === 403) return { kind: "gone", status };
  return { kind: "error", message: status >= 500 ? "The server didn't answer." : `The status read failed (${status}).` };
}

export function trackRun(prev: RunTrack | undefined, r: RunFetch, maxMisses = MAX_MISSES): RunTrack {
  const t: RunTrack = prev ?? { last: null, gone: false, misses: 0, error: "" };
  if (r.kind === "ok") return { last: r.run, gone: false, misses: 0, error: "" };
  if (r.kind === "gone") {
    const misses = t.misses + 1;
    return { ...t, misses, gone: misses >= maxMisses, error: "" };
  }
  return { ...t, error: r.message };
}

/** Nothing more to learn about this run. */
export function settled(t: RunTrack | undefined): boolean {
  return !!t && (t.gone || (!!t.last && !t.last.active));
}

/**
 * Milliseconds to the next poll, or null to stop. errorRounds is how many
 * polls in a row had a transient error; each doubles the wait, up to a minute.
 */
export function nextPollDelay(tracks: Array<RunTrack | undefined>, errorRounds: number,
                              base = POLL_MS, max = MAX_POLL_MS): number | null {
  if (tracks.length && tracks.every(settled)) return null;
  return Math.min(max, base * 2 ** Math.max(0, errorRounds));
}
