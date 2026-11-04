/**
 * Keeps a pop-out window and the main window in step. The main window owns the
 * conversation, so a pop-out asks it to approve, cancel or stop. One main window
 * (the leader) answers: it acknowledges at once, then sends the result when the
 * work is done, so a slow approval never makes the pop-out act a second time.
 */
/** unknown: the request may have been carried out, but its reply was cut off. */
export type Outcome = { ok: boolean; reason?: string; unknown?: boolean };

// sessionId is the pop-out's own conversation, which the main window acts with.
export type ChannelRequest =
  | { type: "request"; id: string; action: "approve" | "cancel"; planId: string; planHash?: string; sessionId?: string }
  | { type: "request"; id: string; action: "stop"; executionId: string; sessionId?: string };

export type ChannelMessage =
  | ChannelRequest
  | { type: "ack"; id: string }
  | { type: "result"; id: string; ok: boolean; reason?: string; unknown?: boolean }
  | { type: "plan-status"; planId: string; status: string }
  | { type: "run-started"; planId: string; card: Record<string, unknown> };

const NAME = "patchy";

export function openChannel(onMessage: (m: ChannelMessage) => void): { post: (m: ChannelMessage) => void; close: () => void } {
  if (typeof BroadcastChannel === "undefined") return { post: () => undefined, close: () => undefined };
  const ch = new BroadcastChannel(NAME);
  ch.onmessage = (e) => onMessage(e.data as ChannelMessage);
  return { post: (m) => ch.postMessage(m), close: () => ch.close() };
}

/** Answer pop-out requests while isLeader() is true. Returns a function that stops serving. */
export function serve(handle: (r: ChannelRequest) => Promise<Outcome>, isLeader: () => boolean): () => void {
  const ch = openChannel(async (m) => {
    if (m.type !== "request" || !isLeader()) return;
    ch.post({ type: "ack", id: m.id });
    let out: Outcome;
    try {
      out = await handle(m);
    } catch (e) {
      out = { ok: false, reason: e instanceof Error ? e.message : "The request failed." };
    }
    ch.post({ type: "result", id: m.id, ...out });
  });
  return ch.close;
}

/**
 * Ask the main window to do something. Resolves to its outcome, or to null when
 * no main window acknowledged, in which case the pop-out acts on its own.
 */
export function request(m: ChannelRequest, opts: { ackMs?: number; resultMs?: number } = {}): Promise<Outcome | null> {
  const ackMs = opts.ackMs ?? 1500, resultMs = opts.resultMs ?? 10 * 60 * 1000;
  return new Promise((resolve) => {
    let done = false, acked = false;
    const finish = (v: Outcome | null) => {
      if (done) return;
      done = true;
      clearTimeout(ackTimer);
      clearTimeout(resultTimer);
      ch.close();
      resolve(v);
    };
    const ch = openChannel((reply) => {
      if (reply.type === "ack" && reply.id === m.id) acked = true;
      if (reply.type === "result" && reply.id === m.id) {
        finish({ ok: reply.ok, ...(reply.reason ? { reason: reply.reason } : {}), ...(reply.unknown ? { unknown: true } : {}) });
      }
    });
    const ackTimer = setTimeout(() => { if (!acked) finish(null); }, ackMs);
    const resultTimer = setTimeout(() => finish({ ok: false, reason: "The main window didn't answer. Check the run in the main window." }), resultMs);
    ch.post(m);
  });
}

type Locks = { request: (name: string, opts: { signal: AbortSignal }, cb: () => Promise<void>) => Promise<unknown> };

/**
 * Hold a Web Lock until the returned function is called. Released before it was
 * granted (React StrictMode mounts, unmounts and mounts again), the lock is let
 * go at once when it arrives, so the next holder isn't left waiting for good.
 */
export function holdLock(locks: Locks | undefined, name: string, onHeld: () => void): () => void {
  if (!locks) {
    onHeld();
    return () => undefined;
  }
  let release: (() => void) | undefined;
  let disposed = false;
  const ctl = new AbortController();
  locks.request(name, { signal: ctl.signal }, () => new Promise<void>((resolve) => {
    if (disposed) return resolve();
    release = resolve;
    onHeld();
  })).catch(() => undefined);   // aborted before it was granted
  return () => { disposed = true; ctl.abort(); release?.(); };
}

export function popoutUrl(view: "change" | "run", planId: string, executionIds: string[] = []): string {
  const q = new URLSearchParams({ pop: view, plan: planId });
  if (executionIds.length) q.set("exec", executionIds.join(","));
  return `/?${q.toString()}`;
}
