"use client";

import type { RunDetail } from "@/lib/operator-types";
import { RunHeader } from "./RunHeader";
import { EventTimeline } from "./EventTimeline";

export function TraceDetail({ run }: { run: RunDetail }) {
  return (
    <div className="op-trace-detail">
      <RunHeader run={run} />
      <EventTimeline events={run.events} />
    </div>
  );
}
