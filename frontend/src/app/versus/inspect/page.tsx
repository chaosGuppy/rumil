import Link from "next/link";
import type { Metadata } from "next";
import type {
  EssayDetail,
  EssayJudgmentsResponse,
  EssayMeta,
  Judgment,
  Source,
} from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { AutoSubmitSelect } from "@/components/versus/AutoSubmitSelect";
import { VersusHeader } from "@/components/versus/VersusHeader";
import { InspectModelFilter } from "@/components/versus/InspectModelFilter";
import { LineDiff } from "@/components/versus/LineDiff";
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
  const cls = words < target * 0.8 || words > target * 1.2 ? "delta-bad" : "delta-good";
  return (
    <span className={cls}>
      ({pct >= 0 ? "+" : ""}{pct.toFixed(0)}%)
    </span>
  );
}

/** Strip a leading `paraphrase:` prefix; otherwise return source_id verbatim. */
function modelOf(sourceId: string): string {
  return sourceId.startsWith("paraphrase:") ? sourceId.slice("paraphrase:".length) : sourceId;
}

/** HTML-id-safe rendering of a source_id or judge_model_base. */
function anchorId(prefix: string, raw: string): string {
  return `${prefix}-${raw.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
}

/** Deterministic light pastel + dark text color pair per variant slug.
 *  Same slug → same color across the page; new variants get a stable
 *  color without needing config. */
function variantColors(vid: string): React.CSSProperties {
  let hash = 0;
  for (let i = 0; i < vid.length; i++) {
    hash = (hash * 31 + vid.charCodeAt(i)) | 0;
  }
  const hue = ((hash % 360) + 360) % 360;
  return {
    background: `hsl(${hue} 55% 92%)`,
    color: `hsl(${hue} 55% 28%)`,
    borderColor: `hsl(${hue} 45% 70%)`,
  };
}

function VariantPill({ vid }: { vid: string }) {
  return (
    <span className="inspect-variant-pill" style={variantColors(vid)}>
      {vid}
    </span>
  );
}

/** Short label for a model id (drop the provider prefix). */
function shortModel(id: string): string {
  const after = id.includes("/") ? id.slice(id.indexOf("/") + 1) : id;
  return id.startsWith("paraphrase:") ? `para:${shortModel(modelOf(id))}` : after;
}

type VariantBundle = {
  id: string;
  detail: EssayDetail;
  sources: Source[];
  judgments: Judgment[];
  staleHidden: number;
  otherVariantHidden: number;
};

export default async function VersusInspectPage({
  searchParams,
}: {
  searchParams: Promise<{ essay?: string }>;
}) {
  const sp = await searchParams;
  const essays = (await fetchJson<EssayMeta[]>("/api/versus/essays")) ?? [];
  const selectedId = sp.essay ?? essays[0]?.id;

  // Bootstrap call to discover the active prefix variants for this
  // essay; subsequent fetches happen in parallel, one per variant.
  const bootstrap = selectedId
    ? await fetchJson<EssayDetail>(`/api/versus/essays/${encodeURIComponent(selectedId)}`)
    : null;
  const variantIds = bootstrap?.prefix_variants.map((v) => v.id) ?? [];

  const variantBundles: VariantBundle[] = selectedId
    ? await Promise.all(
        variantIds.map(async (id) => {
          const qs = `?prefix_label=${encodeURIComponent(id)}`;
          const [detail, sources, judgments] = await Promise.all([
            fetchJson<EssayDetail>(
              `/api/versus/essays/${encodeURIComponent(selectedId)}${qs}`,
            ),
            fetchJson<Source[]>(
              `/api/versus/essays/${encodeURIComponent(selectedId)}/sources${qs}`,
            ),
            fetchJson<EssayJudgmentsResponse>(
              `/api/versus/essays/${encodeURIComponent(selectedId)}/judgments${qs}`,
            ),
          ]);
          return {
            id,
            detail: detail!,
            sources: sources ?? [],
            judgments: judgments?.judgments ?? [],
            staleHidden: judgments?.stale_hidden ?? 0,
            otherVariantHidden: judgments?.other_variant_hidden ?? 0,
          };
        }),
      )
    : [];

  // The non-variant-specific bits (title, markdown, judge templates,
  // paraphrase template) — pull from the first bundle. They'd be
  // identical across variants by construction.
  const head = variantBundles[0];

  // Models seen across all variants, for the model filter.
  const modelSet = new Set<string>();
  for (const v of variantBundles) {
    for (const s of v.sources) {
      if (s.source_id !== "human") modelSet.add(modelOf(s.source_id));
    }
    for (const j of v.judgments) {
      if (j.source_a !== "human") modelSet.add(modelOf(j.source_a));
      if (j.source_b !== "human") modelSet.add(modelOf(j.source_b));
    }
  }
  const modelOptions = Array.from(modelSet).sort().map((m) => ({ value: m, label: m }));

  // Source rows: source_id -> { variantId -> Source }. Renders as one
  // card per source, with one cell per variant.
  const sourceRows = new Map<string, Map<string, Source>>();
  for (const v of variantBundles) {
    for (const s of v.sources) {
      if (!sourceRows.has(s.source_id)) sourceRows.set(s.source_id, new Map());
      sourceRows.get(s.source_id)!.set(v.id, s);
    }
  }
  const sourceOrder = Array.from(sourceRows.keys()).sort((a, b) => {
    if (a === "human") return -1;
    if (b === "human") return 1;
    return a.localeCompare(b);
  });

  // Judgment rows: pair-key -> { variantId -> Judgment }. The pair
  // key is (judge_model, criterion, source_a, source_b) — identical
  // across variants by construction so the same pair lines up.
  const pairKey = (j: Judgment) => `${j.judge_model}|${j.criterion}|${j.source_a}|${j.source_b}`;
  const judgmentRows = new Map<string, Map<string, Judgment>>();
  for (const v of variantBundles) {
    for (const j of v.judgments) {
      const k = pairKey(j);
      if (!judgmentRows.has(k)) judgmentRows.set(k, new Map());
      judgmentRows.get(k)!.set(v.id, j);
    }
  }
  const judgmentOrder = Array.from(judgmentRows.keys()).sort();

  // First key per judge_model_base — the judgment-row that gets the
  // anchor id, so a ToC click jumps to that judge's first row.
  const firstKeyByJudge = new Map<string, string>();
  for (const k of judgmentOrder) {
    const j = judgmentRows.get(k)!.values().next().value as Judgment;
    if (!firstKeyByJudge.has(j.judge_model_base)) {
      firstKeyByJudge.set(j.judge_model_base, k);
    }
  }
  const judgeOrder = Array.from(firstKeyByJudge.keys()).sort();

  // Essay dropdown grouped by source_id, alphabetised within group.
  const grouped: Record<string, { value: string; label: string }[]> = {};
  for (const e of essays) {
    const k = e.source_id || "other";
    (grouped[k] ??= []).push({ value: e.id, label: e.title });
  }
  for (const k of Object.keys(grouped)) {
    grouped[k].sort((a, b) => a.label.localeCompare(b.label));
  }
  const essayGroups = Object.keys(grouped)
    .sort()
    .map((k) => ({ label: k, options: grouped[k] }));

  const essaySelector = (
    <form
      method="get"
      action="/versus/inspect"
      style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}
    >
      <label htmlFor="essay" className="versus-muted" style={{ fontSize: 13 }}>essay</label>
      <AutoSubmitSelect
        id="essay"
        name="essay"
        defaultValue={selectedId ?? ""}
        className="versus-select"
        style={{ padding: "4px 8px", fontSize: 13, minWidth: 240 }}
        groups={essayGroups}
      />
      <noscript>
        <button type="submit" className="versus-button">go</button>
      </noscript>
    </form>
  );

  return (
    <div className="versus-shell">
      <VersusHeader breadcrumb="inspect" right={essaySelector} />
      <main className="versus-main inspect-main">
        {!head ? (
          <p className="versus-muted">
            No essays found yet. Run <code>uv run scripts/fetch_essays.py</code> first.
          </p>
        ) : (
          <div className="inspect-layout">
            <InspectToc
              variantBundles={variantBundles}
              sourceOrder={sourceOrder}
              judgeOrder={judgeOrder}
            />
            <div className="inspect-content">
            <div className="inspect-titlebar">
              <h1>{head.detail.title}</h1>
              <div className="inspect-meta">
                <a href={head.detail.url} target="_blank" rel="noreferrer">source</a>
                <span className="versus-muted">·</span>
                <span className="versus-muted">
                  {head.detail.author}{head.detail.pub_date ? ` · ${head.detail.pub_date}` : ""}
                </span>
                <span className="versus-muted">·</span>
                <span className="versus-muted">target {head.detail.target_words} words</span>
              </div>
              <details className="inspect-footprint" open>
                <summary>provenance footprint</summary>
                <dl className="inspect-footprint-grid">
                  <dt>essay_id</dt>
                  <dd><code>{head.detail.id}</code></dd>
                  <dt>schema_version</dt>
                  <dd><code>{head.detail.schema_version}</code></dd>
                  <dt>prefix variants</dt>
                  <dd>
                    {variantBundles.map((v) => (
                      <span key={v.id} style={{ marginRight: 10 }}>
                        <VariantPill vid={v.id} />{" "}
                        <code className="versus-muted" style={{ fontSize: 11 }}>
                          {v.detail.prefix_config_hash}
                        </code>
                      </span>
                    ))}
                  </dd>
                  <dt>judge_prompt_hash</dt>
                  <dd>
                    <code className="versus-muted" style={{ fontSize: 11 }}>
                      p{head.detail.judge_prompt_hash}
                    </code>{" "}
                    <span className="versus-muted" style={{ fontSize: 11 }}>
                      (criteria[0]: <code>{head.detail.criteria[0]}</code>, blind path)
                    </span>
                  </dd>
                </dl>
              </details>
              {modelOptions.length > 0 && (
                <div className="inspect-modelfilter">
                  <InspectModelFilter options={modelOptions} />
                </div>
              )}
            </div>

            <section className="inspect-prompts" id="prompts">
              <div className="inspect-prompts-col" id="prompts-original">
                <h3 className="inspect-prompts-coltitle">original</h3>
                <CollapsibleBlock summary="essay (normalized markdown)" prose>
                  {head.detail.markdown}
                </CollapsibleBlock>
              </div>
              <div className="inspect-prompts-col" id="prompts-completions">
                <h3 className="inspect-prompts-coltitle">completions</h3>
                {variantBundles.map((v) => (
                  <CollapsibleBlock
                    key={v.id}
                    summary={
                      <>
                        prompt <VariantPill vid={v.id} />
                      </>
                    }
                  >
                    {v.detail.completion_prompt}
                  </CollapsibleBlock>
                ))}
                {variantBundles.length === 2 && (
                  <details className="inspect-collapsible">
                    <summary>
                      diff{" "}
                      <VariantPill vid={variantBundles[0].id} />{" → "}
                      <VariantPill vid={variantBundles[1].id} />
                    </summary>
                    <div className="inspect-diff-wrap">
                      <LineDiff
                        a={variantBundles[0].detail.completion_prompt}
                        b={variantBundles[1].detail.completion_prompt}
                        labelA={variantBundles[0].id}
                        labelB={variantBundles[1].id}
                      />
                    </div>
                  </details>
                )}
              </div>
              <div className="inspect-prompts-col" id="prompts-judging">
                <h3 className="inspect-prompts-coltitle">judging</h3>
                <CollapsibleBlock
                  summary={
                    <>
                      system prompt template <span className="versus-muted">·</span>{" "}
                      <code>{head.detail.criteria[0]}</code>
                    </>
                  }
                >
                  {head.detail.judge_system_prompt_template}
                </CollapsibleBlock>
                <CollapsibleBlock summary="user prompt template (full essay prefix + both continuations)">
                  {head.detail.judge_user_prompt_template}
                </CollapsibleBlock>
              </div>
              <div className="inspect-prompts-col" id="prompts-paraphrase">
                <h3 className="inspect-prompts-coltitle">paraphrase</h3>
                <CollapsibleBlock summary="prompt template">
                  {head.detail.paraphrase_prompt_template}
                </CollapsibleBlock>
              </div>
            </section>

            {variantBundles.some((v) => v.otherVariantHidden + v.staleHidden > 0) && (
              <p className="versus-muted" style={{ fontSize: 12 }}>
                {variantBundles.flatMap((v) =>
                  v.staleHidden > 0
                    ? [
                        <span key={v.id}>
                          <strong>{v.staleHidden}</strong> stale judgment
                          {v.staleHidden === 1 ? "" : "s"} hidden under <code>{v.id}</code>{" "}
                          (prefix_config_hash drift).{" "}
                        </span>,
                      ]
                    : [],
                )}
              </p>
            )}

            <h2 className="inspect-section-head" id="continuations">continuations</h2>
            {sourceOrder.length === 0 ? (
              <p className="versus-muted">No completion rows for this essay yet.</p>
            ) : (
              <div className="inspect-source-list">
                {sourceOrder.map((sid) => (
                  <SourceRow
                    key={sid}
                    sourceId={sid}
                    perVariant={sourceRows.get(sid)!}
                    variantIds={variantIds}
                  />
                ))}
              </div>
            )}

            <h2 className="inspect-section-head" id="judgments">judgments</h2>
            {judgmentOrder.length === 0 ? (
              <p className="versus-muted">No judgments yet for this essay.</p>
            ) : (
              <div className="inspect-judgments">
                {judgmentOrder.map((k) => {
                  const sample = judgmentRows.get(k)!.values().next().value as Judgment;
                  const isFirst = firstKeyByJudge.get(sample.judge_model_base) === k;
                  return (
                    <JudgmentRow
                      key={k}
                      perVariant={judgmentRows.get(k)!}
                      variantIds={variantIds}
                      anchorId={isFirst ? anchorId("judge", sample.judge_model_base) : undefined}
                    />
                  );
                })}
              </div>
            )}
            </div>
          </div>
        )}
      </main>
      <style>{INSPECT_STYLES}</style>
    </div>
  );
}

function CollapsibleBlock({
  summary,
  children,
  prose = false,
  open = false,
}: {
  summary: React.ReactNode;
  children: string;
  prose?: boolean;
  open?: boolean;
}) {
  return (
    <details className="inspect-collapsible" open={open}>
      <summary>{summary}</summary>
      <pre className={prose ? "inspect-pre prose" : "inspect-pre"}>{children}</pre>
    </details>
  );
}

function SourceRow({
  sourceId,
  perVariant,
  variantIds,
}: {
  sourceId: string;
  perVariant: Map<string, Source>;
  variantIds: string[];
}) {
  const isHuman = sourceId === "human";
  const model = modelOf(sourceId);
  return (
    <section
      id={anchorId("src", sourceId)}
      className="inspect-source-row"
      data-filterable
      data-model={isHuman ? "" : model}
      data-always-show={isHuman ? "1" : undefined}
    >
      <header className="inspect-source-row-head">
        <code className="versus-mono">{sourceId}</code>
      </header>
      <div className="inspect-cells">
        {variantIds.map((vid) => {
          const s = perVariant.get(vid);
          return (
            <div key={vid} className="inspect-cell">
              <div className="inspect-cell-head">
                <VariantPill vid={vid} />
                {s ? (
                  <>
                    <span className="versus-muted" style={{ fontSize: 12 }}>{s.kind}</span>
                    <span
                      className="versus-muted"
                      style={{ marginLeft: "auto", fontSize: 12 }}
                    >
                      {s.words}w {deltaSlot(s.words, s.target)}
                    </span>
                  </>
                ) : (
                  <span className="versus-muted" style={{ fontSize: 12 }}>(no row)</span>
                )}
              </div>
              {s?.prompt && (
                <details className="inspect-collapsible inner">
                  <summary>completion prompt (verbatim from row)</summary>
                  <pre className="inspect-pre">{s.prompt}</pre>
                </details>
              )}
              {s && (isHuman ? (
                <details className="inspect-collapsible inner">
                  <summary>held-out remainder ({s.words} words)</summary>
                  <pre className="inspect-pre prose tall">{s.text}</pre>
                </details>
              ) : (
                <pre className="inspect-pre prose tall">{s.text}</pre>
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function JudgmentRow({
  perVariant,
  variantIds,
  anchorId: id,
}: {
  perVariant: Map<string, Judgment>;
  variantIds: string[];
  anchorId?: string;
}) {
  // Header values are identical across variants by construction (same
  // judge model, criterion, sources). Pull from any present cell.
  const sample = perVariant.values().next().value as Judgment;
  const sides = [sample.source_a, sample.source_b]
    .filter((s) => s !== "human")
    .map(modelOf);
  const dataModel = sides.length > 0 ? sides.join(" ") : "";
  const alwaysShow = sides.length === 0 ? "1" : undefined;

  return (
    <article
      id={id}
      className="inspect-judgment-row"
      data-filterable
      data-model={dataModel}
      data-always-show={alwaysShow}
    >
      <header className="inspect-judgment-rowhead">
        <div className="inspect-judgment-id">
          {sample.is_rumil && <span className="versus-pill rumil">rumil</span>}
          <strong className="versus-mono">{sample.judge_model_base}</strong>
          {(sample.prompt_hash || sample.judge_version) && (
            <span className="versus-muted versus-mono" style={{ fontSize: 11 }}>
              {[sample.prompt_hash, sample.judge_version].filter(Boolean).join(" · ")}
            </span>
          )}
          {sample.sampling && (
            <span className="versus-muted versus-mono" style={{ fontSize: 11 }}>
              {samplingTag(sample.sampling)}
            </span>
          )}
        </div>
        <div className="versus-muted" style={{ fontSize: 12 }}>
          {sample.criterion} ·{" "}
          <span className="versus-mono">
            {sample.source_a} <span className="versus-muted">vs</span> {sample.source_b}
          </span>
        </div>
      </header>
      <div className="inspect-cells">
        {variantIds.map((vid) => {
          const j = perVariant.get(vid);
          return (
            <div key={vid} className="inspect-cell">
              <div className="inspect-cell-head">
                <VariantPill vid={vid} />
                {j ? <JudgmentVerdict j={j} /> : (
                  <span className="versus-muted" style={{ fontSize: 12 }}>(no row)</span>
                )}
              </div>
              {j && <JudgmentBody j={j} />}
            </div>
          );
        })}
      </div>
    </article>
  );
}

function JudgmentVerdict({ j }: { j: Judgment }) {
  const verdict = j.verdict ?? "?";
  const winner = j.winner_source ?? "?";
  let wclass = "winner-other";
  if (winner === "human") wclass = "winner-human";
  else if (winner === "tie" || verdict === "tie") wclass = "winner-tie";
  const localPath = localTracePath(j.rumil_trace_url);
  return (
    <span style={{ fontSize: 13 }}>
      verdict <strong>{verdict}</strong> · winner{" "}
      <span className={`inspect-winner ${wclass}`}>{winner}</span>
      {j.preference_label && (
        <>
          {" "}· label <span className="versus-mono">{j.preference_label}</span>
        </>
      )}
      {j.contamination_note && (
        <span className="versus-pill contam" title={j.contamination_note} style={{ marginLeft: 6 }}>
          ⚠ contaminated
        </span>
      )}
      {j.orphaned && (
        <span
          className="versus-pill stale"
          title="No matching completion row for source_a / source_b"
          style={{ marginLeft: 6 }}
        >
          orphan
        </span>
      )}
      {j.rumil_trace_url && (
        <span style={{ marginLeft: 8, fontSize: 12 }}>
          {localPath ? (
            <Link href={localPath}>trace</Link>
          ) : (
            <a href={j.rumil_trace_url} target="_blank" rel="noreferrer">
              trace
            </a>
          )}
          {j.rumil_cost_usd != null && (
            <span className="versus-muted">{" "}· ${j.rumil_cost_usd.toFixed(3)}</span>
          )}
        </span>
      )}
    </span>
  );
}

function JudgmentBody({ j }: { j: Judgment }) {
  return (
    <>
      {j.system_prompt && (
        <details className="inspect-collapsible inner">
          <summary>judge system prompt</summary>
          <pre className="inspect-pre">{j.system_prompt}</pre>
        </details>
      )}
      {j.prompt && (
        <details className="inspect-collapsible inner">
          <summary>judge user prompt</summary>
          <pre className="inspect-pre">{j.prompt}</pre>
        </details>
      )}
      {j.reasoning_text ? (
        <details className="inspect-collapsible inner" open>
          <summary>reasoning</summary>
          <pre className="inspect-pre prose">{j.reasoning_text}</pre>
        </details>
      ) : (
        j.reasoning_preview && (
          <details className="inspect-collapsible inner" open>
            <summary>reasoning (preview only — full text not stored)</summary>
            <pre className="inspect-pre prose">
              {j.reasoning_preview}
              {j.reasoning_preview.length >= 400 && "…"}
            </pre>
          </details>
        )
      )}
    </>
  );
}

function InspectToc({
  variantBundles,
  sourceOrder,
  judgeOrder,
}: {
  variantBundles: VariantBundle[];
  sourceOrder: string[];
  judgeOrder: string[];
}) {
  return (
    <nav className="inspect-toc" aria-label="Inspect contents">
      <ul>
        <li>
          <a href="#prompts">prompts</a>
          <ul>
            <li><a href="#prompts-original">original</a></li>
            <li><a href="#prompts-completions">completions</a></li>
            <li><a href="#prompts-judging">judging</a></li>
            <li><a href="#prompts-paraphrase">paraphrase</a></li>
          </ul>
        </li>
        {sourceOrder.length > 0 && (
          <li>
            <a href="#continuations">continuations</a>
            <ul>
              {sourceOrder.map((sid) => {
                const isHuman = sid === "human";
                const model = modelOf(sid);
                return (
                  <li
                    key={sid}
                    data-filterable
                    data-model={isHuman ? "" : model}
                    data-always-show={isHuman ? "1" : undefined}
                  >
                    <a href={`#${anchorId("src", sid)}`}>
                      {isHuman ? "human" : shortModel(sid)}
                    </a>
                  </li>
                );
              })}
            </ul>
          </li>
        )}
        {judgeOrder.length > 0 && (
          <li>
            <a href="#judgments">judgments</a>
            <ul>
              {judgeOrder.map((jb) => (
                <li
                  key={jb}
                  data-filterable
                  data-model={jb}
                  data-always-show="1"
                >
                  <a href={`#${anchorId("judge", jb)}`}>{shortModel(jb)}</a>
                </li>
              ))}
            </ul>
          </li>
        )}
      </ul>
      <p className="inspect-toc-hint versus-muted">
        {variantBundles.length} variant{variantBundles.length === 1 ? "" : "s"}
      </p>
    </nav>
  );
}

