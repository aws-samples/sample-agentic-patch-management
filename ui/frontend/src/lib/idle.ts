/**
 * The idle clock behind useIdleLogout, without the DOM: activity in any
 * Patchy window is a shared timestamp, a hidden tab doesn't count down, and a
 * tab that comes back checks the clock before it counts as activity, so an
 * idle period that ran out while it was hidden signs out.
 */
export interface IdleDeps {
  idleMs: number;
  now: () => number;
  /** Last activity in any Patchy window (0 when none). */
  read: () => number;
  write: (at: number) => void;
  hidden: () => boolean;
  schedule: (fn: () => void, ms: number) => () => void;
  logout: () => void;
}

export function createIdleWatch(d: IdleDeps) {
  const started = d.now();
  let cancel: (() => void) | null = null;
  let written = 0;
  let done = false;

  const arm = (ms: number) => {
    cancel?.();
    cancel = null;
    // A hidden tab doesn't count down; it checks again when it's shown.
    if (!done && !d.hidden()) cancel = d.schedule(check, ms);
  };
  /** Sign out when no Patchy window has seen activity for the whole idle window. Returns true when it did. */
  function check(): boolean {
    if (done) return true;
    const idleFor = d.now() - Math.max(d.read(), started);
    if (idleFor >= d.idleMs) {
      done = true;
      cancel?.();
      d.logout();
      return true;
    }
    arm(d.idleMs - idleFor);
    return false;
  }
  const activity = () => {
    if (done) return;
    const now = d.now();
    if (now - written > 10_000) {
      written = now;
      d.write(now);
    }
    arm(d.idleMs);
  };
  // Shown again or focused: an idle window that ran out while hidden signs out first.
  const returned = () => { if (!check()) activity(); };

  return { activity, returned, check, dispose: () => { cancel?.(); cancel = null; } };
}
