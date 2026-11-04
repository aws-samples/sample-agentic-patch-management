import { useState, useEffect, useCallback, useRef } from "react";
import { fetchDashboard, type DashboardData } from "../lib/api";
import { latest } from "../lib/latest";

export function useDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  // The last refresh failed: the data shown is from lastRefreshed, and the Rail says so.
  const [refreshFailed, setRefreshFailed] = useState(false);
  const requests = useRef(latest());

  // A background refresh (the poll) doesn't show "Refreshing", so the Rail doesn't flicker.
  // Only the newest request's answer is used; an older one arriving late is dropped.
  const refresh = useCallback(async (force = false, background = false) => {
    const current = requests.current.begin();
    if (!background) setLoading(true);
    try {
      const result = await fetchDashboard(force);
      if (!current()) return;
      setData(result);
      setLastRefreshed(new Date());
      setRefreshFailed(false);
    } catch {
      // The server reports partial failures in the data; this is the request itself failing.
      if (current()) setRefreshFailed(true);
    } finally {
      if (current()) setLoading(false);
    }
  }, []);

  // Load once on mount. Only the Refresh button forces a rebuild; the server
  // cache is shared and shorter-lived than the poll below.
  useEffect(() => {
    refresh();
  }, [refresh]);

  // Refresh every 30 s while runs are in progress and every 2 minutes otherwise, so a run
  // another operator or window starts shows up (30 Oct live: it never did); not in a hidden
  // tab, and a tab shown again catches up at once.
  const hasRunning = (data?.runningOperations?.length ?? 0) > 0;
  useEffect(() => {
    const hidden = () => document.visibilityState === "hidden";
    const timer = setInterval(() => { if (!hidden()) refresh(false, true); }, dashboardPollMs(hasRunning));
    const onShow = () => { if (!hidden()) refresh(false, true); };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [hasRunning, refresh]);

  return { data, loading, lastRefreshed, refreshFailed, refresh };
}

/** How often the dashboard refreshes: often while a run is going, and still now and then when none is known. */
export function dashboardPollMs(hasRunning: boolean): number {
  return hasRunning ? 30_000 : 120_000;
}
