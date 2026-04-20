import Link from "next/link";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Metadata } from "next";
import type { PageDetailOut, LinkedPageOut, Page, RunSummaryOut } from "@/api";
import LinksContainer from "./links-container";
import StagedBanner from "@/components/staged-banner";

import { API_BASE } from "@/lib/api-base";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { fetchProjectName } from "@/lib/fetch-project-name";
import { truncateHeadline } from "@/lib/page-titles";

const TYPE_CONFIG: Record<
  string,
  { accent: string; bg: string; bgHover: string; border: string }
> = {
  claim: {
    accent: "var(--type-claim)",
    bg: "var(--type-claim-bg)",
    bgHover: "var(--type-claim-bg-hover)",
    border: "var(--type-claim-border)",
  },
  question: {
    accent: "var(--type-question)",
    bg: "var(--type-question-bg)",
    bgHover: "var(--type-question-bg-hover)",
    border: "var(--type-question-border)",
  },
  judgement: {
    accent: "var(--type-judgement)",
    bg: "var(--type-judgement-bg)",
    bgHover: "var(--type-judgement-bg-hover)",
    border: "var(--type-judgement-border)",
  },
  source: {
    accent: "var(--type-source)",
    bg: "var(--type-source-bg)",
    bgHover: "var(--type-source-bg-hover)",
    border: "var(--type-source-border)",
  },
  concept: {
    accent: "var(--type-concept)",
    bg: "var(--type-concept-bg)",
    bgHover: "var(--type-concept-bg-hover)",
    border: "var(--type-concept-border)",
  },
  wiki: {
    accent: "var(--type-wiki)",
    bg: "var(--type-wiki-bg)",
    bgHover: "var(--type-wiki-bg-hover)",
    border: "var(--type-wiki-border)",
  },
  view: {
    accent: "var(--type-view)",
    bg: "var(--type-view-bg)",
    bgHover: "var(--type-view-bg-hover)",
    border: "var(--type-view-border)",
  },
  view_item: {
    accent: "var(--type-view-item)",
    bg: "var(--type-view-item-bg)",
    bgHover: "var(--type-view-item-bg-hover)",
    border: "var(--type-view-item-border)",
  },
};

function stagedQs(stagedRunId?: string): string {
  return stagedRunId ? `?staged_run_id=${stagedRunId}` : "";
}

