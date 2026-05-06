import Link from "next/link";
import type { RegistryRollup } from "@/api";
import { atlasFetch } from "./_lib/fetch";

export const metadata = { title: "atlas" };

export default async function AtlasIndex() {
  const reg = await atlasFetch<RegistryRollup>("/api/atlas/registry", {
    n_moves: 0,
    n_dispatches: 0,
    n_call_types: 0,
    n_page_types: 0,
    n_workflows: 0,
    n_prompt_files: 0,
    move_summaries: [],
    dispatch_summaries: [],
    call_type_summaries: [],
    page_type_summaries: [],
    workflow_summaries: [],
    presets: {},
    available_calls_presets: [],
  });

  const orchestrators = reg.workflow_summaries.filter(
    (w) => w.kind === "orchestrator",
  );
  const versusFlows = reg.workflow_summaries.filter(
    (w) => w.kind === "versus_workflow",
  );

  return (
    <div>

      <div className="atlas-stat-grid">
        <Link href="/atlas/workflows" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_workflows}</span>
          <span className="atlas-stat-label">workflows</span>
        </Link>
        <Link href="/atlas/calls" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_call_types}</span>
          <span className="atlas-stat-label">call types</span>
        </Link>
        <Link href="/atlas/dispatches" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_dispatches}</span>
          <span className="atlas-stat-label">dispatches</span>
        </Link>
        <Link href="/atlas/moves" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_moves}</span>
          <span className="atlas-stat-label">moves</span>
        </Link>
        <Link href="/atlas/pages" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_page_types}</span>
          <span className="atlas-stat-label">page types</span>
        </Link>
        <Link href="/atlas/prompts" className="atlas-stat atlas-stat-link">
          <span className="atlas-stat-num">{reg.n_prompt_files}</span>
          <span className="atlas-stat-label">prompt files</span>
        </Link>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>workflows</h2>
          <span className="atlas-section-meta">
            the marquee — start here
          </span>
        </div>
        <div className="atlas-grid-cards">
          {orchestrators.map((w) => (
            <article key={w.name} className="atlas-card">
              <div className="atlas-card-head">
                <span className="atlas-card-name">
                  <Link href={`/atlas/workflows/${w.name}`}>{w.name}</Link>
                </span>
                <span className="atlas-chip is-orchestrator">orchestrator</span>
              </div>
              <p className="atlas-card-summary">{w.summary}</p>
              <div className="atlas-chip-row">
                <Link
                  href={`/atlas/workflows/${w.name}`}
                  className="atlas-chip is-accent"
                >
                  stage diagram →
                </Link>
                <Link
                  href={`/atlas/workflows/${w.name}/aggregate`}
                  className="atlas-chip"
                >
                  aggregate behavior →
                </Link>
              </div>
            </article>
          ))}
          {versusFlows.map((w) => (
            <article key={w.name} className="atlas-card">
              <div className="atlas-card-head">
                <span className="atlas-card-name">
                  <Link href={`/atlas/workflows/${w.name}`}>{w.name}</Link>
                </span>
                <span className="atlas-chip is-versus">versus</span>
              </div>
              <p className="atlas-card-summary">{w.summary}</p>
              <div className="atlas-chip-row">
                <Link
                  href={`/atlas/workflows/${w.name}`}
                  className="atlas-chip is-accent"
                >
                  stage diagram →
                </Link>
                <Link
                  href={`/atlas/workflows/${w.name}/aggregate`}
                  className="atlas-chip"
                >
                  aggregate →
                </Link>
              </div>
            </article>
          ))}
        </div>
      </section>

      <div className="atlas-rule-stripe" />

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>traverse</h2>
          <span className="atlas-section-meta">
            workflows fan out to call types · call types compose dispatches and
            moves · moves create pages · all sourced from prompts
          </span>
        </div>
        <div className="atlas-grid-cards">
          <Link href="/atlas/calls" className="atlas-card" style={{ textDecoration: "none", color: "inherit" }}>
            <div className="atlas-card-head">
              <span className="atlas-card-name">call types</span>
              <span className="atlas-chip is-muted">{reg.n_call_types}</span>
            </div>
            <p className="atlas-card-summary">
              The unit of dispatch. Each pairs a context-builder with a
              workspace-updater and a closing-reviewer; some carry a dispatch
              tool that orchestrators use to plan them.
            </p>
          </Link>
          <Link href="/atlas/moves" className="atlas-card" style={{ textDecoration: "none", color: "inherit" }}>
            <div className="atlas-card-head">
              <span className="atlas-card-name">moves</span>
              <span className="atlas-chip is-muted">{reg.n_moves}</span>
            </div>
            <p className="atlas-card-summary">
              The tools made available to LLMs inside a call — create_claim,
              link_consideration, propose_view_item, etc. Each carries a
              payload schema with field-level descriptions.
            </p>
          </Link>
          <Link href="/atlas/pages" className="atlas-card" style={{ textDecoration: "none", color: "inherit" }}>
            <div className="atlas-card-head">
              <span className="atlas-card-name">page types</span>
              <span className="atlas-chip is-muted">{reg.n_page_types}</span>
            </div>
            <p className="atlas-card-summary">
              The typed nodes in the research graph: claims, questions,
              judgements, views, view_items, sources, and the rest.
            </p>
          </Link>
          <Link href="/atlas/prompts" className="atlas-card" style={{ textDecoration: "none", color: "inherit" }}>
            <div className="atlas-card-head">
              <span className="atlas-card-name">prompts</span>
              <span className="atlas-chip is-muted">{reg.n_prompt_files}</span>
            </div>
            <p className="atlas-card-summary">
              The markdown that runs the system. preamble.md is shared; each
              call type and prioritization phase has its own.
            </p>
          </Link>
        </div>
      </section>
    </div>
  );
}
