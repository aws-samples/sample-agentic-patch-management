import { useEffect, useRef } from "react";

/**
 * Auto sign-out after a period of inactivity.
 *
 * Watches for user activity (pointer, key, scroll, touch, focus). After the
 * configured idle window with no activity, clears the local session keys
 * and redirects to the Cognito logout URL. The user lands on the signed-out
 * page and must re-authenticate.
 *
 * The ALB session cookie defaults to 7 days, which means a user who walks
 * away from their desk stays signed in. This hook closes that gap with a
 * client-side idle timer. It does not change session lifetime — only idle.
 *
 * No-ops when:
 *   - logoutUrl is null (no Cognito configured / user not signed in yet)
 *   - the document is hidden (page in a background tab — defer the clock)
 *
 * Activity also resumes when the tab returns to the foreground, so a user
 * who tabs away for an hour and comes back gets a fresh idle window.
 */
export function useIdleLogout(logoutUrl: string | null | undefined,
                              idleMs: number = 60 * 60 * 1000) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const logoutUrlRef = useRef<string | null | undefined>(logoutUrl);
  logoutUrlRef.current = logoutUrl;

  useEffect(() => {
    if (!logoutUrl) return;

    const triggerLogout = () => {
      const url = logoutUrlRef.current;
      if (!url) return;
      try {
        // Mirror the manual sign-out path so the next sign-in starts fresh
        // (no stale session keys, no "session restored" banner).
        localStorage.removeItem("patchy-session-id");
        localStorage.removeItem("patchy-session-user");
      } catch {
        // localStorage may be unavailable; proceed anyway
      }
      window.location.replace(url);
    };

    const reset = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      // Skip arming the timer when the page is hidden — a backgrounded tab
      // would otherwise log out users mid-coffee. The visibilitychange
      // listener re-arms when they return.
      if (document.visibilityState === "hidden") return;
      timerRef.current = setTimeout(triggerLogout, idleMs);
    };

    const events: (keyof DocumentEventMap)[] = [
      "mousedown",
      "keydown",
      "scroll",
      "touchstart",
      "click",
    ];
    events.forEach((e) => document.addEventListener(e, reset, { passive: true }));
    document.addEventListener("visibilitychange", reset);
    window.addEventListener("focus", reset);

    reset();

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      events.forEach((e) => document.removeEventListener(e, reset));
      document.removeEventListener("visibilitychange", reset);
      window.removeEventListener("focus", reset);
    };
  }, [logoutUrl, idleMs]);
}
