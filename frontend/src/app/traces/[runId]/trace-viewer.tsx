"use client";

import { useEffect, useReducer } from "react";
import { createClient } from "@supabase/supabase-js";
import type { CallTraceOut, RunTraceOut } from "@/api/types.gen";
import { CallNode } from "./call-node";

type TraceEvent = CallTraceOut["events"][number];

type Action =
  | { type: "SET_TRACE"; trace: RunTraceOut }
  | { type: "ADD_EVENT"; event: TraceEvent };

function findAndAddEvent(
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
    const updatedChildren = findAndAddEvent(node.children, event);
    if (updatedChildren !== node.children) {
      return { ...node, children: updatedChildren };
    }
    return node;
  });
}

function reducer(state: RunTraceOut, action: Action): RunTraceOut {
  switch (action.type) {
    case "SET_TRACE":
      return action.trace;
    case "ADD_EVENT": {
      const updated = findAndAddEvent(state.root_calls, action.event);
      if (updated === state.root_calls) return state;
      return { ...state, root_calls: updated };
    }
  }
}

export function TraceViewer({
  initialTrace,
  runId,
  realtimeConfig,
}: {
  initialTrace: RunTraceOut;
  runId: string;
  realtimeConfig: { url: string; anon_key: string } | null;
}) {
  const [trace, dispatch] = useReducer(reducer, initialTrace);

  useEffect(() => {
    if (!realtimeConfig) return;

    const client = createClient(realtimeConfig.url, realtimeConfig.anon_key);
    const channel = client
      .channel(`trace:${runId}`)
      .on("broadcast", { event: "*" }, (payload) => {
        const event = payload.payload as TraceEvent;
        if (event && event.call_id && event.event) {
          dispatch({ type: "ADD_EVENT", event });
        }
      })
      .subscribe();

    return () => {
      channel.unsubscribe();
    };
  }, [runId, realtimeConfig]);

  return (
    <div className="trace-root">
      {trace.root_calls.map((ct) => (
        <CallNode key={ct.call.id} trace={ct} depth={0} />
      ))}
      {trace.root_calls.length === 0 && (
        <p className="trace-empty">
          No calls recorded for this run yet.
        </p>
      )}
    </div>
  );
}
