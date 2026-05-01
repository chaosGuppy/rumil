import Link from "next/link";
import type { Metadata } from "next";
import type {
  EssayDetail,
  EssayJudgmentsResponse,
  EssayMeta,
  Judgment,
  ResultsBundle,
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

/** HTML-id-safe rendering of a source_id or config_hash. */
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

/** Trailing config-hash chunk on a legacy compound judge_model_id, e.g.
 *  "blind:claude-haiku-4-5:general_quality:c19517145" → "c19517145". Returns
 *  the empty string when the id has no recognizable suffix (raw model id). */
function configHashSuffix(judgeModelId: string): string {
  const parts = judgeModelId.split(":");
  const last = parts[parts.length - 1];
  return last.startsWith("c") && last.length > 1 ? last : "";
}

/** Build a map id → display label, appending the config-hash suffix when two
 *  ids would otherwise share the same short name. Used so thinking/effort
 *  variants of the same model render as distinct columns instead of two
 *  visually-identical labels. */
function disambiguatedLabels(judgeModelIds: string[]): Map<string, string> {
  const labels = new Map<string, string>();
  const baseCounts = new Map<string, number>();
  for (const id of judgeModelIds) {
    const base = shortModel(id);
    baseCounts.set(base, (baseCounts.get(base) ?? 0) + 1);
  }
  for (const id of judgeModelIds) {
    const base = shortModel(id);
    if ((baseCounts.get(base) ?? 0) > 1) {
      const suffix = configHashSuffix(id);
      labels.set(id, suffix ? `${base}:${suffix}` : base);
    } else {
      labels.set(id, base);
    }
  }
  return labels;
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

  // Corpus baseline: per-variant pick-human rate, averaged across judges
  // for each gen_model. Used by the essay-vs-corpus outlier block. Fetches
  // run in parallel with the variant bundles' downstream rendering; if any
  // request fails we just hide the outlier sub-block.
  const corpusByVariant = new Map<string, Map<string, number>>();
  if (variantIds.length > 0) {
    const settled = await Promise.all(
      variantIds.map((id) =>
        fetchJson<ResultsBundle>(
          `/api/versus/results?include_stale=true&prefix_label=${encodeURIComponent(id)}`,
        ),
      ),
    );
    for (let i = 0; i < variantIds.length; i++) {
      const bundle = settled[i];
      if (!bundle) continue;
      const compMatrix = bundle.main_matrices.find((m) => m.condition === "completion");
      if (!compMatrix) continue;
      const byGen = new Map<string, { num: number; den: number }>();
      for (const c of compMatrix.cells) {
        if (c.cell.pct === null || c.cell.pct === undefined) continue;
        const acc = byGen.get(c.gen_model) ?? { num: 0, den: 0 };
        acc.num += c.cell.pct * c.cell.n;
        acc.den += c.cell.n;
        byGen.set(c.gen_model, acc);
      }
      const rates = new Map<string, number>();
      for (const [gen, acc] of byGen) {
        if (acc.den > 0) rates.set(gen, acc.num / acc.den);
      }
      corpusByVariant.set(variantIds[i], rates);
    }
  }

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

  // First key per config_hash — the judgment-row that gets the
  // anchor id, so a ToC click jumps to that judge's first row.
  // Also map config_hash -> judge_model_id so column headers and ToC
  // entries render a model name rather than the opaque hex hash.
  const firstKeyByJudge = new Map<string, string>();
  const judgeModelIdByHash = new Map<string, string>();
  for (const k of judgmentOrder) {
    const j = judgmentRows.get(k)!.values().next().value as Judgment;
    if (!firstKeyByJudge.has(j.config_hash)) {
      firstKeyByJudge.set(j.config_hash, k);
    }
    if (!judgeModelIdByHash.has(j.config_hash)) {
      judgeModelIdByHash.set(j.config_hash, j.judge_model_id);
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
              judgeModelIdByHash={judgeModelIdByHash}
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

            <ResultsPanel
              variantBundles={variantBundles}
              corpusByVariant={corpusByVariant}
            />

            <h2 className="inspect-section-head" id="judgments">judgments</h2>
            {judgmentOrder.length === 0 ? (
              <p className="versus-muted">No judgments yet for this essay.</p>
            ) : (
              <div className="inspect-judgments">
                {judgmentOrder.map((k) => {
                  const sample = judgmentRows.get(k)!.values().next().value as Judgment;
                  const isFirst = firstKeyByJudge.get(sample.config_hash) === k;
                  return (
                    <JudgmentRow
                      key={k}
                      perVariant={judgmentRows.get(k)!}
                      variantIds={variantIds}
                      anchorId={isFirst ? anchorId("judge", sample.config_hash) : undefined}
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

type WinAccum = {
  humanWins: number;
  ties: number;
  genWins: number;
  n: number;
  score7Sum: number;
  score7N: number;
};

function emptyAccum(): WinAccum {
  return { humanWins: 0, ties: 0, genWins: 0, n: 0, score7Sum: 0, score7N: 0 };
}

/** Aggregate one judgment into a (gen, judge) accum where gen is the
 *  non-human side. Outcome is human-centric ("human" wins, "gen" wins,
 *  or tie) so downstream cell formulas can compute pick-human rate
 *  without re-inverting. Returns null when neither side is human. */
function genVsHumanAccum(j: Judgment): { gen: string; outcome: "human" | "tie" | "gen" } | null {
  const aHuman = j.source_a === "human";
  const bHuman = j.source_b === "human";
  if (aHuman === bHuman) return null;
  const gen = aHuman ? modelOf(j.source_b) : modelOf(j.source_a);
  if (j.verdict === "tie" || j.winner_source === "tie" || !j.winner_source) {
    return { gen, outcome: "tie" };
  }
  if (j.winner_source === "human") return { gen, outcome: "human" };
  return { gen, outcome: "gen" };
}

/** Mirrors versus.analyze.cell_color so inspect win-matrix cells use
 *  the same orange→gray→green ramp as /results. */
function pctColor(pct: number, n: number): { bg: string; fg: string } {
  if (n === 0) return { bg: "transparent", fg: "var(--color-muted)" };
  let r: number;
  let g: number;
  let b: number;
  if (pct <= 0.5) {
    const t = pct / 0.5;
    r = 255;
    g = Math.round(111 + (238 - 111) * t);
    b = Math.round(67 + (238 - 67) * t);
  } else {
    const t = (pct - 0.5) / 0.5;
    r = Math.round(238 - (238 - 110) * t);
    g = Math.round(238 - (238 - 199) * t);
    b = Math.round(238 - (238 - 120) * t);
  }
  return { bg: `rgb(${r},${g},${b})`, fg: "#111" };
}

/** 7-point preference label → score in [0, 1] for side A (display_first).
 *  Equally-spaced: B-strong=0, indifferent=0.5, A-strong=1. */
const PREF_LABEL_TO_A_SCORE: Record<string, number> = {
  "A strongly preferred": 1,
  "A somewhat preferred": 5 / 6,
  "A slightly preferred": 4 / 6,
  "Approximately indifferent between A and B": 0.5,
  "B slightly preferred": 2 / 6,
  "B somewhat preferred": 1 / 6,
  "B strongly preferred": 0,
};

/** Per-judgment score in [0, 1] for the human side, derived from the
 *  7-pt preference_label when present. Returns null when the judgment
 *  has no human side or no usable label — caller should fall back to
 *  the binary winner_source signal. */
function humanScore7pt(j: Judgment): number | null {
  if (!j.preference_label) return null;
  const aScore = PREF_LABEL_TO_A_SCORE[j.preference_label];
  if (aScore === undefined) return null;
  if (j.display_first === "human") return aScore;
  if (j.display_second === "human") return 1 - aScore;
  return null;
}

function ResultsPanel({
  variantBundles,
  corpusByVariant,
}: {
  variantBundles: VariantBundle[];
  corpusByVariant: Map<string, Map<string, number>>;
}) {
  const hasAnyJudgments = variantBundles.some((v) => v.judgments.length > 0);
  return (
    <>
      <h2 className="inspect-section-head" id="results">results</h2>
      <details className="inspect-collapsible inspect-results" open>
        <summary>
          {hasAnyJudgments
            ? `per-essay aggregates across the ${variantBundles.length} active variant${variantBundles.length === 1 ? "" : "s"}`
            : "no judgments yet for this essay"}
        </summary>
        <div className="inspect-results-body">
          {hasAnyJudgments ? (
            <>
              <ResultsWinMatrix variantBundles={variantBundles} />
              <ResultsConsistency variantBundles={variantBundles} />
              <ResultsOutlier
                variantBundles={variantBundles}
                corpusByVariant={corpusByVariant}
              />
              <ResultsVariantFlip variantBundles={variantBundles} />
            </>
          ) : (
            <ResultsEmptyPlaceholders />
          )}
        </div>
      </details>
    </>
  );
}

function ResultsEmptyPlaceholders() {
  const sections: { id: string; title: string }[] = [
    { id: "results-winmatrix", title: "win matrix" },
    { id: "results-consistency", title: "verdict consistency" },
    { id: "results-outlier", title: "essay vs corpus" },
    { id: "results-flip", title: "variant flips" },
  ];
  return (
    <>
      {sections.map((s) => (
        <section key={s.id} id={s.id} className="inspect-results-block">
          <header className="inspect-results-blockhead">
            <h3>{s.title}</h3>
            <p className="versus-muted" style={{ fontSize: 12 }}>
              Pending — no judgments yet for this essay.
            </p>
          </header>
        </section>
      ))}
    </>
  );
}

function ResultsWinMatrix({ variantBundles }: { variantBundles: VariantBundle[] }) {
  return (
    <section id="results-winmatrix" className="inspect-results-block">
      <header className="inspect-results-blockhead">
        <h3>win matrix</h3>
        <p className="versus-muted">
          % of judgments that picked the human continuation, per (gen × judge). Cells with
          n=0 are blank. Aggregated within this essay only.
        </p>
      </header>
      <div className="inspect-winmatrix-grid">
        {variantBundles.map((v) => {
          const acc = new Map<string, Map<string, WinAccum>>();
          const judgesSet = new Set<string>();
          const gensSet = new Set<string>();
          for (const j of v.judgments) {
            const r = genVsHumanAccum(j);
            if (!r) continue;
            // Group columns by the compound judge_model (full display
            // string), not the bare judge_model_id — under the new
            // ``<task>/<workflow>:<model>:c<hash8>`` shape, multiple
            // workflows (blind / two_phase / draft_and_edit) at the same
            // model would otherwise silently collapse into one cell.
            // Using config_hash would over-fragment in the other
            // direction; judge_model is the right granularity.
            const judge = j.judge_model;
            judgesSet.add(judge);
            gensSet.add(r.gen);
            const row = acc.get(r.gen) ?? new Map<string, WinAccum>();
            const cell = row.get(judge) ?? emptyAccum();
            cell.n += 1;
            if (r.outcome === "human") cell.humanWins += 1;
            else if (r.outcome === "tie") cell.ties += 1;
            else cell.genWins += 1;
            const s7 = humanScore7pt(j);
            if (s7 !== null) {
              cell.score7Sum += s7;
              cell.score7N += 1;
            }
            row.set(judge, cell);
            acc.set(r.gen, row);
          }
          const judges = Array.from(judgesSet).sort();
          const gens = Array.from(gensSet).sort();
          return (
            <div key={v.id} className="inspect-winmatrix-card">
              <div className="inspect-winmatrix-cardhead">
                <VariantPill vid={v.id} />
                <span className="versus-muted" style={{ fontSize: 11 }}>
                  vs human · pick-human %
                </span>
              </div>
              {gens.length === 0 ? (
                <p className="versus-muted" style={{ fontSize: 12, padding: "6px 2px" }}>
                  No gen-vs-human pairs under this variant.
                </p>
              ) : (
                <table className="matrix-table small inspect-winmatrix-table">
                  <thead>
                    <tr>
                      <th></th>
                      {(() => {
                        const labels = disambiguatedLabels(judges);
                        return judges.map((jb) => (
                          <th key={jb} title={jb}>
                            {labels.get(jb) ?? shortModel(jb)}
                          </th>
                        ));
                      })()}
                    </tr>
                  </thead>
                  <tbody>
                    {gens.map((g) => (
                      <tr
                        key={g}
                        data-filterable
                        data-model={g}
                      >
                        <th title={g}>{shortModel(g)}</th>
                        {judges.map((jb) => {
                          const cell = acc.get(g)?.get(jb);
                          if (!cell || cell.n === 0) {
                            return <td key={jb} className="matrix-cell-empty"></td>;
                          }
                          const pct = (cell.humanWins + 0.5 * cell.ties) / cell.n;
                          const colors = pctColor(pct, cell.n);
                          const lowN = cell.n < 5;
                          const pct7 = cell.score7N > 0 ? cell.score7Sum / cell.score7N : null;
                          const tooltip =
                            `pick-human ${Math.round(pct * 100)}% (binary, ties=½) · n=${cell.n}` +
                            ` (${cell.humanWins}H / ${cell.ties}T / ${cell.genWins}M)` +
                            (pct7 !== null
                              ? ` · 7pt-avg ${(pct7 * 100).toFixed(1)}% (n=${cell.score7N})`
                              : "");
                          return (
                            <td
                              key={jb}
                              className={lowN ? "matrix-cell low-n" : "matrix-cell"}
                              style={{ background: colors.bg, color: colors.fg }}
                              title={tooltip}
                            >
                              {lowN && <span className="low-n-mark">~</span>}
                              {Math.round(pct * 100)}
                              <span className="n">{cell.n}</span>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

type ConsistencyKey = {
  variantId: string;
  source_a: string;
  source_b: string;
  criterion: string;
  human: number;
  other: number;
  ties: number;
  total: number;
};

function ResultsConsistency({ variantBundles }: { variantBundles: VariantBundle[] }) {
  const groups: ConsistencyKey[] = [];
  for (const v of variantBundles) {
    const buckets = new Map<string, ConsistencyKey>();
    for (const j of v.judgments) {
      const k = `${j.source_a}|${j.source_b}|${j.criterion}`;
      const slot = buckets.get(k) ?? {
        variantId: v.id,
        source_a: j.source_a,
        source_b: j.source_b,
        criterion: j.criterion,
        human: 0,
        other: 0,
        ties: 0,
        total: 0,
      };
      slot.total += 1;
      const w = j.winner_source;
      if (w === "human") slot.human += 1;
      else if (w === "tie" || j.verdict === "tie" || !w) slot.ties += 1;
      else slot.other += 1;
      buckets.set(k, slot);
    }
    for (const g of buckets.values()) groups.push(g);
  }
  groups.sort((a, b) => {
    if (a.total === 0 || b.total === 0) return b.total - a.total;
    const aDecisive = Math.max(a.human, a.other, a.ties) / a.total;
    const bDecisive = Math.max(b.human, b.other, b.ties) / b.total;
    if (bDecisive !== aDecisive) return bDecisive - aDecisive;
    return b.total - a.total;
  });
  return (
    <section id="results-consistency" className="inspect-results-block">
      <header className="inspect-results-blockhead">
        <h3>verdict consistency per pair</h3>
        <p className="versus-muted">
          Each row is a (variant, source_a, source_b, criterion) group across all judges.
          Sorted most-decisive first — pairs where judges agreed most strongly bubble up.
        </p>
      </header>
      {groups.length === 0 ? (
        <p className="versus-muted" style={{ fontSize: 12 }}>No grouped verdicts to show.</p>
      ) : (
        <ul className="inspect-consistency-list">
          {groups.map((g, idx) => {
            const sides = [g.source_a, g.source_b]
              .filter((s) => s !== "human")
              .map(modelOf);
            const dataModel = sides.length > 0 ? sides.join(" ") : "";
            const alwaysShow = sides.length === 0 ? "1" : undefined;
            const labelParts: string[] = [];
            if (g.human > 0) labelParts.push(`${g.human} human`);
            if (g.ties > 0) labelParts.push(`${g.ties} tie`);
            if (g.other > 0) labelParts.push(`${g.other} model`);
            const label = labelParts.join(" / ") || `${g.total} undecided`;
            return (
              <li
                key={`${g.variantId}|${g.source_a}|${g.source_b}|${g.criterion}|${idx}`}
                className="inspect-consistency-row"
                data-filterable
                data-model={dataModel}
                data-always-show={alwaysShow}
              >
                <div className="inspect-consistency-meta">
                  <VariantPill vid={g.variantId} />
                  <span className="versus-mono inspect-consistency-pair">
                    {g.source_a} <span className="versus-muted">vs</span> {g.source_b}
                  </span>
                  <span className="versus-muted" style={{ fontSize: 11 }}>{g.criterion}</span>
                </div>
                <div className="inspect-consistency-bar" aria-hidden="true">
                  {g.human > 0 && (
                    <span
                      className="inspect-bar-seg seg-human"
                      style={{ flex: g.human }}
                      title={`${g.human} pick human`}
                    />
                  )}
                  {g.ties > 0 && (
                    <span
                      className="inspect-bar-seg seg-tie"
                      style={{ flex: g.ties }}
                      title={`${g.ties} tie`}
                    />
                  )}
                  {g.other > 0 && (
                    <span
                      className="inspect-bar-seg seg-other"
                      style={{ flex: g.other }}
                      title={`${g.other} pick model`}
                    />
                  )}
                </div>
                <div className="inspect-consistency-label">
                  <strong>{label}</strong>
                  <span className="versus-muted" style={{ fontSize: 11 }}>n={g.total}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ResultsOutlier({
  variantBundles,
  corpusByVariant,
}: {
  variantBundles: VariantBundle[];
  corpusByVariant: Map<string, Map<string, number>>;
}) {
  if (corpusByVariant.size === 0) return null;
  type Row = {
    variantId: string;
    gen: string;
    essayPct: number;
    corpusPct: number | null;
    deltaPp: number | null;
    n: number;
  };
  const rows: Row[] = [];
  for (const v of variantBundles) {
    const perGen = new Map<string, WinAccum>();
    for (const j of v.judgments) {
      const r = genVsHumanAccum(j);
      if (!r) continue;
      const slot = perGen.get(r.gen) ?? emptyAccum();
      slot.n += 1;
      if (r.outcome === "human") slot.humanWins += 1;
      else if (r.outcome === "gen") slot.genWins += 1;
      else slot.ties += 1;
      perGen.set(r.gen, slot);
    }
    const corpus = corpusByVariant.get(v.id);
    for (const [gen, acc] of perGen) {
      if (acc.n === 0) continue;
      const essayPct = (acc.humanWins + 0.5 * acc.ties) / acc.n;
      const corpusPct = corpus?.get(gen) ?? null;
      const deltaPp = corpusPct === null ? null : (essayPct - corpusPct) * 100;
      rows.push({ variantId: v.id, gen, essayPct, corpusPct, deltaPp, n: acc.n });
    }
  }
  rows.sort((a, b) => {
    const ad = a.deltaPp === null ? -1 : Math.abs(a.deltaPp);
    const bd = b.deltaPp === null ? -1 : Math.abs(b.deltaPp);
    return bd - ad;
  });
  return (
    <section id="results-outlier" className="inspect-results-block">
      <header className="inspect-results-blockhead">
        <h3>essay vs corpus</h3>
        <p className="versus-muted">
          For each (gen, variant): pick-human rate on this essay vs the corpus average
          (averaged across judges). Cells whose delta exceeds 20pp are flagged — this
          essay over- or under-performs that gen-model relative to its baseline.
        </p>
      </header>
      {rows.length === 0 ? (
        <p className="versus-muted" style={{ fontSize: 12 }}>
          Not enough decisive judgments for any (gen, variant) cell.
        </p>
      ) : (
        <ul className="inspect-outlier-list">
          {rows.map((r) => {
            const flagged = r.deltaPp !== null && Math.abs(r.deltaPp) >= 20;
            const klass = flagged
              ? r.deltaPp! > 0
                ? "outlier-up"
                : "outlier-down"
              : "outlier-flat";
            return (
              <li
                key={`${r.variantId}|${r.gen}`}
                className={`inspect-outlier-row ${klass}`}
                data-filterable
                data-model={r.gen}
              >
                <div className="inspect-outlier-id">
                  <VariantPill vid={r.variantId} />
                  <strong className="versus-mono">{shortModel(r.gen)}</strong>
                </div>
                <div className="inspect-outlier-nums">
                  <span title="this essay">
                    <span className="versus-muted">essay</span>{" "}
                    <strong>{Math.round(r.essayPct * 100)}%</strong>
                    <span className="versus-muted" style={{ fontSize: 11 }}> n={r.n}</span>
                  </span>
                  <span title="corpus average for this gen × variant">
                    <span className="versus-muted">corpus</span>{" "}
                    {r.corpusPct === null ? (
                      <span className="versus-muted">—</span>
                    ) : (
                      <strong>{Math.round(r.corpusPct * 100)}%</strong>
                    )}
                  </span>
                  <span title="delta in percentage points">
                    <span className="versus-muted">Δ</span>{" "}
                    {r.deltaPp === null ? (
                      <span className="versus-muted">—</span>
                    ) : (
                      <strong className={`outlier-delta ${klass}`}>
                        {r.deltaPp > 0 ? "+" : ""}
                        {r.deltaPp.toFixed(0)}pp
                      </strong>
                    )}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ResultsVariantFlip({ variantBundles }: { variantBundles: VariantBundle[] }) {
  if (variantBundles.length < 2) return null;
  type Pair = {
    gen: string;
    judge: string;
    criterion: string;
    source_a: string;
    source_b: string;
    perVariant: Map<string, string>;
  };
  const byKey = new Map<string, Pair>();
  for (const v of variantBundles) {
    for (const j of v.judgments) {
      // Bucket by the compound judge_model (full display string) so the
      // same (judge identity, pair, criterion) groups across variants.
      // Using bare judge_model_id would silently merge new-shape rows
      // that share a base model but differ in workflow (blind vs
      // two_phase vs …); using per-row config_hash would never match
      // across variants and the section would always report 0 comparable.
      const k = `${j.judge_model}|${j.criterion}|${j.source_a}|${j.source_b}`;
      const slot = byKey.get(k) ?? {
        gen: [j.source_a, j.source_b].filter((s) => s !== "human").map(modelOf).join("+") ||
          "human",
        judge: j.judge_model,
        criterion: j.criterion,
        source_a: j.source_a,
        source_b: j.source_b,
        perVariant: new Map<string, string>(),
      };
      slot.perVariant.set(v.id, j.winner_source ?? "?");
      byKey.set(k, slot);
    }
  }
  const comparable = Array.from(byKey.values()).filter((p) => p.perVariant.size >= 2);
  const flips = comparable.filter((p) => {
    const winners = Array.from(p.perVariant.values());
    return winners.some((w) => w !== winners[0]);
  });
  return (
    <section id="results-flip" className="inspect-results-block">
      <header className="inspect-results-blockhead">
        <h3>variant flips</h3>
        <p className="versus-muted">
          Pairs where the judged winner differs across active prefix variants — same gen,
          same judge, same criterion, same sides.
        </p>
      </header>
      <p className="inspect-flip-summary">
        <strong>{flips.length}</strong> pair{flips.length === 1 ? "" : "s"} flip winner
        across variants{" "}
        <span className="versus-muted">out of {comparable.length} comparable</span>
      </p>
      {flips.length > 0 && (
        <ul className="inspect-flip-list">
          {(() => {
            const judgeLabels = disambiguatedLabels(flips.map((p) => p.judge));
            return flips.map((p, idx) => {
            const sides = [p.source_a, p.source_b].filter((s) => s !== "human").map(modelOf);
            const dataModel = sides.length > 0 ? sides.join(" ") : "";
            const alwaysShow = sides.length === 0 ? "1" : undefined;
            return (
              <li
                key={`${p.judge}|${p.criterion}|${p.source_a}|${p.source_b}|${idx}`}
                className="inspect-flip-row"
                data-filterable
                data-model={dataModel}
                data-always-show={alwaysShow}
              >
                <div className="inspect-flip-pair">
                  <span className="versus-mono">{judgeLabels.get(p.judge) ?? shortModel(p.judge)}</span>{" "}
                  <span className="versus-muted">·</span>{" "}
                  <span className="versus-muted" style={{ fontSize: 11 }}>{p.criterion}</span>{" "}
                  <span className="versus-muted">·</span>{" "}
                  <span className="versus-mono inspect-flip-sides">
                    {p.source_a} <span className="versus-muted">vs</span> {p.source_b}
                  </span>
                </div>
                <div className="inspect-flip-winners">
                  {Array.from(p.perVariant.entries()).map(([vid, winner]) => {
                    const wclass =
                      winner === "human"
                        ? "winner-human"
                        : winner === "tie"
                          ? "winner-tie"
                          : "winner-other";
                    return (
                      <span key={vid} className="inspect-flip-winner-cell">
                        <VariantPill vid={vid} />
                        <span className={`inspect-winner ${wclass}`}>{winner}</span>
                      </span>
                    );
                  })}
                </div>
              </li>
            );
            });
          })()}
        </ul>
      )}
    </section>
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
          <strong className="versus-mono" title={sample.judge_model}>
            {sample.judge_model_id}
          </strong>
          <span className="versus-muted versus-mono" style={{ fontSize: 11 }}>
            c{sample.config_hash.slice(0, 8)}
          </span>
          {sample.prompt_hash && (
            <span className="versus-muted versus-mono" style={{ fontSize: 11 }}>
              {sample.prompt_hash}
            </span>
          )}
          {(sample.model_config_snapshot || sample.sampling) && (
            <span className="versus-muted versus-mono" style={{ fontSize: 11 }}>
              {modelConfigTag(sample.model_config_snapshot, sample.sampling)}
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
  judgeModelIdByHash,
}: {
  variantBundles: VariantBundle[];
  sourceOrder: string[];
  judgeOrder: string[];
  judgeModelIdByHash: Map<string, string>;
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
        <li>
          <a href="#results">results</a>
          <ul>
            <li><a href="#results-winmatrix">win matrix</a></li>
            <li><a href="#results-consistency">verdict consistency</a></li>
            <li><a href="#results-outlier">essay vs corpus</a></li>
            <li><a href="#results-flip">variant flips</a></li>
          </ul>
        </li>
        {judgeOrder.length > 0 && (
          <li>
            <a href="#judgments">judgments</a>
            <ul>
              {(() => {
                const ids = judgeOrder.map((jb) => judgeModelIdByHash.get(jb) ?? jb);
                const labels = disambiguatedLabels(ids);
                return judgeOrder.map((jb) => {
                  const id = judgeModelIdByHash.get(jb) ?? jb;
                  return (
                    <li
                      key={jb}
                      data-filterable
                      data-model={jb}
                      data-always-show="1"
                    >
                      <a href={`#${anchorId("judge", jb)}`}>
                        {labels.get(id) ?? shortModel(id)}
                      </a>
                    </li>
                  );
                });
              })()}
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

function modelConfigTag(
  modelConfig: { [k: string]: unknown } | null | undefined,
  sampling: { [k: string]: unknown } | null | undefined,
): string {
  // Prefer the full ModelConfig snapshot (post-registry rows). Fall back
  // to the legacy sampling-only field for rows written before the schema
  // migration. Renders the per-row condition compactly:
  //   T=0 · mt=32000 · think=adaptive · effort=xhigh · tier=priority
  // Empty bits are dropped so the tag stays short on plain rows.
  const src = modelConfig ?? sampling ?? {};
  const bits: string[] = [];
  const t = src["temperature"];
  const m = src["max_tokens"];
  if (t !== undefined && t !== null) bits.push(`T=${t as number | string}`);
  if (m !== undefined && m !== null) bits.push(`mt=${m as number | string}`);
  const thinking = src["thinking"];
  if (thinking && typeof thinking === "object") {
    const th = thinking as { type?: string; display?: string };
    const label = th.type
      ? th.display
        ? `${th.type}/${th.display}`
        : th.type
      : "on";
    bits.push(`think=${label}`);
  }
  const effort = src["effort"];
  if (typeof effort === "string") bits.push(`effort=${effort}`);
  const mtt = src["max_thinking_tokens"];
  if (typeof mtt === "number") bits.push(`mtt=${mtt}`);
  const tier = src["service_tier"];
  if (typeof tier === "string") bits.push(`tier=${tier}`);
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

.inspect-results > summary {
  font-size: 13px;
  color: var(--foreground);
}
.inspect-results-body {
  display: flex; flex-direction: column; gap: 22px;
  padding: 14px 14px 18px;
  border-top: 1px solid var(--color-border);
}
.inspect-results-block {
  display: flex; flex-direction: column; gap: 8px;
}
.inspect-results-blockhead { display: flex; flex-direction: column; gap: 2px; }
.inspect-results-blockhead h3 {
  margin: 0;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--foreground);
}
.inspect-results-blockhead p {
  margin: 0;
  font-size: 12px;
  line-height: 1.45;
}

.inspect-winmatrix-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.inspect-winmatrix-card {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  padding: 10px 12px;
  display: flex; flex-direction: column; gap: 8px;
}
.inspect-winmatrix-cardhead {
  display: flex; gap: 8px; align-items: baseline;
}
.inspect-winmatrix-table th {
  font-size: 11px;
}
.inspect-winmatrix-table .matrix-cell-empty {
  background: transparent;
  color: var(--color-muted);
}

.inspect-consistency-list {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: 4px;
}
.inspect-consistency-row {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(80px, 200px) minmax(120px, max-content);
  gap: 12px;
  align-items: center;
  padding: 6px 8px;
  border-radius: 4px;
}
.inspect-consistency-row:hover { background: var(--color-surface); }
.inspect-consistency-meta {
  display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
  font-size: 12px;
  min-width: 0;
}
.inspect-consistency-pair {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.inspect-consistency-bar {
  display: flex;
  height: 10px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}
.inspect-bar-seg { display: block; }
.inspect-bar-seg.seg-human { background: hsl(135 45% 48%); }
.inspect-bar-seg.seg-tie { background: hsl(0 0% 70%); }
.inspect-bar-seg.seg-other { background: hsl(20 75% 55%); }
@media (prefers-color-scheme: dark) {
  .inspect-bar-seg.seg-human { background: hsl(135 45% 42%); }
  .inspect-bar-seg.seg-tie { background: hsl(0 0% 45%); }
  .inspect-bar-seg.seg-other { background: hsl(20 70% 50%); }
}
.inspect-consistency-label {
  display: flex; gap: 8px; align-items: baseline;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.inspect-outlier-list {
  list-style: none; padding: 0; margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 6px;
}
.inspect-outlier-row {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px;
  border-radius: 4px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  font-size: 12px;
}
.inspect-outlier-row.outlier-up {
  border-left: 3px solid hsl(135 50% 40%);
  background: hsl(135 45% 96%);
}
.inspect-outlier-row.outlier-down {
  border-left: 3px solid hsl(15 75% 50%);
  background: hsl(15 70% 96%);
}
@media (prefers-color-scheme: dark) {
  .inspect-outlier-row.outlier-up {
    background: hsl(135 35% 12%);
  }
  .inspect-outlier-row.outlier-down {
    background: hsl(15 50% 14%);
  }
}
.inspect-outlier-id {
  display: flex; gap: 8px; align-items: baseline;
}
.inspect-outlier-nums {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-variant-numeric: tabular-nums;
}
.outlier-delta.outlier-up { color: hsl(135 55% 32%); }
.outlier-delta.outlier-down { color: hsl(15 75% 38%); }
@media (prefers-color-scheme: dark) {
  .outlier-delta.outlier-up { color: hsl(135 50% 65%); }
  .outlier-delta.outlier-down { color: hsl(15 75% 65%); }
}

.inspect-flip-summary {
  margin: 0;
  font-size: 13px;
}
.inspect-flip-list {
  list-style: none; padding: 0; margin: 4px 0 0;
  display: flex; flex-direction: column; gap: 4px;
}
.inspect-flip-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, max-content);
  gap: 12px;
  align-items: baseline;
  padding: 6px 8px;
  border-radius: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  font-size: 12px;
}
.inspect-flip-pair {
  display: flex; flex-wrap: wrap; gap: 4px; align-items: baseline;
  min-width: 0;
}
.inspect-flip-sides {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.inspect-flip-winners {
  display: flex; gap: 10px; flex-wrap: wrap;
  align-items: baseline;
}
.inspect-flip-winner-cell {
  display: inline-flex;
  gap: 4px;
  align-items: baseline;
}
`;
