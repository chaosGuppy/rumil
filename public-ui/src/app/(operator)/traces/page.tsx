"use client";

import { useEffect, useState } from "react";
import { TraceList } from "@/components/operator/TraceList";
import { fetchRuns } from "@/lib/operator-api";
import type { RunSummary } from "@/lib/operator-types";

export default function TracesPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns({ limit: 100 })
      .then((data) => {
        setRuns(data.runs);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  if (loading) return <div className="op-trace-list-empty">Loading runs...</div>;
  if (error) return <div className="op-trace-list-empty">Error: {error}</div>;
  return <TraceList runs={runs} />;
}
