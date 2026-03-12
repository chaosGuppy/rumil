"use client";

import { useEffect, useCallback, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createClient } from "@supabase/supabase-js";
import type { CallTraceOut, RunTraceOut } from "@/api/types.gen";
import { traceKeys, runTraceOptions } from "./queries";

type TraceEvent = CallTraceOut["events"][number];

function findCallNode(nodes: CallTraceOut[], callId: string): boolean {
  for (const node of nodes) {
    if (node.call.id === callId) return true;
    if (findCallNode(node.children, callId)) return true;
  }
  return false;
}

function addEventToNodes(
  nodes: CallTraceOut[],
  event: TraceEvent,
): CallTraceOut[] {
  return nodes.map((node) => {
    if (node.call.id === event.call_id) {
      const isDuplicate = node.events.some(
        (e) => e.ts === event.ts && e.event === event.event,
      );
      if (isDuplicate) return node;
      return { ...node, events: [...node.events, event] };
    }
    const updatedChildren = addEventToNodes(node.children, event);
    if (updatedChildren !== node.children) {
      return { ...node, children: updatedChildren };
    }
    return node;
  });
}

function applyOverlay(
  serverTrace: RunTraceOut,
  overlay: TraceEvent[],
): RunTraceOut {
  if (overlay.length === 0) return serverTrace;
  let nodes = serverTrace.root_calls;
  for (const event of overlay) {
    nodes = addEventToNodes(nodes, event);
  }
  if (nodes === serverTrace.root_calls) return serverTrace;
  return { ...serverTrace, root_calls: nodes };
}

export function useRunTrace(
  runId: string,
  initialData: RunTraceOut,
  realtimeConfig: { url: string; anon_key: string } | null,
): RunTraceOut {
  const queryClient = useQueryClient();
  const { data: serverTrace } = useQuery(runTraceOptions(runId, initialData));
  const [overlay, setOverlay] = useState<TraceEvent[]>([]);

  const handleEvent = useCallback(
    (event: TraceEvent) => {
      setOverlay((prev) => [...prev, event]);

      const key = traceKeys.detail(runId);
      const current = queryClient.getQueryData<RunTraceOut>(key);
      if (current && !findCallNode(current.root_calls, event.call_id)) {
        queryClient.invalidateQueries({ queryKey: key });
      }
    },
    [queryClient, runId],
  );

  useEffect(() => {
    if (!realtimeConfig) return;

    const client = createClient(realtimeConfig.url, realtimeConfig.anon_key);
    const channel = client
      .channel(`trace:${runId}`)
      .on("broadcast", { event: "*" }, (payload) => {
        const event = payload.payload as TraceEvent;
        if (event && event.call_id && event.event) {
          handleEvent(event);
        }
      })
      .subscribe();

    return () => {
      channel.unsubscribe();
    };
  }, [runId, realtimeConfig, handleEvent]);

  return useMemo(
    () => applyOverlay(serverTrace as RunTraceOut, overlay),
    [serverTrace, overlay],
  );
}
