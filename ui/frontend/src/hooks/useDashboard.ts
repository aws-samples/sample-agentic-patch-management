import { useState, useEffect, useCallback, useRef } from "react";
import { fetchDashboard, type DashboardData } from "../lib/api";

export function useDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    try {
      const result = await fetchDashboard(force);
      setData(result);
      setLastRefreshed(new Date());
    } catch {
      // fetchDashboard handles partial failures internally
    } finally {
      setLoading(false);
    }
  }, []);

  // Load once on mount
  useEffect(() => {
    refresh(true);
  }, [refresh]);

  // Auto-refresh every 30s when running operations exist
  useEffect(() => {
    const hasRunning = (data?.runningOperations?.length ?? 0) > 0;

    if (hasRunning && !intervalRef.current) {
      intervalRef.current = setInterval(() => {
        refresh(true);
      }, 30_000);
    } else if (!hasRunning && intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [data?.runningOperations?.length, refresh]);

  return { data, loading, lastRefreshed, refresh };
}
