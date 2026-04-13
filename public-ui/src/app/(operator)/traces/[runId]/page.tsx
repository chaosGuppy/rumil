"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { TraceDetail } from "@/components/operator/TraceDetail";
import { MOCK_RUN_DETAILS } from "@/lib/operator-mock";

export default function TraceDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const run = MOCK_RUN_DETAILS[runId];

  if (!run) {
    return (
      <div className="op-trace-list-empty">
        Run not found: {runId}
        <br />
        <Link href="/traces" style={{ color: "var(--accent)" }}>
          Back to traces
        </Link>
      </div>
    );
  }

  return (
    <div>
      <Link href="/traces" className="op-back-link">
        &larr; traces
      </Link>
      <TraceDetail run={run} />
    </div>
  );
}
