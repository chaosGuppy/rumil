import Link from "next/link";
import Markdown from "react-markdown";
import type { PageDetailOut, LinkedPageOut, Page, PageLink, RunSummaryOut } from "@/api";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

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
};

const DIRECTION_CONFIG: Record<string, { color: string; label: string }> = {
  supports: { color: "var(--dir-supports)", label: "supports" },
  opposes: { color: "var(--dir-opposes)", label: "opposes" },
  neutral: { color: "var(--color-muted)", label: "neutral" },
};

async function getPageDetail(pageId: string): Promise<PageDetailOut | null> {
  const res = await fetch(`${API_BASE}/api/pages/${pageId}/detail`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

function pageHref(page: Page): string {
  return `/pages/${page.id}`;
}

async function getPageRun(pageId: string): Promise<RunSummaryOut | null> {
  const res = await fetch(
    `${API_BASE}/api/pages/${pageId}/run`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return res.json();
}

function EpistemicGauge({ value }: { value: number }) {
  const segments = 5;
  return (
    <div className="ep-gauge">
      {Array.from({ length: segments }, (_, i) => {
        const filled = value >= i + 1;
        const partial = !filled && value > i;
        const pct = partial ? (value - i) * 100 : filled ? 100 : 0;
        return (
          <div key={i} className="ep-segment">
            <div className="ep-segment-fill" style={{ width: `${pct}%` }} />
          </div>
        );
      })}
      <span className="ep-value">{value.toFixed(1)}</span>
    </div>
  );
}

function LinkMeta({ link }: { link: PageLink }) {
  const dir = link.direction ? DIRECTION_CONFIG[link.direction] : null;
  return (
    <div className="link-meta">
      <span className="link-type-label">{link.link_type.replace("_", " ")}</span>
      {dir && (
        <span className="link-direction" style={{ color: dir.color }}>
          {dir.label}
        </span>
      )}
      {link.strength > 0 && (
        <span className="link-strength">
          <span className="link-strength-bar">
            <span
              className="link-strength-fill"
              style={{ width: `${(link.strength / 5) * 100}%` }}
            />
          </span>
          {link.strength.toFixed(1)}
        </span>
      )}
    </div>
  );
}

function LinkedCard({ lp }: { lp: LinkedPageOut }) {
  const cfg = TYPE_CONFIG[lp.page.page_type] || TYPE_CONFIG.source;
  return (
    <Link href={pageHref(lp.page)} className="linked-card">
      <div className="linked-card-accent" style={{ background: cfg.accent }} />
      <div className="linked-card-body">
        <LinkMeta link={lp.link} />
        <div className="linked-card-header">
          <span
            className="linked-card-type"
            style={{ color: cfg.accent, background: cfg.bg }}
          >
            {lp.page.page_type}
          </span>
          <span className="linked-card-id">{lp.page.id.slice(0, 8)}</span>
        </div>
        <div className="linked-card-summary">{lp.page.summary}</div>
        {lp.link.reasoning && (
          <div className="linked-card-reasoning">{lp.link.reasoning}</div>
        )}
      </div>
    </Link>
  );
}

function LinkSection({
  title,
  links,
}: {
  title: string;
  links: LinkedPageOut[];
}) {
  if (links.length === 0) return null;
  return (
    <div className="link-section">
      <div className="link-section-header">
        <span className="link-section-title">{title}</span>
        <span className="link-section-count">{links.length}</span>
      </div>
      <div className="link-grid">
        {links.map((lp) => (
          <LinkedCard key={lp.link.id} lp={lp} />
        ))}
      </div>
    </div>
  );
}

export default async function PageDetailPage({
  params,
}: {
  params: Promise<{ pageId: string }>;
}) {
  const { pageId } = await params;
  const [detail, run] = await Promise.all([
    getPageDetail(pageId),
    getPageRun(pageId),
  ]);

  if (!detail) {
    return (
      <main className="page-detail">
        <style>{styles}</style>
        <div className="not-found">
          <span className="not-found-code">404</span>
          <span className="not-found-msg">Page not found</span>
          <Link href="/" className="back-link">
            &larr; Home
          </Link>
        </div>
      </main>
    );
  }

  const { page, links_from, links_to } = detail;
  const cfg = TYPE_CONFIG[page.page_type] || TYPE_CONFIG.source;

  return (
    <main className="page-detail">
      <style>{styles}</style>

      <Link href={`/projects/${page.project_id}`} className="back-link">
        &larr; Workspace
      </Link>

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
            {page.is_superseded && (
              <span className="superseded-tag">superseded</span>
            )}
          </div>
          <h1 className="page-summary">{page.summary}</h1>
        </header>

        <div className="page-content">
          <Markdown>{page.content}</Markdown>
        </div>

        <div className="page-meta-row">
          <div className="meta-block">
            <span className="meta-label">epistemic status</span>
            <EpistemicGauge value={page.epistemic_status} />
          </div>
          {page.epistemic_type && (
            <div className="meta-block">
              <span className="meta-label">uncertainty</span>
              <span className="meta-value">{page.epistemic_type}</span>
            </div>
          )}
          {page.superseded_by && (
            <div className="meta-block">
              <span className="meta-label">superseded by</span>
              <Link
                href={`/pages/${page.superseded_by}`}
                className="meta-link"
              >
                {page.superseded_by.slice(0, 8)}
              </Link>
            </div>
          )}
        </div>
      </article>

      <div className="links-container">
        <LinkSection title="Outgoing" links={links_from} />
        <LinkSection title="Incoming" links={links_to} />
      </div>

      <footer className="page-footer">
        <span>{new Date(page.created_at).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" })}</span>
        <span className="footer-sep" />
        <span>{page.provenance_call_type}</span>
        <span className="footer-sep" />
        <span>{page.provenance_model}</span>
        {run && (
          <>
            <span className="footer-sep" />
            <Link href={`/traces/${run.run_id}`} className="footer-run-link">
              run {run.run_id.slice(0, 8)}
            </Link>
          </>
        )}
      </footer>
    </main>
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

  .page-summary {
    font-size: 1.4rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.3;
    margin: 0;
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
  }
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
  .page-content strong {
    font-weight: 600;
  }
  .page-content a {
    color: var(--color-accent);
    text-decoration: underline;
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
    width: 1rem;
    height: 4px;
    background: var(--color-border);
    overflow: hidden;
  }
  .ep-segment-fill {
    height: 100%;
    background: var(--color-foreground);
    opacity: 0.5;
    transition: width 0.3s ease;
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
    }
  }
`;
