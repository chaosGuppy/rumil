import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { PromptDoc } from "@/api";
import { atlasFetch } from "../../_lib/fetch";
import { Crumbs } from "../../_components/Crumbs";

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
  const p = await atlasFetch<PromptDoc | null>(
    `/api/atlas/registry/prompts/${encodeURIComponent(name)}`,
    null,
  );
  if (!p) notFound();

  const sections = p.sections ?? [];
  const usedInCompositions = p.used_in_compositions ?? [];

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
            <span className="atlas-chip is-muted">
              {p.char_count.toLocaleString()} chars
            </span>
            <span className="atlas-chip is-muted">
              {sections.length} section{sections.length === 1 ? "" : "s"}
            </span>
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
            sections.map((s) => (
              <section
                key={s.anchor}
                id={s.anchor}
                className="atlas-prompt-section"
              >
                <div className="atlas-prompt-section-head">
                  {s.level === 0 ? (
                    <h2 style={{ fontFamily: "var(--a-mono)", fontSize: "0.78rem", textTransform: "uppercase", letterSpacing: "0.14em", color: "var(--a-muted)" }}>
                      {s.title || "(intro)"}
                    </h2>
                  ) : s.level <= 2 ? (
                    <h2>{s.title}</h2>
                  ) : (
                    <h3 style={{ textTransform: "none", letterSpacing: "0", fontFamily: "var(--a-sans)", fontSize: "0.95rem" }}>
                      {s.title}
                    </h3>
                  )}
                  <a
                    href={`#${s.anchor}`}
                    className="atlas-prompt-section-anchor"
                    aria-label="link to section"
                  >
                    #
                  </a>
                  <span className="atlas-prompt-section-meta">
                    {s.char_count.toLocaleString()} chars
                  </span>
                </div>
                <div className="atlas-markdown">
                  <Markdown remarkPlugins={[remarkGfm]}>{s.body || ""}</Markdown>
                </div>
              </section>
            ))
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
