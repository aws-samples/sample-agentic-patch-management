import { useEffect, useRef } from "react";
import { createIdleWatch } from "../lib/idle";
import { forgetSession } from "../lib/session";

/**
 * Auto sign-out after a period of inactivity.
 *
 * Watches for user activity (pointer, key, scroll, touch, focus). After the
 * configured idle window with no activity, clears the local session keys
 * and redirects to the Cognito logout URL. The user lands on the signed-out
 * page and must re-authenticate.
 *
 * The ALB session lasts 12 hours, so a user who walks away from their desk
 * stays signed in. This hook adds a client-side idle timer. Activity in any
 * Patchy window (main or pop-out) counts, through a shared timestamp.
 *
 * No-ops when:
 *   - logoutUrl is null (no Cognito configured / user not signed in yet)
 *   - the document is hidden (a background tab defers the clock)
 *
 * Returning to a hidden tab counts as activity, once the clock shows the idle
 * window hasn't run out while it was hidden.
 */
// Last activity in any Patchy window, so a pop-out left alone doesn't sign out the main window.
const ACTIVITY_KEY = "patchy-last-activity";

function lastActivity(): number {
  try {
    return Number(localStorage.getItem(ACTIVITY_KEY)) || 0;
  } catch {
    return 0;
  }
}

export function useIdleLogout(logoutUrl: string | null | undefined,
                              idleMs: number = 60 * 60 * 1000) {
  const logoutUrlRef = useRef<string | null | undefined>(logoutUrl);
  logoutUrlRef.current = logoutUrl;

  useEffect(() => {
    if (!logoutUrl) return;

    const watch = createIdleWatch({
      idleMs,
      now: Date.now,
      read: lastActivity,
      write: (at) => { try { localStorage.setItem(ACTIVITY_KEY, String(at)); } catch { /* best-effort */ } },
      hidden: () => document.visibilityState === "hidden",
      schedule: (fn, ms) => { const t = setTimeout(fn, ms); return () => clearTimeout(t); },
      logout: () => {
        const url = logoutUrlRef.current;
        if (!url) return;
        // Mirror the manual sign-out path so the next sign-in starts fresh
        // (no stale session keys, no "session restored" banner).
        forgetSession();
        window.location.replace(url);
      },
    });
    const reset = watch.activity;
    const returned = watch.returned;

    const events: (keyof DocumentEventMap)[] = ["mousedown", "keydown", "touchstart", "click", "wheel"];
    events.forEach((e) => document.addEventListener(e, reset, { passive: true }));
    // Scrolling happens inside the chat and detail panes, so listen in the capture phase.
    document.addEventListener("scroll", reset, { passive: true, capture: true });
    document.addEventListener("visibilitychange", returned);
    window.addEventListener("focus", returned);

    reset();

    return () => {
      watch.dispose();
      events.forEach((e) => document.removeEventListener(e, reset));
      document.removeEventListener("scroll", reset, { capture: true });
      document.removeEventListener("visibilitychange", returned);
      window.removeEventListener("focus", returned);
    };
  }, [logoutUrl, idleMs]);
}