function samplingTag(sampling: { [k: string]: unknown }): string {
  const bits: string[] = [];
  const t = sampling["temperature"];
  const m = sampling["max_tokens"];
  if (t !== undefined && t !== null) bits.push(`T=${t as number | string}`);
  if (m !== undefined && m !== null) bits.push(`mt=${m as number | string}`);
  return bits.join(" · ");
}

const INSPECT_STYLES = `
.inspect-main { max-width: 1500px; }
.inspect-layout {
  display: grid;
  grid-template-columns: 160px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}
.inspect-content { min-width: 0; }
.inspect-toc {
  position: sticky;
  top: 12px;
  font-size: 12px;
  line-height: 1.5;
  align-self: start;
  max-height: calc(100vh - 24px);
  overflow-y: auto;
  padding-right: 4px;
}
.inspect-toc ul {
  list-style: none; padding: 0; margin: 0;
}
.inspect-toc > ul > li { margin-bottom: 8px; }
.inspect-toc > ul > li > a {
  font-weight: 600;
  color: var(--foreground);
  text-decoration: none;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 11px;
}
.inspect-toc ul ul {
  margin: 4px 0 0 0;
  padding-left: 8px;
  border-left: 1px solid var(--color-border);
}
.inspect-toc ul ul li {
  padding: 1px 0 1px 8px;
}
.inspect-toc ul ul a {
  color: var(--color-muted);
  text-decoration: none;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 11px;
  display: block;
  word-break: break-word;
}
.inspect-toc a:hover { color: var(--vaccent-fg); }
.inspect-toc-hint {
  margin-top: 14px;
  font-size: 11px;
}
@media (max-width: 900px) {
  .inspect-layout { grid-template-columns: minmax(0, 1fr); }
  .inspect-toc { position: static; max-height: none; }
}
.inspect-titlebar h1 {
  font-weight: 400; font-size: 22px; margin: 0 0 6px;
  letter-spacing: -0.005em;
}
.inspect-meta {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline;
  font-size: 13px; margin-bottom: 8px;
}
.inspect-meta a { color: var(--vaccent-fg); }
.inspect-modelfilter { margin: 6px 0 14px; }

.inspect-footprint {
  margin: 6px 0 12px;
  font-size: 12px;
}
.inspect-footprint > summary {
  cursor: pointer;
  color: var(--color-muted);
  user-select: none;
  list-style: none;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.inspect-footprint > summary::-webkit-details-marker { display: none; }
.inspect-footprint > summary::before {
  content: "▸"; display: inline-block; margin-right: 6px;
  transition: transform 120ms ease;
}
.inspect-footprint[open] > summary::before { transform: rotate(90deg); }
.inspect-footprint-grid {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 14px;
  margin: 6px 0 0 14px;
}
.inspect-footprint-grid dt {
  color: var(--color-muted);
  font-size: 11px;
  font-family: ui-monospace, Menlo, monospace;
}
.inspect-footprint-grid dd {
  margin: 0;
  font-size: 12px;
}

.inspect-section-head {
  font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--color-muted);
  margin: 28px 0 10px; padding-bottom: 4px;
  border-bottom: 1px solid var(--color-border);
}

.inspect-prompts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 18px;
  margin: 4px 0 6px;
}
@media (max-width: 720px) {
  .inspect-prompts { grid-template-columns: minmax(0, 1fr); }
}
.inspect-prompts-col {
  display: flex; flex-direction: column; gap: 6px;
}
.inspect-prompts-coltitle {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--color-muted);
  margin: 0 0 2px;
}

.inspect-collapsible {
  border: 1px solid var(--color-border);
  border-radius: 4px; background: var(--color-surface);
}
.inspect-collapsible > summary {
  cursor: pointer; padding: 8px 12px; font-size: 13px;
  color: var(--foreground); user-select: none;
  list-style: none;
}
.inspect-collapsible > summary::-webkit-details-marker { display: none; }
.inspect-collapsible > summary::before {
  content: "▸"; display: inline-block; margin-right: 8px;
  color: var(--color-muted); font-size: 11px;
  transition: transform 120ms ease;
}
.inspect-collapsible[open] > summary::before { transform: rotate(90deg); }
.inspect-collapsible > summary:hover { background: var(--vaccent-dim); }
.inspect-collapsible.inner {
  margin-top: 6px;
  background: transparent;
}

.inspect-pre {
  white-space: pre-wrap; word-wrap: break-word;
  font: 12px/1.5 ui-monospace, Menlo, monospace;
  background: var(--background);
  border-top: 1px solid var(--color-border);
  padding: 12px 14px; margin: 0;
  max-height: 60vh; overflow: auto;
}
.inspect-pre.prose {
  font: 14px/1.55 Georgia, "Times New Roman", serif;
}
.inspect-pre.tall { max-height: 70vh; }

.inspect-source-list,
.inspect-judgments {
  display: flex; flex-direction: column; gap: 18px;
}

.inspect-source-row,
.inspect-judgment-row {
  display: flex; flex-direction: column; gap: 8px;
}

.inspect-source-row-head,
.inspect-judgment-rowhead {
  padding: 6px 0;
  border-bottom: 1px solid var(--color-border);
}
.inspect-source-row-head { display: flex; gap: 8px; align-items: baseline; }
.inspect-judgment-rowhead { display: flex; flex-direction: column; gap: 4px; }
.inspect-judgment-id {
  display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
}

.inspect-cells {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 14px;
}
.inspect-cell {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  padding: 10px 12px;
  display: flex; flex-direction: column; gap: 6px;
}

.inspect-diff-wrap {
  border-top: 1px solid var(--color-border);
  background: var(--background);
}
.line-diff {
  font: 12px/1.5 ui-monospace, Menlo, monospace;
}
.line-diff-head {
  display: flex; gap: 16px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--color-border);
  font-size: 11px;
}
.line-diff-label.del { color: hsl(0 60% 40%); }
.line-diff-label.add { color: hsl(140 50% 32%); }
.line-diff-body {
  margin: 0;
  padding: 8px 12px;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 60vh;
  overflow: auto;
}
.line-diff-line { display: inline; }
.line-diff-line.del {
  background: hsl(0 70% 95%);
  color: hsl(0 60% 35%);
}
.line-diff-line.add {
  background: hsl(140 60% 94%);
  color: hsl(140 50% 25%);
}
.line-diff-line.skip {
  color: var(--color-muted);
  font-style: italic;
}
@media (prefers-color-scheme: dark) {
  .line-diff-line.del {
    background: hsl(0 50% 15%);
    color: hsl(0 60% 75%);
  }
  .line-diff-line.add {
    background: hsl(140 40% 13%);
    color: hsl(140 50% 75%);
  }
  .line-diff-label.del { color: hsl(0 60% 70%); }
  .line-diff-label.add { color: hsl(140 50% 65%); }
}

.inspect-variant-pill {
  display: inline-block;
  padding: 1px 9px;
  border: 1px solid;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  font-family: ui-monospace, Menlo, monospace;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.inspect-cell-head {
  display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
}

.inspect-cell .inspect-pre {
  border: 1px solid var(--color-border);
  border-radius: 3px;
  background: var(--background);
  max-height: 60vh;
}
.inspect-cell .inspect-collapsible.inner > summary {
  padding: 6px 10px; font-size: 12px; color: var(--color-muted);
}

.inspect-winner.winner-human { color: #2a7d3a; font-weight: 600; }
.inspect-winner.winner-tie { color: var(--color-muted); font-weight: 600; }
.inspect-winner.winner-other { color: #b04a16; font-weight: 600; }
@media (prefers-color-scheme: dark) {
  .inspect-winner.winner-human { color: #4ec97a; }
  .inspect-winner.winner-other { color: #e08855; }
}
`;
