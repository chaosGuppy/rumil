"use client";

import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createClient } from "@supabase/supabase-js";
import type { RunTraceOut } from "@/api/types.gen";
import { traceKeys, runTraceOptions } from "./queries";

export function useRunTrace(
  runId: string,
  initialData: RunTraceOut,
  realtimeConfig: { url: string; anon_key: string } | null,
): RunTraceOut {
  const queryClient = useQueryClient();
  const { data } = useQuery(runTraceOptions(runId, initialData));

  useEffect(() => {
    if (!realtimeConfig) return;

    const client = createClient(realtimeConfig.url, realtimeConfig.anon_key);
    const channel = client
      .channel(`trace:${runId}`)
      .on("broadcast", { event: "*" }, () => {
        queryClient.invalidateQueries({ queryKey: traceKeys.detail(runId) });
      })
      .subscribe((status) => {
        if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") {
          // Force an immediate refetch so polling picks up the slack.
          queryClient.invalidateQueries({ queryKey: traceKeys.detail(runId) });
        }
      });

    return () => {
      channel.unsubscribe();
      client.removeAllChannels();
    };
  }, [runId, realtimeConfig, queryClient]);

  return data as RunTraceOut;
}
