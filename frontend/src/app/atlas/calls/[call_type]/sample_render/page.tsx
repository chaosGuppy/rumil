import Link from "next/link";
import { notFound } from "next/navigation";
import type { RenderedPromptSample } from "@/api";
import { atlasFetch } from "../../../_lib/fetch";
import { fmtRelative } from "../../../_lib/format";
import { Crumbs } from "../../../_components/Crumbs";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ call_type: string }>;
}) {
  const { call_type } = await params;
  return { title: `sample render · ${call_type}` };
}

export default async function CallSampleRender({
  params,
}: {
  params: Promise<{ call_type: string }>;
}) {
  const { call_type } = await params;

  const sample = await atlasFetch<RenderedPromptSample | null>(
    `/api/atlas/registry/calls/${encodeURIComponent(call_type)}/sample_render`,
    null,
  );
  if (!sample) notFound();

  const anomalies = sample.anomalies ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "calls", href: "/atlas/calls" },
              {
                label: call_type,
                href: `/atlas/calls/${encodeURIComponent(call_type)}`,
              },
              { label: "sample render" },
            ]}
          />
          <h1>sample render</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.55rem" }}>
            <Link
              href={`/atlas/exchanges/${encodeURIComponent(sample.exchange_id)}/playground`}
              className="atlas-chip is-accent"
            >
              fork this exchange →
            </Link>
            {sample.model && <span className="atlas-chip">{sample.model}</span>}
            {sample.phase && (
              <span className="atlas-chip is-muted">phase {sample.phase}</span>
            )}
            {sample.round != null && (
              <span className="atlas-chip is-muted">round {sample.round}</span>
            )}
            {sample.run_id && (
              <Link
                href={`/atlas/runs/${encodeURIComponent(sample.run_id)}/flow`}
                className="atlas-chip"
              >
                run {sample.run_id.slice(0, 8)} →
              </Link>
            )}
            {sample.created_at && (
              <span className="atlas-chip is-muted">
                {fmtRelative(sample.created_at)}
              </span>
            )}
          </div>
          <p className="atlas-lede">
            One real captured exchange of <span className="atlas-mono">{call_type}</span>
            {" "}as the model actually saw it. Use the playground to edit and
            re-fire.
          </p>
        </div>
      </div>

      {anomalies.length > 0 && (
        <div className="atlas-pg-anomalies">
          <span className="atlas-pg-anomalies-label">anomalies</span>
          <ul>
            {anomalies.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>system prompt</h2>
          <span className="atlas-section-meta">
            {(sample.system_prompt ?? "").length} chars
          </span>
        </div>
        <pre className="atlas-pg-response-pre">
          {sample.system_prompt || "(empty)"}
        </pre>
      </section>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>user message</h2>
          <span className="atlas-section-meta">
            {(sample.user_message ?? "").length} chars
          </span>
        </div>
        <pre className="atlas-pg-response-pre">
          {sample.user_message || "(empty)"}
        </pre>
      </section>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>response</h2>
          <span className="atlas-section-meta">
            {(sample.response_text ?? "").length} chars
            {sample.has_error && (
              <span style={{ color: "var(--a-warm)", marginLeft: "0.5rem" }}>
                · errored
              </span>
            )}
          </span>
        </div>
        <pre className="atlas-pg-response-pre">
          {sample.response_text || "(empty)"}
        </pre>
        <div style={{ marginTop: "0.85rem" }}>
          <Link
            href={`/atlas/exchanges/${encodeURIComponent(sample.exchange_id)}/playground`}
            className="atlas-chip is-accent"
          >
            fork this exchange →
          </Link>
        </div>
      </section>
    </div>
  );
}
