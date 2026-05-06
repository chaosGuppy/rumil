import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { PromptDoc, PromptHistory, PromptSection } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";
import { fmtRelative, fmtWhen } from "../../_lib/format";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return { title: name };
}

export default async function PromptDetail({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  const [p, history] = await Promise.all([
    atlasFetch<PromptDoc | null>(
      `/api/atlas/registry/prompts/${encodeURIComponent(name)}`,
      null,
    ),
    atlasFetch<PromptHistory | null>(
      `/api/atlas/registry/prompts/${encodeURIComponent(name)}/history?max_entries=40`,
      null,
    ),
  ]);
  if (!p) notFound();

  const sections = p.sections ?? [];
  const usedInCompositions = p.used_in_compositions ?? [];
  const currentHash = p.content_hash ?? history?.current_content_hash ?? null;
  const currentChars = p.char_count;
  const historyEntries = history?.entries ?? [];

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "prompts", href: "/atlas/prompts" },
              { label: p.name },
            ]}
          />
          <h1>{p.name}</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip is-muted">{p.path}</span>
            {currentHash && (
              <span
                className="atlas-content-hash"
                title={`sha-256 of file contents · ${currentHash}`}
              >
                {currentHash.slice(0, 16)}
              </span>
            )}
            <span className="atlas-chip is-muted">
              {p.char_count.toLocaleString()} chars
            </span>
            <span className="atlas-chip is-muted">
              {sections.length} section{sections.length === 1 ? "" : "s"}
            </span>
            {historyEntries.length > 0 && (
              <a href="#history" className="atlas-chip is-accent">
                {historyEntries.length} commit{historyEntries.length === 1 ? "" : "s"} →
              </a>
            )}
          </div>
        </div>
      </div>

      <div className="atlas-prompt-grid">
        {sections.length > 0 ? (
          <aside className="atlas-toc" aria-label="table of contents">
            <div className="atlas-toc-label">contents</div>
            {sections.map((s) => (
              <a
                key={s.anchor}
                href={`#${s.anchor}`}
                className={s.level >= 3 ? "is-h3" : ""}
              >
                {s.title || "(intro)"}
              </a>
            ))}
          </aside>
        ) : (
          <div />
        )}

        <div>
          {sections.length === 0 ? (
            <div className="atlas-card atlas-markdown" style={{ padding: "1.5rem 1.8rem" }}>
              <Markdown remarkPlugins={[remarkGfm]}>{p.content || ""}</Markdown>
            </div>
          ) : (
            sections.map((s) => <PromptSectionView key={s.anchor} section={s} />)
          )}

          {historyEntries.length > 0 && (
            <div id="history" style={{ marginTop: "2.5rem" }}>
              <h3 style={{ marginBottom: "0.6rem" }}>history</h3>
              <div style={{ fontFamily: "var(--a-mono)", fontSize: "0.7rem", color: "var(--a-muted)", marginBottom: "0.6rem" }}>
                {historyEntries.length} commit{historyEntries.length === 1 ? "" : "s"} touched this file
                {history?.truncated ? " · truncated" : ""}
              </div>
              <div className="atlas-history">
                <div className="atlas-history-row is-head">
                  <span>commit</span>
                  <span>when</span>
                  <span>subject · author</span>
                  <span>content_hash</span>
                  <span style={{ textAlign: "right" }}>Δ chars</span>
                </div>
                {historyEntries.map((e) => {
                  const isCurrent = currentHash != null && e.content_hash === currentHash;
                  const delta =
                    e.char_count != null && currentChars != null
                      ? e.char_count - currentChars
                      : null;
                  const deltaCls =
                    delta == null
                      ? "is-flat"
                      : delta > 0
                        ? "is-positive"
                        : delta < 0
                          ? "is-negative"
                          : "is-flat";
                  return (
                    <div
                      key={e.commit_sha}
                      className={`atlas-history-row ${isCurrent ? "is-current" : ""}`}
                    >
                      <span className="atlas-history-sha" title={e.commit_sha}>
                        {e.commit_short}
                      </span>
                      <span className="atlas-history-when" title={fmtWhen(e.commit_ts)}>
                        {fmtRelative(e.commit_ts)}
                      </span>
                      <span className="atlas-history-subject">
                        {e.subject ?? "(no subject)"}
                        {e.author && (
                          <span className="atlas-history-author">· {e.author}</span>
                        )}
                      </span>
                      <span
                        className="atlas-history-hash"
                        title={`sha-256 · ${e.content_hash}`}
                      >
                        {e.content_hash.slice(0, 16)}
                        {isCurrent && (
                          <span style={{ marginLeft: "0.4rem", color: "var(--a-accent)" }}>
                            ● current
                          </span>
                        )}
                      </span>
                      <span
                        className={`atlas-history-delta ${deltaCls}`}
                        style={{ textAlign: "right" }}
                        title={
                          e.char_count != null
                            ? `${e.char_count.toLocaleString()} chars at this revision`
                            : ""
                        }
                      >
                        {delta == null
                          ? "—"
                          : delta === 0
                            ? "·"
                            : `${delta > 0 ? "+" : "−"}${Math.abs(delta).toLocaleString()}`}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div style={{ marginTop: "2rem" }}>
            <h3 style={{ marginBottom: "0.6rem" }}>raw file</h3>
            <details className="atlas-collapsible">
              <summary>
                <span>raw markdown ({p.char_count.toLocaleString()} chars)</span>
              </summary>
              <div className="atlas-collapsible-body">
                <pre
                  style={{
                    background: "var(--a-bg)",
                    padding: "0.8rem 1rem",
                    overflowX: "auto",
                    fontSize: "0.74rem",
                    lineHeight: 1.5,
                    color: "var(--a-fg-soft)",
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {p.content || ""}
                </pre>
              </div>
            </details>
          </div>
        </div>
      </div>

      <aside style={{ marginTop: "3rem", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "2rem" }}>
        <div className="atlas-aside-block">
          <h3>used in compositions</h3>
          <div className="atlas-chip-row" style={{ marginTop: "0.4rem" }}>
            {usedInCompositions.length === 0 ? (
              <span style={{ color: "var(--a-muted)", fontFamily: "var(--a-mono)", fontSize: "0.78rem" }}>
                not used in any registered composition
              </span>
            ) : (
              usedInCompositions.map((c) => (
                <Link
                  key={c}
                  href={`/atlas/calls/${encodeURIComponent(c)}`}
                  className="atlas-chip is-accent"
                >
                  {c}
                </Link>
              ))
            )}
          </div>
        </div>
        <div className="atlas-aside-block">
          <h3>referenced by</h3>
          <ul className="atlas-aside-list">
            {(p.referenced_by ?? []).length === 0 && (
              <li style={{ color: "var(--a-muted)" }}>none recorded</li>
            )}
            {(p.referenced_by ?? []).map((r) => (
              <li key={r}>
                <Link href={`/atlas/calls/${encodeURIComponent(r)}`}>{r}</Link>
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}

function PromptSectionView({ section }: { section: PromptSection }) {
  const appliesTo = section.applies_to ?? [];
  const note = section.applies_to_note;
  const cls = `atlas-prompt-section ${appliesTo.length > 0 ? "has-applies-to" : ""}`;
  return (
    <section id={section.anchor} className={cls}>
      <div className="atlas-prompt-section-head">
        {section.level === 0 ? (
          <h2 style={{ fontFamily: "var(--a-mono)", fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.14em", color: "var(--a-muted)" }}>
            {section.title || "(intro)"}
          </h2>
        ) : section.level <= 2 ? (
          <h2>{section.title}</h2>
        ) : (
          <h3 style={{ textTransform: "none", letterSpacing: "0", fontFamily: "var(--a-sans)", fontSize: "0.95rem" }}>
            {section.title}
          </h3>
        )}
        {appliesTo.length > 0 && (
          <span className="atlas-applies-chip" title={note ?? undefined}>
            <span className="atlas-applies-chip-label">applies to</span>
            {appliesTo.join(", ")}
          </span>
        )}
        <a
          href={`#${section.anchor}`}
          className="atlas-prompt-section-anchor"
          aria-label="link to section"
        >
          #
        </a>
        <span className="atlas-prompt-section-meta">
          {section.char_count.toLocaleString()} chars
        </span>
      </div>
      {note && <div className="atlas-applies-note">{note}</div>}
      <div className="atlas-markdown">
        <Markdown remarkPlugins={[remarkGfm]}>{section.body || ""}</Markdown>
      </div>
    </section>
  );
}
