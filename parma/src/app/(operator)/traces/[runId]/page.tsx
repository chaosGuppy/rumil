"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { TraceDetail } from "@/components/operator/TraceDetail";
import { fetchRunDetail } from "@/lib/operator-api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import type { RunDetail } from "@/lib/operator-types";

export default function TraceDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fall back through the most descriptive field we have. `description` is
  // the operator-surfaced run summary; `scope_node_headline` is the
  // targeted question; short id is always available.
  const titleHeadline =
    run?.description ??
    run?.scope_node_headline ??
    (runId ? `run ${runId.slice(0, 8)}` : null);
  useDocumentTitle(
    ["trace", titleHeadline, run?.workspace_name],
    "Rumil operator",
  );

  useEffect(() => {
    fetchRunDetail(runId)
      .then((data) => {
        setRun(data);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, [runId]);

  if (loading) return <div className="op-trace-list-empty">Loading trace...</div>;
  if (error) return (
    <div className="op-trace-list-empty">
      Error: {error}
      <br />
      <Link href="/traces" style={{ color: "var(--accent)" }}>Back to traces</Link>
    </div>
  );
  if (!run) return (
    <div className="op-trace-list-empty">
      Run not found: {runId}
      <br />
      <Link href="/traces" style={{ color: "var(--accent)" }}>Back to traces</Link>
    </div>
  );

  return (
    <div>
      <Link href="/traces" className="op-back-link">&larr; traces</Link>
      <TraceDetail run={run} />
    </div>
  );
}
