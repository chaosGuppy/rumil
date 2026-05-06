import Link from "next/link";
import { notFound } from "next/navigation";
import type { ExchangePlaygroundContext } from "@/api";
import { atlasFetch } from "../../../_lib/fetch";
import { fmtRelative } from "../../../_lib/format";
import { Crumbs } from "../../../_components/Crumbs";
import { PlaygroundEditor } from "./PlaygroundEditor";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ exchange_id: string }>;
}) {
  const { exchange_id } = await params;
  return { title: `playground · ${exchange_id.slice(0, 8)}` };
}

export default async function ExchangePlayground({
  params,
}: {
  params: Promise<{ exchange_id: string }>;
}) {
  const { exchange_id } = await params;
  const ctx = await atlasFetch<ExchangePlaygroundContext | null>(
    `/api/atlas/exchanges/${encodeURIComponent(exchange_id)}/playground`,
    null,
  );
  if (!ctx) notFound();

  const composition = ctx.composition;
  const partsCount = composition?.parts?.length ?? 0;
  const partsKinds = composition?.parts
    ? Array.from(new Set(composition.parts.map((p) => p.role))).join("+")
    : "";
  const anomalies = ctx.anomalies ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "exchanges" },
              { label: ctx.exchange_id.slice(0, 8) },
              { label: "playground" },
            ]}
          />
          <h1>playground</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.55rem" }}>
            <Link
              href={`/atlas/calls/${encodeURIComponent(ctx.call_type)}`}
              className="atlas-chip is-accent"
            >
              {ctx.call_type} →
            </Link>
            {ctx.model && <span className="atlas-chip">{ctx.model}</span>}
            {ctx.temperature != null && (
              <span className="atlas-chip is-muted">temp {ctx.temperature}</span>
            )}
            {ctx.max_tokens != null && (
              <span className="atlas-chip is-muted">max {ctx.max_tokens}</span>
            )}
            {ctx.has_thinking && (
              <span className="atlas-chip is-muted">
                thinking {ctx.thinking_off ? "off" : "on"}
              </span>
            )}
            {ctx.run_id && (
              <Link
                href={`/atlas/runs/${encodeURIComponent(ctx.run_id)}/flow`}
                className="atlas-chip"
              >
                run {ctx.run_id.slice(0, 8)} →
              </Link>
            )}
            {ctx.created_at && (
              <span className="atlas-chip is-muted">
                {fmtRelative(ctx.created_at)}
              </span>
            )}
          </div>
          <p className="atlas-lede">
            Edit prompt, model, or sampling and re-fire this exchange to see
            how the model would respond under different conditions. Side-effect
            free — tool calls in fork responses are stored, never executed.
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

      {composition && partsCount > 0 && (
        <div className="atlas-callout" style={{ marginBottom: "1.4rem" }}>
          this exchange&apos;s call type composes its prompt from{" "}
          <strong>{partsCount}</strong> part{partsCount === 1 ? "" : "s"}{" "}
          {partsKinds && (
            <span className="atlas-mono" style={{ color: "var(--a-fg-soft)" }}>
              ({partsKinds})
            </span>
          )}{" "}
          —{" "}
          <Link
            href={`/atlas/calls/${encodeURIComponent(ctx.call_type)}#prompt`}
          >
            inspect composition →
          </Link>
        </div>
      )}

      <PlaygroundEditor ctx={ctx} />
    </div>
  );
}
