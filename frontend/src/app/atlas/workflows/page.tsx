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

      {graph && graphNodes.length > 0 && (
        <section className="atlas-section">
          <div className="atlas-section-head">
            <h2>recurse graph</h2>
            <span className="atlas-section-meta">
              who can dispatch into whom · self-loops shown as dashed flag arcs
            </span>
          </div>
          <RecurseGraph
            nodes={graphNodes}
            edges={graph.edges ?? []}
            groupOrder={["orchestrator", "versus_workflow"]}
            groupLabels={{ orchestrator: "orchestrators", versus_workflow: "versus" }}
            height={300}
          />
        </section>
      )}

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>orchestrators</h2>
          <span className="atlas-section-meta">
            {orchestrators.length} workflows · drive rumil research
          </span>
        </div>
        <div className="atlas-grid-cards">
          {orchestrators.map((w) => (
            <WorkflowCard key={w.name} w={w} kind="is-orchestrator" />
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
            <WorkflowCard key={w.name} w={w} kind="is-versus" />
          ))}
        </div>
      </section>
    </div>
  );
}

function WorkflowCard({
  w,
  kind,
}: {
  w: WorkflowSummary;
  kind: "is-orchestrator" | "is-versus";
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
