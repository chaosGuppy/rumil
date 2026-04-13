"use client";

import { TraceList } from "@/components/operator/TraceList";
import { MOCK_RUNS } from "@/lib/operator-mock";

export default function TracesPage() {
  return <TraceList runs={MOCK_RUNS} />;
}