async function getPageDetail(
  pageId: string,
  stagedRunId?: string,
): Promise<PageDetailOut | null> {
  const res = await fetch(
    `${API_BASE}/api/pages/${pageId}/detail${stagedQs(stagedRunId)}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return res.json();
}

async function getPageHeadline(
  pageId: string,
  stagedRunId?: string,
): Promise<{ headline: string; page_type: string } | null> {
  const res = await fetch(
    `${API_BASE}/api/pages/${pageId}/detail${stagedQs(stagedRunId)}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  const data = (await res.json()) as PageDetailOut;
  return { headline: data.page.headline, page_type: data.page.page_type };
}

function pageHref(page: Page, stagedRunId?: string): string {
  if (stagedRunId) return `/pages/${page.id}?staged_run_id=${stagedRunId}`;
  return `/pages/${page.id}`;
}

const ALL_CITATIONS_RE = /\[([a-f0-9]{8}(?:,\s*[a-f0-9]{8})*)\]/g;

function extractCitedIds(content: string): Set<string> {
  const ids = new Set<string>();
  for (const match of content.matchAll(ALL_CITATIONS_RE)) {
    for (const id of match[1].split(/,\s*/)) {
      ids.add(id);
    }
  }
  return ids;
}

async function buildCitationMap(
  links_from: LinkedPageOut[],
  links_to: LinkedPageOut[],
  content: string,
  stagedRunId?: string,
): Promise<Map<string, { fullId: string; pageType: string }>> {
  const map = new Map<string, { fullId: string; pageType: string }>();
  for (const lp of [...links_from, ...links_to]) {
    const short = lp.page.id.slice(0, 8);
    map.set(short, { fullId: lp.page.id, pageType: lp.page.page_type });
  }
  const cited = extractCitedIds(content);
  const missing = [...cited].filter((id) => !map.has(id));
  if (missing.length > 0) {
    const qs = stagedQs(stagedRunId);
    const results = await Promise.all(
      missing.map(async (shortId) => {
        const res = await fetch(
          `${API_BASE}/api/pages/short/${shortId}${qs}`,
          { cache: "no-store" },
        );
        if (!res.ok) return null;
        const page: Page = await res.json();
        return { shortId, fullId: page.id, pageType: page.page_type };
      }),
    );
    for (const r of results) {
      if (r) map.set(r.shortId, { fullId: r.fullId, pageType: r.pageType });
    }
  }
  return map;
}

const CITATION_RE = /\[([a-f0-9]{8}(?:,\s*[a-f0-9]{8})*)\]/g;

function injectCitationLinks(
  content: string,
  citationMap: Map<string, { fullId: string; pageType: string }>,
  stagedRunId?: string,
): string {
  const orderMap = new Map<string, number>();
  let nextNum = 1;
  const stagedSuffix = stagedRunId ? `&staged_run_id=${stagedRunId}` : "";
  return content.replace(CITATION_RE, (_match, group: string) => {
    const ids = group.split(/,\s*/);
    const parts = ids.map((shortId) => {
      const entry = citationMap.get(shortId);
      if (!entry) return `[${shortId}]`;
      if (!orderMap.has(shortId)) {
        orderMap.set(shortId, nextNum++);
      }
      const num = orderMap.get(shortId)!;
      return `[${num}](/pages/${entry.fullId}?cite=${entry.pageType}${stagedSuffix})`;
    });
    return parts.join(" ");
  });
}

async function getPageRun(pageId: string): Promise<RunSummaryOut | null> {
  const res = await fetch(
    `${API_BASE}/api/pages/${pageId}/run`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return res.json();
}

function titleCase(sec: string): string {
  return sec
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function ViewItemsSection({
  linksFrom,
  sections,
  stagedRunId,
}: {
  linksFrom: LinkedPageOut[];
  sections: string[];
  stagedRunId?: string;
}) {
  const viewItems = linksFrom.filter(
    (lp) => lp.link.link_type === "view_item" && lp.page.page_type === "view_item"
  );
  if (viewItems.length === 0) return null;

  const bySection = new Map<string, LinkedPageOut[]>();
  for (const item of viewItems) {
    const sec = item.link.section || "other";
    const list = bySection.get(sec) ?? [];
    list.push(item);
    bySection.set(sec, list);
  }
  for (const list of bySection.values()) {
    list.sort((a, b) => (a.link.position ?? 0) - (b.link.position ?? 0));
  }

  const orderedSections = sections.length > 0
    ? sections.filter((s) => bySection.has(s))
    : Array.from(bySection.keys());

  return (
    <div className="view-items-section">
      {orderedSections.map((sec) => {
        const items = bySection.get(sec) ?? [];
        return (
          <div key={sec} className="view-section">
            <h3 className="view-section-heading">
              {titleCase(sec)}
            </h3>
            <div className="view-items-list">
              {items.map((lp) => {
                const imp = lp.link.importance;
                const cfg = TYPE_CONFIG.view_item;
                return (
                  <Link
                    key={lp.page.id}
                    href={`/pages/${lp.page.id}${stagedRunId ? `?staged_run_id=${stagedRunId}` : ""}`}
                    className={`view-item-card${imp != null && imp >= 5 ? " view-item-important" : ""}`}
                  >
                    <div
                      className="view-item-accent"
                      style={{ background: cfg.accent }}
                    />
                    <div className="view-item-body">
                      <div className="view-item-scores">
                        {(lp.page.credence != null || lp.page.robustness != null) && (
                          <span className="view-item-score">
                            {[
                              lp.page.credence != null ? `C${lp.page.credence}` : null,
                              lp.page.robustness != null ? `R${lp.page.robustness}` : null,
                            ]
                              .filter(Boolean)
                              .join("/")}
                          </span>
                        )}
                        {imp != null && (
                          <span className={`view-item-importance${imp >= 5 ? " importance-high" : imp >= 4 ? " importance-mid" : ""}`}>
                            I{imp}
                          </span>
                        )}
                      </div>
                      <span className="view-item-headline">
                        {lp.page.headline}
                      </span>
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AssociationBoxes({
  page,
  linksFrom,
  linksTo,
  stagedRunId,
}: {
  page: Page;
  linksFrom: LinkedPageOut[];
  linksTo: LinkedPageOut[];
  stagedRunId?: string;
}) {
  const items: { lp: LinkedPageOut; label: string }[] = [];
  if (page.page_type === "question") {
    for (const lp of linksTo) {
      if (
        lp.link.link_type === "view_of" &&
        lp.page.page_type === "view" &&
        !lp.page.is_superseded
      ) {
        items.push({ lp, label: "view" });
      }
    }
    for (const lp of linksTo) {
      if (
        lp.link.link_type === "answers" &&
        lp.page.page_type === "judgement" &&
        !lp.page.is_superseded
      ) {
        items.push({ lp, label: "judgement" });
      }
    }
  } else if (page.page_type === "judgement") {
    for (const lp of linksFrom) {
      if (
        lp.link.link_type === "answers" &&
        lp.page.page_type === "question"
      ) {
        items.push({ lp, label: "question" });
      }
    }
  } else if (page.page_type === "view") {
    for (const lp of linksFrom) {
      if (
        lp.link.link_type === "view_of" &&
        lp.page.page_type === "question"
      ) {
        items.push({ lp, label: "question" });
      }
    }
  }
  if (items.length === 0) return null;
  return (
    <div className="assoc-boxes">
      <div className="assoc-grid">
        {items.map(({ lp, label }) => {
          const cfg = TYPE_CONFIG[lp.page.page_type] || TYPE_CONFIG.source;
          return (
            <Link
              key={lp.page.id}
              href={pageHref(lp.page, stagedRunId)}
              className="linked-card"
            >
              <div className="linked-card-accent" style={{ background: cfg.accent }} />
              <div className="linked-card-body">
                <div className="link-meta">
                  <span className="link-type-label">{label}</span>
                </div>
                <div className="linked-card-header">
                  <span
                    className="linked-card-type"
                    style={{ color: cfg.accent, background: cfg.bg }}
                  >
                    {lp.page.page_type}
                  </span>
                  <span className="linked-card-id">{lp.page.id.slice(0, 8)}</span>
                </div>
                <div className="linked-card-summary">{lp.page.headline}</div>
                {lp.link.reasoning && (
                  <div className="linked-card-reasoning">{lp.link.reasoning}</div>
                )}
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function SegmentGauge({ value, max, label }: { value: number; max: number; label: string }) {
  return (
    <div className="ep-gauge">
      {Array.from({ length: max }, (_, i) => (
        <div key={i} className={`ep-segment ${i < value ? "ep-segment-active" : ""}`} />
      ))}
      <span className="ep-value">{label}{value}/{max}</span>
    </div>
  );
}

export async function generateMetadata({
  params,
  searchParams,
}: {
  params: Promise<{ pageId: string }>;
  searchParams: Promise<{ staged_run_id?: string; cite?: string }>;
}): Promise<Metadata> {
  const { pageId } = await params;
  const { staged_run_id: stagedRunId } = await searchParams;
  const detail = await getPageDetail(pageId, stagedRunId);
  if (!detail) return { title: "Not found" };
  const { page } = detail;
  const projectName = await fetchProjectName(page.project_id);
  const headline = truncateHeadline(page.headline, 50);
  const mid = headline ? ` "${headline}"` : ` ${pageId.slice(0, 8)}`;
  const suffix = projectName ? ` — ${projectName}` : "";
  return { title: `${page.page_type}${mid}${suffix}` };
}

export default async function PageDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ pageId: string }>;
  searchParams: Promise<{ staged_run_id?: string; cite?: string }>;
}) {
  const { pageId } = await params;
  const { staged_run_id: stagedRunId } = await searchParams;
  const [detail, run] = await Promise.all([
    getPageDetail(pageId, stagedRunId),
    getPageRun(pageId),
  ]);

  if (!detail) {
    return (
      <>
        <style>{styles}</style>
        <main className="page-detail">
          <div className="not-found">
            <span className="not-found-code">404</span>
            <span className="not-found-msg">Page not found</span>
            <Link href="/" className="back-link">
              &larr; Home
            </Link>
          </div>
        </main>
      </>
    );
  }

  const { page, links_from, links_to } = detail;
  const cfg = TYPE_CONFIG[page.page_type] || TYPE_CONFIG.source;
  const [citationMap, supersedingPage, projectName] = await Promise.all([
    buildCitationMap(links_from, links_to, page.content, stagedRunId),
    page.superseded_by ? getPageHeadline(page.superseded_by, stagedRunId) : null,
    fetchProjectName(page.project_id),
  ]);
  const processedContent = injectCitationLinks(page.content, citationMap, stagedRunId);

  return (
    <>
      <style>{styles}</style>
      <main className="page-detail">
        <WorkspaceIndicator projectId={page.project_id} projectName={projectName} />

      {stagedRunId && (
        <StagedBanner runId={stagedRunId} pageUrl={`/pages/${pageId}`} />
      )}

      <article className="page-article">
        <div
          className="page-type-bar"
          style={{ background: cfg.accent }}
        />

        <header className="page-header">
          <div className="page-header-top">
            <span
              className="page-type-badge"
              style={{
                color: cfg.accent,
                background: cfg.bg,
                borderColor: cfg.border,
              }}
            >
              {page.page_type}
            </span>
            <span className="page-id">{page.id.slice(0, 8)}</span>
            {page.is_superseded && page.superseded_by && supersedingPage ? (
              <Link
                href={`/pages/${page.superseded_by}${stagedRunId ? `?staged_run_id=${stagedRunId}` : ""}`}
                className="superseded-link"
                title={supersedingPage.headline}
              >
                <span className="superseded-link-label">superseded by</span>
                <span className="superseded-link-headline">
                  {supersedingPage.headline}
                </span>
                <span className="superseded-link-arrow">{"\u2192"}</span>
              </Link>
            ) : page.is_superseded ? (
              <span className="superseded-tag">superseded</span>
            ) : null}
            {page.page_type === "question" && (
              <Link
                href={`/pages/${pageId}/stats${stagedRunId ? `?staged_run_id=${stagedRunId}` : ""}`}
                className="page-stats-link"
              >
                Stats
              </Link>
            )}
          </div>
          <h1 className="page-summary">{page.headline}</h1>
          <AssociationBoxes page={page} linksFrom={links_from} linksTo={links_to} stagedRunId={stagedRunId} />
          {page.page_type === "source" && typeof page.extra?.url === "string" && (
            <a
              href={page.extra.url}
              target="_blank"
              rel="noopener noreferrer"
              className="source-url"
            >
              <span className="source-url-text">{page.extra.url}</span>
              <span className="source-url-arrow">{"\u2197"}</span>
            </a>
          )}
          {page.abstract && (
            <div className="page-abstract">{page.abstract}</div>
          )}
        </header>

        <div className="page-content">
          <Markdown
            remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
            components={{
              a: ({ href, children }) => {
                if (href && href.startsWith("/pages/") && href.includes("?cite=")) {
                  const url = new URL(href, "http://x");
                  const pageType = url.searchParams.get("cite") || "claim";
                  const typeCfg = TYPE_CONFIG[pageType] || TYPE_CONFIG.source;
                  const stagedParam = url.searchParams.get("staged_run_id");
                  const cleanHref = stagedParam
                    ? `${url.pathname}?staged_run_id=${stagedParam}`
                    : url.pathname;
                  return (
                    <Link href={cleanHref} className="citation-chip" style={{
                      color: typeCfg.accent,
                      borderColor: typeCfg.border,
                      background: typeCfg.bg,
                    }}>
                      {children}
                    </Link>
                  );
                }
                return <a href={href}>{children}</a>;
              },
            }}
          >
            {processedContent}
          </Markdown>
        </div>

        {page.page_type === "view" && (
          <ViewItemsSection
            linksFrom={links_from}
            sections={page.sections ?? []}
            stagedRunId={stagedRunId}
          />
        )}

        {(page.credence != null || page.robustness != null) && (
          <div className="page-meta-row">
            {page.credence != null && (
              <div className="meta-block">
                <span className="meta-label">credence</span>
                <SegmentGauge value={page.credence} max={9} label="C" />
                {page.credence_reasoning && (
                  <p className="meta-reasoning">{page.credence_reasoning}</p>
                )}
              </div>
            )}
            {page.robustness != null && (
              <div className="meta-block">
                <span className="meta-label">robustness</span>
                <SegmentGauge value={page.robustness} max={5} label="R" />
                {page.robustness_reasoning && (
                  <p className="meta-reasoning">{page.robustness_reasoning}</p>
                )}
              </div>
            )}
          </div>
        )}
      </article>

      <LinksContainer links_from={links_from} links_to={links_to} stagedRunId={stagedRunId} />

      <footer className="page-footer">
        <span>{new Date(page.created_at).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" })}</span>
        <span className="footer-sep" />
        <span>{page.provenance_call_type}</span>
        <span className="footer-sep" />
        <span>{page.provenance_model}</span>
        {run && (
          <>
            <span className="footer-sep" />
            <Link
              href={
                run.provenance_call_id
                  ? `/traces/${run.run_id}#call-${run.provenance_call_id.slice(0, 8)}`
                  : `/traces/${run.run_id}`
              }
              className="footer-run-link"
            >
              run {run.run_id.slice(0, 8)}
            </Link>
          </>
        )}
      </footer>
      </main>
    </>
  );
}

const styles = `
  .page-detail {
    max-width: 52rem;
    margin: 0 auto;
    padding: 2rem;
    font-family: var(--font-geist-sans), system-ui, sans-serif;
  }

  .page-detail a.back-link {
    font-size: 0.75rem;
    color: var(--color-muted);
    text-decoration: none;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-family: var(--font-geist-mono), monospace;
  }
  .page-detail a.back-link:hover {
    color: var(--color-foreground);
  }

  .not-found {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    padding: 6rem 1rem;
  }
  .not-found-code {
    font-size: 2.5rem;
    font-weight: 700;
    color: var(--color-muted);
    font-family: var(--font-geist-mono), monospace;
    opacity: 0.3;
  }
  .not-found-msg {
    font-size: 0.9rem;
    color: var(--color-muted);
  }

  .page-article {
    margin-top: 1.5rem;
    border: 1px solid var(--color-border);
    position: relative;
    overflow: hidden;
  }

  .page-type-bar {
    height: 3px;
    width: 100%;
  }

  .page-header {
    padding: 1.25rem 1.5rem 0;
  }
  .page-header-top {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }
  .page-stats-link {
    margin-left: auto;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--color-muted);
    text-decoration: none;
    border: 1px solid var(--color-border);
    padding: 0.3rem 0.6rem;
    transition: all 0.12s ease;
  }
  .page-stats-link:hover {
    color: var(--color-foreground);
    border-color: var(--color-accent);
  }
  .page-type-badge {
    font-size: 0.7rem;
    font-weight: 600;
    font-family: var(--font-geist-mono), monospace;
    padding: 0.2rem 0.5rem;
    border: 1px solid;
    letter-spacing: 0.02em;
  }
  .page-id {
    font-size: 0.7rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    opacity: 0.7;
  }
  .superseded-tag {
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--dir-opposes);
    text-decoration: line-through;
    opacity: 0.8;
  }
  .superseded-link {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    background: color-mix(in srgb, var(--dir-opposes) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--dir-opposes) 20%, transparent);
    text-decoration: none;
    max-width: 340px;
    transition: background 0.15s, border-color 0.15s;
  }
  .superseded-link:hover {
    background: color-mix(in srgb, var(--dir-opposes) 14%, transparent);
    border-color: color-mix(in srgb, var(--dir-opposes) 35%, transparent);
  }
  .superseded-link-label {
    font-size: 0.6rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--dir-opposes);
    opacity: 0.7;
    flex-shrink: 0;
  }
  .superseded-link-headline {
    font-size: 0.65rem;
    color: var(--color-fg);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    opacity: 0.85;
  }
  .superseded-link-arrow {
    font-size: 0.6rem;
    color: var(--dir-opposes);
    opacity: 0.6;
    flex-shrink: 0;
  }

  .page-summary {
    font-size: 1.4rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.3;
    margin: 0;
  }

  .source-url {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    margin-top: 0.5rem;
    max-width: 100%;
    font-size: 0.72rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    text-decoration: none;
    transition: color 0.15s;
  }
  .source-url:hover {
    color: var(--color-foreground);
  }
  .source-url-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .source-url-arrow {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    font-size: 0.8em;
    opacity: 0.5;
    transition: opacity 0.15s, transform 0.15s;
  }
  .source-url:hover .source-url-arrow {
    opacity: 1;
    transform: translate(1px, -1px);
  }

  .assoc-boxes {
    margin-top: 0.75rem;
  }
  .assoc-grid {
    display: grid;
    gap: 1px;
    background: var(--color-border);
    border: 1px solid var(--color-border);
    overflow: hidden;
  }

  .page-abstract {
    max-width: 36rem;
    margin: 0.75rem auto 0;
    padding: 0 1.5rem;
    font-size: 0.82rem;
    font-style: italic;
    line-height: 1.55;
    color: var(--color-foreground);
    opacity: 0.65;
  }

  .page-content {
    padding: 1rem 1.5rem 1.25rem;
    font-size: 0.9rem;
    line-height: 1.65;
    color: var(--color-foreground);
    opacity: 0.85;
  }

  .page-content p {
    margin: 0 0 0.75em;
  }
  .page-content p:last-child {
    margin-bottom: 0;
  }
  .page-content ul, .page-content ol {
    margin: 0 0 0.75em;
    padding-left: 1.5em;
  }
  .page-content li {
    margin-bottom: 0.25em;
  }
  .page-content h1, .page-content h2, .page-content h3,
  .page-content h4, .page-content h5, .page-content h6 {
    margin: 1em 0 0.5em;
    line-height: 1.3;
    font-weight: 600;
  }
  .page-content h1 { font-size: 1.4em; }
  .page-content h2 { font-size: 1.2em; }
  .page-content h3 { font-size: 1.05em; }
  .page-content h4 { font-size: 1em; }
  .page-content h1:first-child, .page-content h2:first-child,
  .page-content h3:first-child {
    margin-top: 0;
  }
  .page-content code {
    font-size: 0.85em;
    background: var(--color-surface);
    padding: 0.15em 0.35em;
    border-radius: 3px;
  }
  .page-content pre {
    background: var(--color-surface);
    padding: 0.75em 1em;
    border-radius: 4px;
    overflow-x: auto;
    margin: 0 0 0.75em;
  }
  .page-content pre code {
    background: none;
    padding: 0;
  }
  .page-content blockquote {
    border-left: 3px solid var(--color-border);
    margin: 0 0 0.75em;
    padding: 0.25em 0 0.25em 1em;
    opacity: 0.85;
  }
  .page-content table {
    width: 100%;
    border-collapse: collapse;
    margin: 0 0 0.75em;
    font-size: 0.92em;
  }
  .page-content th, .page-content td {
    border: 1px solid var(--color-border);
    padding: 0.4em 0.65em;
    text-align: left;
  }
  .page-content th {
    font-weight: 600;
    background: var(--color-surface);
  }
  .page-content tr:nth-child(even) td {
    background: color-mix(in srgb, var(--color-surface) 40%, transparent);
  }
  .page-content strong {
    font-weight: 600;
  }
  .page-content a {
    color: var(--color-accent);
    text-decoration: underline;
  }
  .page-content .citation-chip {
    display: inline;
    font-size: 0.78em;
    font-family: var(--font-geist-mono), monospace;
    font-weight: 500;
    padding: 0.1em 0.4em;
    border: 1px solid;
    text-decoration: none;
    letter-spacing: 0.01em;
    transition: background 0.12s ease, border-color 0.12s ease;
    cursor: pointer;
    vertical-align: baseline;
  }
  .page-content .citation-chip:hover {
    filter: brightness(0.92);
    text-decoration: none;
  }
  @media (prefers-color-scheme: dark) {
    .page-content .citation-chip:hover {
      filter: brightness(1.3);
    }
  }

  .page-meta-row {
    display: flex;
    gap: 1.5rem;
    padding: 0.85rem 1.5rem;
    border-top: 1px solid var(--color-border);
    background: var(--color-surface);
    flex-wrap: wrap;
  }
  .meta-block {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    max-width: 28rem;
  }
  .meta-reasoning {
    margin: 0.15rem 0 0;
    font-size: 0.78rem;
    line-height: 1.45;
    color: var(--color-muted);
    font-style: italic;
  }
  .meta-label {
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .meta-value {
    font-size: 0.8rem;
    color: var(--color-foreground);
    opacity: 0.8;
  }
  .meta-link {
    font-size: 0.8rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-accent);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color 0.15s;
  }
  .meta-link:hover {
    border-color: var(--color-accent);
  }

  .ep-gauge {
    display: flex;
    align-items: center;
    gap: 2px;
  }
  .ep-segment {
    width: 0.75rem;
    height: 4px;
    background: var(--color-border);
  }
  .ep-segment-active {
    background: var(--color-foreground);
    opacity: 0.5;
  }
  .ep-value {
    font-size: 0.75rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    margin-left: 0.35rem;
  }

  .links-container {
    margin-top: 2rem;
    display: flex;
    flex-direction: column;
    gap: 2rem;
  }

  .links-filter-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .links-superseded-toggle {
    font-size: 0.7rem;
    font-family: var(--font-geist-mono), monospace;
    font-weight: 500;
    letter-spacing: 0.02em;
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--color-border);
    background: transparent;
    color: var(--color-muted);
    cursor: pointer;
    opacity: 0.5;
    transition: opacity 0.15s, background 0.15s, color 0.15s, border-color 0.15s;
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  .links-superseded-toggle:hover {
    opacity: 0.75;
  }
  .links-superseded-toggle.active {
    background: var(--type-judgement-bg);
    color: var(--type-judgement);
    border-color: var(--type-judgement-border);
    opacity: 1;
  }
  .links-superseded-count {
    font-size: 0.65rem;
    opacity: 0.7;
  }

  .linked-card-superseded {
    opacity: 0.4;
  }
  .linked-card-superseded:hover {
    opacity: 0.7;
  }
  .linked-card-superseded-badge {
    font-size: 0.6rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-dim);
    text-decoration: line-through;
    opacity: 0.6;
  }

  .link-section-header {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }
  .link-section-title {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
  }
  .link-section-count {
    font-size: 0.7rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    opacity: 0.5;
  }

  .link-grid {
    display: grid;
    gap: 1px;
    background: var(--color-border);
    border: 1px solid var(--color-border);
    overflow: hidden;
  }

  .linked-card {
    display: flex;
    background: var(--color-background);
    text-decoration: none;
    color: inherit;
    transition: background 0.1s ease;
  }
  .linked-card:hover {
    background: var(--color-surface);
  }
  .linked-card-accent {
    width: 3px;
    flex-shrink: 0;
  }
  .linked-card-body {
    padding: 0.6rem 0.75rem;
    min-width: 0;
    flex: 1;
  }

  .link-meta {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .link-type-label {
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .link-direction {
    font-size: 0.65rem;
    font-weight: 600;
    font-family: var(--font-geist-mono), monospace;
    letter-spacing: 0.02em;
  }
  .link-strength {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
  }
  .link-strength-bar {
    display: inline-block;
    width: 2rem;
    height: 2px;
    background: var(--color-border);
    overflow: hidden;
  }
  .link-strength-fill {
    display: block;
    height: 100%;
    background: var(--color-muted);
  }

  .linked-card-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.2rem;
  }
  .linked-card-type {
    font-size: 0.65rem;
    font-weight: 600;
    font-family: var(--font-geist-mono), monospace;
    padding: 0.1rem 0.35rem;
    letter-spacing: 0.02em;
  }
  .linked-card-id {
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    opacity: 0.6;
  }
  .linked-card-summary {
    font-size: 0.82rem;
    font-weight: 500;
    line-height: 1.35;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .linked-card-reasoning {
    font-size: 0.72rem;
    line-height: 1.4;
    color: var(--color-muted);
    margin-top: 0.25rem;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  .page-footer {
    margin-top: 2.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--color-border);
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.7rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
    opacity: 0.6;
  }
  .footer-sep {
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: var(--color-muted);
    opacity: 0.4;
  }
  .footer-run-link {
    color: var(--color-accent);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color 0.15s;
  }
  .footer-run-link:hover {
    border-color: var(--color-accent);
  }

  :root {
    --color-foreground: #111;
    --color-muted: #888;
    --color-dim: #aaa;
    --color-border: #ddd;
    --color-surface: #f6f6f6;
    --color-background: #fff;
    --color-accent: #444;

    --dir-supports: #3a7d44;
    --dir-opposes: #b04040;

    --type-claim: #4a6d9c;
    --type-claim-bg: #f3f5f8;
    --type-claim-bg-hover: #eaeff4;
    --type-claim-border: #b0bfcf;

    --type-question: #6b5b8a;
    --type-question-bg: #f4f3f7;
    --type-question-bg-hover: #edebf2;
    --type-question-border: #b5aec5;

    --type-judgement: #8c7040;
    --type-judgement-bg: #f7f5f0;
    --type-judgement-bg-hover: #f0ece2;
    --type-judgement-border: #c4b48a;

    --type-source: #6b7280;
    --type-source-bg: #f4f5f6;
    --type-source-bg-hover: #ededef;
    --type-source-border: #c0c3c8;

    --type-concept: #4a7f78;
    --type-concept-bg: #f2f6f5;
    --type-concept-bg-hover: #e8efed;
    --type-concept-border: #a3bdb8;

    --type-wiki: #4e7f56;
    --type-wiki-bg: #f3f6f3;
    --type-wiki-bg-hover: #e9f0ea;
    --type-wiki-border: #a5c0a9;

    --type-view: #7a5a8c;
    --type-view-bg: #f6f3f8;
    --type-view-bg-hover: #eee9f2;
    --type-view-border: #b8a5c5;

    --type-view-item: #8c6a9e;
    --type-view-item-bg: #f7f4f9;
    --type-view-item-bg-hover: #f0eaf4;
    --type-view-item-border: #c0aece;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --color-foreground: #ededed;
      --color-muted: #6a6a6a;
      --color-dim: #4a4a4a;
      --color-border: #222;
      --color-surface: #111;
      --color-background: #0a0a0a;
      --color-accent: #888;

      --dir-supports: #5aaa66;
      --dir-opposes: #d06060;

      --type-claim: #7a9abb;
      --type-claim-bg: #0e1318;
      --type-claim-bg-hover: #131a22;
      --type-claim-border: #1e2a38;

      --type-question: #9388ad;
      --type-question-bg: #11101a;
      --type-question-bg-hover: #181524;
      --type-question-border: #242038;

      --type-judgement: #b8a46a;
      --type-judgement-bg: #141108;
      --type-judgement-bg-hover: #1c180f;
      --type-judgement-border: #2e2714;

      --type-source: #8a8f96;
      --type-source-bg: #101112;
      --type-source-bg-hover: #171819;
      --type-source-border: #242628;

      --type-concept: #6aaa9f;
      --type-concept-bg: #0b1413;
      --type-concept-bg-hover: #111d1b;
      --type-concept-border: #1a2e2a;

      --type-wiki: #6fa877;
      --type-wiki-bg: #0b140d;
      --type-wiki-bg-hover: #111d14;
      --type-wiki-border: #1a2e1f;

      --type-view: #a07ab8;
      --type-view-bg: #140e18;
      --type-view-bg-hover: #1c1424;
      --type-view-border: #2e2038;

      --type-view-item: #b08ac8;
      --type-view-item-bg: #150f1a;
      --type-view-item-bg-hover: #1e1526;
      --type-view-item-border: #32243e;
    }
  }

  .view-items-section {
    margin-top: 2rem;
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }
  .view-section-heading {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--color-muted);
    margin: 0 0 0.5rem 0;
    font-family: var(--font-geist-mono), monospace;
  }
  .view-items-list {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .view-item-card {
    display: flex;
    align-items: stretch;
    border: 1px solid var(--type-view-item-border);
    border-radius: 6px;
    background: var(--type-view-item-bg);
    text-decoration: none;
    color: inherit;
    transition: background 0.12s ease, border-color 0.12s ease;
    overflow: hidden;
  }
  .view-item-card:hover {
    background: var(--type-view-item-bg-hover);
  }
  .view-item-important {
    border-color: var(--type-view-item);
  }
  .view-item-accent {
    width: 3px;
    flex-shrink: 0;
  }
  .view-item-body {
    padding: 0.5rem 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }
  .view-item-scores {
    display: flex;
    gap: 0.5rem;
    font-size: 0.65rem;
    font-family: var(--font-geist-mono), monospace;
    color: var(--color-muted);
  }
  .view-item-importance {
    font-weight: 500;
  }
  .view-item-importance.importance-high {
    color: var(--type-view);
    font-weight: 700;
  }
  .view-item-importance.importance-mid {
    color: var(--type-view-item);
    font-weight: 600;
  }
  .view-item-headline {
    font-size: 0.85rem;
    line-height: 1.35;
  }
`;
