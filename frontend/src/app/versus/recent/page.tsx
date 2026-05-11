import Link from "next/link";
import type { Metadata } from "next";
import type { RecentBundle, RecentTextRow } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { JudgmentRowsTable } from "@/components/versus/JudgmentRowsTable";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "../versus.css";

export const metadata: Metadata = { title: "versus · recent" };

type Tab = "completions" | "judgments";

async function getRecent(limit: number): Promise<RecentBundle | null> {
  const qs = new URLSearchParams({ limit: String(limit) });
  const res = await serverFetch(`${API_BASE}/api/versus/recent?${qs}`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

function shortModel(id: string | null | undefined): string {
  if (!id) return "";
  return id.includes("/") ? id.slice(id.indexOf("/") + 1) : id;
}

function flavorPillClass(flavor: string): string {
  if (flavor === "human") return "versus-pill subtle";
  if (flavor === "orch") return "versus-pill rumil";
  return "versus-pill";
}

function deltaPct(words: number, target: number): { pct: number; cls: string } | null {
  if (!target) return null;
  const pct = ((words - target) / target) * 100;
  const cls = words < target * 0.8 || words > target * 1.2 ? "delta-bad" : "delta-good";
  return { pct, cls };
}

export default async function VersusRecentPage({
  searchParams,
}: {
  searchParams: Promise<{ tab?: string; limit?: string }>;
}) {
  const sp = await searchParams;
  const tab: Tab = sp.tab === "judgments" ? "judgments" : "completions";
  const limit = Math.max(1, Math.min(parseInt(sp.limit || "100", 10) || 100, 1000));
  const data = await getRecent(limit);

  if (!data) {
    return (
      <div className="versus-shell">
        <VersusHeader breadcrumb="recent" />
        <main className="versus-main versus-main-full">
          <div className="versus-card">
            <em className="versus-muted">
              Failed to load /api/versus/recent. Make sure the API is running.
            </em>
          </div>
        </main>
      </div>
    );
  }

  const tabHref = (t: Tab) => {
    const qs = new URLSearchParams({ tab: t });
    if (limit !== 100) qs.set("limit", String(limit));
    return `/versus/recent?${qs}`;
  };

  return (
    <div className="versus-shell">
      <VersusHeader
        breadcrumb="recent"
        right={
          <span className="muted">
            {data.texts_total} completions · {data.judgments_total} judgments
          </span>
        }
      />
      <main className="versus-main versus-main-full">
        <div className="recent-tabs">
          <Link
            href={tabHref("completions")}
            className={`recent-tab${tab === "completions" ? " active" : ""}`}
          >
            completions
            <span className="recent-tab-count">
              {data.texts.length === limit
                ? `latest ${limit}`
                : `${data.texts.length}`}
            </span>
          </Link>
          <Link
            href={tabHref("judgments")}
            className={`recent-tab${tab === "judgments" ? " active" : ""}`}
          >
            judgments
            <span className="recent-tab-count">
              {data.judgments.length === limit
                ? `latest ${limit}`
                : `${data.judgments.length}`}
            </span>
          </Link>
          <div className="grow"></div>
          <form method="get" action="/versus/recent" className="recent-limit-form">
            <input type="hidden" name="tab" value={tab} />
            <label className="versus-muted" style={{ fontSize: 12 }}>
              limit
              <input
                name="limit"
                defaultValue={limit}
                type="number"
                min={1}
                max={1000}
                style={{ width: 64, marginLeft: 6 }}
              />
            </label>
          </form>
        </div>

        {tab === "completions" ? (
          <CompletionsTable rows={data.texts} />
        ) : (
          <JudgmentRowsTable rows={data.judgments} />
        )}
      </main>
    </div>
  );
}

function CompletionsTable({ rows }: { rows: RecentTextRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="versus-card">
        <em className="versus-muted">No completions yet.</em>
      </div>
    );
  }
  return (
    <div style={{ overflowX: "auto", marginTop: 6 }}>
      <table className="log">
        <thead>
          <tr>
            <th>ts</th>
            <th>essay</th>
            <th title="human / single-shot / orch / paraphrase">flavor</th>
            <th title="orchestrator workflow (orch rows only)">workflow</th>
            <th>model</th>
            <th title="raw source_id stored on the row">source_id</th>
            <th title="prefix_hash this completion was generated against">prefix</th>
            <th title="response_words (Δ vs target)">words</th>
            <th title="orch budget / single-shot temperature">gen knobs</th>
            <th title="orch run cost in USD (-)">cost</th>
            <th title="orchestrator status / completion status">status</th>
            <th>flags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const delta = deltaPct(r.response_words, r.target_words);
            const knobs: string[] = [];
            if (r.budget != null) knobs.push(`b=${r.budget}`);
            if (r.temperature != null) knobs.push(`T=${r.temperature}`);
            if (r.max_tokens != null) knobs.push(`mt=${r.max_tokens}`);
            if (r.thinking_mode) knobs.push(`th=${r.thinking_mode}`);
            const traceHref = r.trace_url
              ? r.trace_url.replace(/^https?:\/\/[^/]+/, "")
              : null;
            return (
              <tr
                key={r.key}
                className={r.stale ? "is-stale" : ""}
                title={r.config_hash ? `config_hash=${r.config_hash}` : undefined}
              >
                <td className="versus-muted" style={{ whiteSpace: "nowrap" }}>
                  {r.ts}
                </td>
                <td className="versus-mono">
                  <Link href={`/versus/inspect?essay=${encodeURIComponent(r.essay_id)}`}>
                    {r.essay_id}
                  </Link>
                </td>
                <td>
                  <span className={flavorPillClass(r.flavor)}>{r.flavor}</span>
                </td>
                <td className="versus-mono">{r.workflow ?? ""}</td>
                <td className="versus-mono" title={r.model_id ?? undefined}>
                  {shortModel(r.model_id)}
                </td>
                <td className="versus-mono versus-muted" title={r.source_id}>
                  {r.source_id.length > 36 ? r.source_id.slice(0, 36) + "…" : r.source_id}
                </td>
                <td
                  className="versus-mono versus-muted"
                  style={{ fontSize: 11 }}
                  title={r.prefix_hash}
                >
                  {r.prefix_hash.slice(0, 8)}
                </td>
                <td className="versus-mono">
                  {r.response_words}
                  {delta && (
                    <span className={delta.cls} style={{ marginLeft: 4, fontSize: 11 }}>
                      ({delta.pct >= 0 ? "+" : ""}
                      {delta.pct.toFixed(0)}%)
                    </span>
                  )}
                </td>
                <td className="versus-mono" style={{ fontSize: 11 }}>
                  {knobs.join(" · ")}
                </td>
                <td className="versus-mono" style={{ fontSize: 11 }}>
                  {r.rumil_cost_usd != null ? `$${r.rumil_cost_usd.toFixed(2)}` : ""}
                </td>
                <td style={{ fontSize: 11 }}>{r.status ?? ""}</td>
                <td>
                  {traceHref && (
                    <Link
                      href={traceHref}
                      style={{ fontSize: 11 }}
                      title="open rumil trace"
                    >
                      trace
                    </Link>
                  )}
                  {r.stale && (
                    <span
                      className="versus-pill stale"
                      style={{ marginLeft: 4 }}
                      title="prefix_hash differs from the current essay's prefix_hash"
                    >
                      stale
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
