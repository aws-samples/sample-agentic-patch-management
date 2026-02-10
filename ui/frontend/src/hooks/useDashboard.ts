import { useState, useEffect, useCallback } from "react";
import { fetchDashboard, type DashboardData } from "../lib/api";

export function useDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

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

  return { data, loading, lastRefreshed, refresh };
}
