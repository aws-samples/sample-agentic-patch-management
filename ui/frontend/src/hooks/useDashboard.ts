import { useState, useEffect, useCallback, useRef } from "react";
import { fetchDashboard, type DashboardData } from "../lib/api";

const AUTO_REFRESH_MS = 60_000; // 60 seconds

export function useDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchDashboard();
      setData(result);
      setLastRefreshed(new Date());
    } catch {
      // fetchDashboard handles partial failures internally
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load + auto-refresh
  useEffect(() => {
    refresh();
    timerRef.current = setInterval(refresh, AUTO_REFRESH_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [refresh]);

  return { data, loading, lastRefreshed, refresh };
}
