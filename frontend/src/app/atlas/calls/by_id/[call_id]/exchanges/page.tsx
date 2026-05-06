import Link from "next/link";
import { notFound } from "next/navigation";
import type { InvocationIndex } from "@/api";
import { atlasFetch } from "../../../../_lib/fetch";
import { Crumbs } from "../../../../_components/Crumbs";
import { InvocationsList } from "../../../../_components/InvocationsList";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ call_id: string }>;
}) {
  const { call_id } = await params;
  return { title: `call ${call_id.slice(0, 8)} · exchanges` };
}

export default async function CallExchangesPage({
  params,
}: {
  params: Promise<{ call_id: string }>;
}) {
  const { call_id } = await params;
  const idx = await atlasFetch<InvocationIndex | null>(
    `/api/atlas/calls/by_id/${encodeURIComponent(call_id)}/exchanges?limit=100`,
    null,
  );
  if (!idx) notFound();

  const callType = idx.items[0]?.call_type ?? "";
  const runId = idx.items[0]?.run_id ?? "";

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "calls" },
              { label: callType || "—", href: callType ? `/atlas/calls/${callType}` : undefined },
              { label: call_id.slice(0, 8) },
              { label: "exchanges" },
            ]}
          />
          <h1>call exchanges</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip" title={call_id}>
              {call_id.slice(0, 8)}
            </span>
            {callType && (
              <Link href={`/atlas/calls/${encodeURIComponent(callType)}`} className="atlas-chip">
                {callType}
              </Link>
            )}
            {runId && (
              <Link href={`/atlas/runs/${encodeURIComponent(runId)}/flow`} className="atlas-chip">
                run {runId.slice(0, 8)}
              </Link>
            )}
          </div>
          <p className="atlas-lede">
            Every LLM exchange recorded against this call. Same shape as the
            recent-invocations index on the call type page, scoped to this one
            call_id.
          </p>
        </div>
      </div>

      <InvocationsList index={idx} kind="call_type" />
    </div>
  );
}
