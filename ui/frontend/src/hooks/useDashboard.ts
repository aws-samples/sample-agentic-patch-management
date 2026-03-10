import { useState, useEffect, useCallback } from "react";
import { fetchDashboard, type DashboardData } from "../lib/api";

export function useDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchDashboard(force);
      setData(result);
      setLastRefreshed(new Date());
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load dashboard";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  // Load once on mount
  useEffect(() => {
    refresh(true);
  }, [refresh]);

  return { data, loading, error, lastRefreshed, refresh };
}
