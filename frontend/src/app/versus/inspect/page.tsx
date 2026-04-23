import Link from "next/link";
import type { Metadata } from "next";
import type { EssayDetail, EssayMeta, Judgment, Source } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "../versus.css";

export const metadata: Metadata = { title: "versus · inspect" };

async function fetchJson<T>(path: string): Promise<T | null> {
  const res = await serverFetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

function localTracePath(url: string | null | undefined): string | null {
  if (!url) return null;
  const m = url.match(/\/traces\/[^?#]+/);
  return m ? m[0] : null;
}

function deltaSlot(words: number, target: number): React.ReactNode {
  if (!target) return null;
  const pct = ((words - target) / target) * 100;
  const color = words < target * 0.8 || words > target * 1.2 ? "#b14" : "#1a8";
  return (
    <span style={{ color }}>
      ({pct >= 0 ? "+" : ""}{pct.toFixed(0)}%)
    </span>
  );
}

export default async function VersusInspectPage({
  searchParams,
}: {
  searchParams: Promise<{ essay?: string }>;
}) {
  const sp = await searchParams;
  const essays = (await fetchJson<EssayMeta[]>("/api/versus/essays")) ?? [];

  const selectedId = sp.essay ?? essays[0]?.id;
  const detail = selectedId
    ? await fetchJson<EssayDetail>(`/api/versus/essays/${encodeURIComponent(selectedId)}`)
    : null;
  const sources = selectedId
    ? ((await fetchJson<Source[]>(
        `/api/versus/essays/${encodeURIComponent(selectedId)}/sources`,
      )) ?? [])
    : [];
  const judgments = selectedId
    ? ((await fetchJson<Judgment[]>(
        `/api/versus/essays/${encodeURIComponent(selectedId)}/judgments`,
      )) ?? [])
    : [];

  const essaySelector = (
    <form method="get" action="/versus/inspect" style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <label htmlFor="essay" className="versus-muted">essay</label>
      <select
        id="essay"
        name="essay"
        defaultValue={selectedId ?? ""}
        className="versus-select"
        style={{ padding: "4px 8px", fontSize: 13 }}
      >
        {essays.map((e) => (
          <option key={e.id} value={e.id}>{e.title}</option>
        ))}
      </select>
      <noscript>
        <button type="submit" className="versus-button">go</button>
      </noscript>
    </form>
  );

  return (
    <div className="versus-shell">
      <VersusHeader breadcrumb="inspect" right={essaySelector} />
      <main className="versus-main">
        {!detail ? (
          <p className="versus-muted">
            No essays found yet. Run <code>uv run scripts/fetch_essays.py</code> first.
          </p>
        ) : (
          <>
            <h1 style={{ fontWeight: 300, fontSize: 26, margin: "0 0 8px" }}>{detail.title}</h1>
            <p className="versus-muted">
              prefix_config_hash: <code>{detail.prefix_config_hash}</code> · target words for completion:{" "}
              <strong>{detail.target_words}</strong>
            </p>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr 1fr",
                gap: 16,
                marginTop: 16,
              }}
            >
              <PromptPane title="Original essay (normalized markdown)" body={detail.markdown} prose />
              <PromptPane
                title="Completion prompt"
                subtitle="What each completion model receives."
                body={detail.completion_prompt}
              />
              <PromptPane
                title="Judge prompt template"
                subtitle={
                  <>
                    Criterion shown: <code>{detail.criteria[0]}</code>. Placeholders are substituted
                    at run time.
                  </>
                }
                body={detail.judge_prompt_template}
              />
            </div>

            <h2 style={{ marginTop: 30, fontSize: 16, fontWeight: 500 }}>
              Paraphrase prompt template
            </h2>
            <p className="versus-muted">
              Sent once per essay × paraphrase model; the output becomes a new contestant{" "}
              <code>paraphrase:&lt;model&gt;</code>.
            </p>
            <PromptPane body={detail.paraphrase_prompt_template} maxHeight="60vh" />

            {judgments.length > 0 && (
              <>
                <h2 style={{ marginTop: 36, fontSize: 16, fontWeight: 500 }}>
                  Judgments for this essay
                </h2>
                <p className="versus-muted">
                  One row per judgment. Rumil-path judges show the raw 7-point preference label and a
                  link to the rumil trace.
                </p>
                <div className="versus-card" style={{ padding: 0, overflow: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                    <thead>
                      <tr style={{ background: "#f6f6f0", textAlign: "left" }}>
                        <th style={{ padding: "8px 10px" }}>judge</th>
                        <th style={{ padding: "8px 10px" }}>criterion</th>
                        <th style={{ padding: "8px 10px" }}>pair</th>
                        <th style={{ padding: "8px 10px" }}>verdict</th>
                        <th style={{ padding: "8px 10px" }}>rumil label</th>
                        <th style={{ padding: "8px 10px" }}>trace</th>
                        <th style={{ padding: "8px 10px" }}>reasoning (preview)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {judgments.map((j, i) => (
                        <JudgmentRow key={i} j={j} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {sources.length > 0 && (
              <>
                <h2 style={{ marginTop: 36, fontSize: 16, fontWeight: 500 }}>
                  Generated sources side-by-side
                </h2>
                <p className="versus-muted">
                  Each column is a "contestant" that the judges pair up. Target was{" "}
                  <strong>{detail.target_words}</strong> words.
                </p>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: `repeat(${sources.length}, 1fr)`,
                    gap: 14,
                    marginTop: 8,
                  }}
                >
                  {sources.map((s) => (
                    <section key={s.source_id}>
                      <div
                        style={{
                          display: "flex",
                          gap: 8,
                          alignItems: "baseline",
                          marginBottom: 6,
                        }}
                      >
                        <span className="versus-pill">{s.kind}</span>
                        <code style={{ fontSize: 13 }}>{s.source_id}</code>
                        <span
                          className="versus-muted"
                          style={{ marginLeft: "auto", fontSize: 12 }}
                        >
                          {s.words} w {deltaSlot(s.words, s.target)}
                        </span>
                      </div>
                      <div
                        className="versus-card judging-prose"
                        style={{ maxHeight: "70vh", overflow: "auto", fontSize: 14 }}
                      >
                        <pre
                          style={{
                            whiteSpace: "pre-wrap",
                            fontFamily: "Georgia, serif",
                            margin: 0,
                          }}
                        >
                          {s.text}
                        </pre>
                      </div>
                    </section>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}

function PromptPane({
  title,
  subtitle,
  body,
  prose = false,
  maxHeight = "80vh",
}: {
  title?: string;
  subtitle?: React.ReactNode;
  body: string;
  prose?: boolean;
  maxHeight?: string;
}) {
  return (
    <section>
      {title && <h2 style={{ fontSize: 16, fontWeight: 500, margin: "0 0 6px" }}>{title}</h2>}
      {subtitle && <p className="versus-muted">{subtitle}</p>}
      <div
        className={prose ? "versus-card judging-prose" : "versus-card"}
        style={{ maxHeight, overflow: "auto" }}
      >
        <pre
          style={{
            whiteSpace: "pre-wrap",
            fontFamily: prose
              ? "Georgia, serif"
              : "ui-monospace, Menlo, monospace",
            fontSize: prose ? 15 : 13,
            margin: 0,
          }}
        >
          {body}
        </pre>
      </div>
    </section>
  );
}

function JudgmentRow({ j }: { j: Judgment }) {
  const localPath = localTracePath(j.rumil_trace_url);
  const rowStyle: React.CSSProperties = {
    borderTop: "1px solid #eee",
    ...(j.contamination_note
      ? { background: "#fde8e8", opacity: 0.65 }
      : j.is_rumil
        ? { background: "#fdfaf3" }
        : {}),
  };

  return (
    <tr style={rowStyle}>
      <td
        style={{
          padding: "6px 10px",
          fontFamily: "ui-monospace, Menlo, monospace",
          whiteSpace: "nowrap",
        }}
      >
        {j.contamination_note && (
          <span
            className="versus-pill"
            style={{ background: "#c63d3d", color: "white", borderColor: "#c63d3d" }}
            title={j.contamination_note}
          >
            ⚠ contaminated
          </span>
        )}
        {j.is_rumil && (
          <span
            className="versus-pill"
            style={{ background: "#e4d7a8", color: "#5a4a1a", borderColor: "#c8b878" }}
          >
            rumil
          </span>
        )}{" "}
        {j.judge_model}
      </td>
      <td style={{ padding: "6px 10px" }}>{j.criterion}</td>
      <td
        style={{
          padding: "6px 10px",
          fontFamily: "ui-monospace, Menlo, monospace",
          fontSize: 12,
        }}
      >
        {j.source_a} vs {j.source_b}
      </td>
      <td style={{ padding: "6px 10px" }}>
        <strong>{j.verdict ?? "?"}</strong>{" "}
        {j.winner_source && j.winner_source !== "tie" && j.verdict !== "tie" && (
          <span className="versus-muted">({j.winner_source})</span>
        )}
      </td>
      <td style={{ padding: "6px 10px", fontSize: 12 }}>
        {j.rumil_preference_label ?? <span className="versus-muted">-</span>}
      </td>
      <td style={{ padding: "6px 10px", fontSize: 12 }}>
        {j.rumil_trace_url ? (
          <>
            {localPath ? (
              <Link href={localPath}>trace</Link>
            ) : (
              <a href={j.rumil_trace_url} target="_blank" rel="noreferrer">
                trace
              </a>
            )}{" "}
            {j.rumil_cost_usd != null && (
              <span className="versus-muted">${j.rumil_cost_usd.toFixed(3)}</span>
            )}
          </>
        ) : (
          <span className="versus-muted">-</span>
        )}
      </td>
      <td style={{ padding: "6px 10px", maxWidth: 420 }}>
        <span
          className="versus-muted"
          style={{ fontFamily: "Georgia, serif", fontSize: 12 }}
        >
          {j.reasoning_preview}
          {j.reasoning_preview.length >= 400 && "…"}
        </span>
      </td>
    </tr>
  );
}
