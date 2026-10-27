import { useCallback, useEffect, useRef, useState } from "react";

import { checkHealth } from "../api/client";

export type ApiStatus = "checking" | "online" | "offline";

const POLL_INTERVAL_MS = 30_000;

/**
 * Polls GET /health and exposes a manual "mark offline" hook so a failed
 * ask request can flip the header indicator immediately.
 */
export function useApiHealth() {
  const [status, setStatus] = useState<ApiStatus>("checking");
  const [lastCheckedAt, setLastCheckedAt] = useState<Date | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();

    const probe = async () => {
      const healthy = await checkHealth(controller.signal);

      if (!mounted.current || controller.signal.aborted) {
        return;
      }

      setStatus(healthy ? "online" : "offline");
      setLastCheckedAt(new Date());
    };

    void probe();

    const interval = window.setInterval(() => void probe(), POLL_INTERVAL_MS);
    const onFocus = () => void probe();
    window.addEventListener("focus", onFocus);

    return () => {
      mounted.current = false;
      controller.abort();
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  const markOffline = useCallback(() => {
    if (mounted.current) {
      setStatus("offline");
      setLastCheckedAt(new Date());
    }
  }, []);

  return { status, lastCheckedAt, markOffline };
}
