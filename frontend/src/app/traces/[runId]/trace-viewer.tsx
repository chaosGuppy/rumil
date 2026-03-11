"use client";

import { useEffect, useReducer } from "react";
import { createClient } from "@supabase/supabase-js";
import { CallNode } from "./call-node";

interface TraceEvent {
  event: string;
  ts: string;
  call_id: string;
  data: Record<string, unknown>;
}

interface CallOut {
  id: string;
  call_type: string;
  status: string;
  parent_call_id: string | null;
  scope_page_id: string | null;
  budget_allocated: number | null;
  budget_used: number;
  result_summary: string;
  review_json: Record<string, unknown>;
  created_at: string;
  completed_at: string | null;
}

interface CallTrace {
  call: CallOut;
  events: TraceEvent[];
  children: CallTrace[];
}

interface RunTrace {
  run_id: string;
  question: {
    id: string;
    summary: string;
    content: string;
    project_id: string;
  } | null;
  root_calls: CallTrace[];
}

type Action =
  | { type: "SET_TRACE"; trace: RunTrace }
  | { type: "ADD_EVENT"; event: TraceEvent };

function findAndAddEvent(
  nodes: CallTrace[],
  event: TraceEvent,
): CallTrace[] {
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

function reducer(state: RunTrace, action: Action): RunTrace {
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
  initialTrace: RunTrace;
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
    <div className="space-y-4">
      {trace.root_calls.map((ct) => (
        <CallNode key={ct.call.id} trace={ct} depth={0} />
      ))}
      {trace.root_calls.length === 0 && (
        <p className="text-gray-500 text-sm">
          No calls recorded for this run yet.
        </p>
      )}
    </div>
  );
}
