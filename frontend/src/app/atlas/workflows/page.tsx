import Link from "next/link";
import type { WorkflowGraph, WorkflowSummary } from "@/api";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { RecurseGraph, type GraphNode } from "../_components/RecurseGraph";

export const metadata = { title: "workflows" };

export default async function WorkflowsList() {
  const [workflows, graph] = await Promise.all([
    atlasFetch<WorkflowSummary[]>("/api/atlas/workflows", []),
    atlasFetch<WorkflowGraph | null>("/api/atlas/workflows/graph", null),
  ]);

  const orchestrators = workflows.filter((w) => w.kind === "orchestrator");
  const versusFlows = workflows.filter((w) => w.kind === "versus_workflow");

  const graphNodes: GraphNode[] = (graph?.nodes ?? []).map((n) => ({
    id: n.id,
    label: n.label,
    kind: n.kind,
    href: `/atlas/workflows/${encodeURIComponent(n.id)}`,
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "workflows" }]} />
          <h1 className="is-sans">workflows</h1>
          <p className="atlas-lede">
            Orchestrators and workflow templates that drive multi-call
            investigation. Each defines its own stage diagram and dispatch
            grammar; aggregate views show how each one actually behaves on real
            runs.
          </p>
        </div>
      </div>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>orchestrators</h2>
          <span className="atlas-section-meta">
            {orchestrators.length} workflows · drive rumil research
          </span>
        </div>
        <div className="atlas-grid-cards">
          {orchestrators.map((w) => (
            <WorkflowCard
              key={w.name}
              w={w}
              kind="is-orchestrator"
              recurses={recurseSummary(graph, w.name)}
            />
          ))}
        </div>
      </section>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>versus workflows</h2>
          <span className="atlas-section-meta">
            {versusFlows.length} workflows · pairwise essay-completion eval
          </span>
        </div>
        <div className="atlas-grid-cards">
          {versusFlows.map((w) => (
            <WorkflowCard
              key={w.name}
              w={w}
              kind="is-versus"
              recurses={recurseSummary(graph, w.name)}
            />
          ))}
        </div>
      </section>

      {graph && graphNodes.length > 0 && (
        <details className="atlas-section" style={{ marginTop: "2rem" }}>
          <summary
            className="atlas-section-head"
            style={{ cursor: "pointer", listStyle: "revert" }}
          >
            <h2 style={{ display: "inline-block", margin: 0 }}>recurse graph</h2>
            <span className="atlas-section-meta" style={{ marginLeft: "0.6rem" }}>
              full graph · click to expand
            </span>
          </summary>
          <div style={{ marginTop: "0.85rem" }}>
            <RecurseGraph
              nodes={graphNodes}
              edges={graph.edges ?? []}
              groupOrder={["orchestrator", "versus_workflow"]}
              groupLabels={{ orchestrator: "orchestrators", versus_workflow: "versus" }}
              height={300}
            />
          </div>
        </details>
      )}
    </div>
  );
}

function recurseSummary(
  graph: WorkflowGraph | null,
  name: string,
): { selfLoop: boolean; targets: string[] } {
  if (!graph) return { selfLoop: false, targets: [] };
  const targets = new Set<string>();
  let selfLoop = false;
  for (const e of graph.edges ?? []) {
    if (e.from_id !== name) continue;
    if (e.to_id === name) {
      selfLoop = true;
    } else {
      targets.add(e.to_id);
    }
  }
  return { selfLoop, targets: [...targets].sort() };
}

function WorkflowCard({
  w,
  kind,
  recurses,
}: {
  w: WorkflowSummary;
  kind: "is-orchestrator" | "is-versus";
  recurses?: { selfLoop: boolean; targets: string[] };
}) {
  return (
    <article className="atlas-card">
      <div className="atlas-card-head">
        <span className="atlas-card-name">
          <Link href={`/atlas/workflows/${w.name}`}>{w.name}</Link>
        </span>
        <span className={`atlas-chip ${kind}`}>{w.kind.replace("_", " ")}</span>
      </div>
      <p className="atlas-card-summary">{w.summary}</p>
      {recurses && (recurses.selfLoop || recurses.targets.length > 0) && (
        <div
          className="atlas-card-summary"
          style={{ fontSize: "0.7rem", color: "var(--a-muted)", marginTop: "-0.25rem" }}
        >
          recurses{" "}
          {recurses.selfLoop && (
            <span style={{ color: "var(--a-accent)" }}>↻ self</span>
          )}
          {recurses.selfLoop && recurses.targets.length > 0 && " · "}
          {recurses.targets.length > 0 && (
            <>
              →{" "}
              {recurses.targets.map((t, i) => (
                <span key={t}>
                  <Link href={`/atlas/workflows/${t}`}>{t}</Link>
                  {i < recurses.targets.length - 1 && ", "}
                </span>
              ))}
            </>
          )}
        </div>
      )}
      <div className="atlas-chip-row">
        <Link href={`/atlas/workflows/${w.name}`} className="atlas-chip is-accent">
          stages →
        </Link>
        <Link href={`/atlas/workflows/${w.name}/aggregate`} className="atlas-chip">
          aggregate →
        </Link>
      </div>
    </article>
  );
}
