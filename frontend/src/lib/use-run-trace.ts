"use client";

import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createClient } from "@supabase/supabase-js";
import type { RunTraceTreeOut } from "@/api/types.gen";
import { traceKeys, runTraceTreeOptions } from "./queries";

export function useRunTraceTree(
  runId: string,
  initialData: RunTraceTreeOut,
  realtimeConfig: { url: string; anon_key: string } | null,
): RunTraceTreeOut {
  const queryClient = useQueryClient();
  const { data } = useQuery(runTraceTreeOptions(runId, initialData));

  useEffect(() => {
    if (!realtimeConfig) return;

    const client = createClient(realtimeConfig.url, realtimeConfig.anon_key);
    const channel = client
      .channel(`trace:${runId}`)
      .on("broadcast", { event: "*" }, () => {
        queryClient.invalidateQueries({ queryKey: traceKeys.tree(runId) });
      })
      .subscribe((status) => {
        if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") {
          queryClient.invalidateQueries({ queryKey: traceKeys.tree(runId) });
        }
      });

    return () => {
      channel.unsubscribe();
      client.removeAllChannels();
    };
  }, [runId, realtimeConfig, queryClient]);

  return data as RunTraceTreeOut;
}
