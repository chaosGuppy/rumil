import Link from "next/link";
import { notFound } from "next/navigation";
import type { RunFlow, RunFlowNode } from "@/api";
import { atlasFetch } from "../../../_lib/fetch";
import { Crumbs } from "../../../_components/Crumbs";
import { CrossLink } from "../../../_components/CrossLink";

export const metadata = { title: "run flow" };

function fmtCost(v: number): string {
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(4)}`;
}

function fmtDuration(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

type TreeNode = RunFlowNode & { children: TreeNode[]; depth: number };

function buildTree(nodes: RunFlowNode[]): TreeNode[] {
  const wrapped: TreeNode[] = nodes.map((n) => ({ ...n, children: [], depth: 0 }));
  const byId = new Map(wrapped.map((n) => [n.call_id, n]));
  const roots: TreeNode[] = [];
  for (const n of wrapped) {
    if (n.parent_call_id && byId.has(n.parent_call_id)) {
      byId.get(n.parent_call_id)!.children.push(n);
    } else {
      roots.push(n);
    }
  }
  function setDepth(n: TreeNode, d: number) {
    n.depth = d;
    n.children.forEach((c) => setDepth(c, d + 1));
  }
  roots.forEach((r) => setDepth(r, 0));
  return roots;
}

function flatten(roots: TreeNode[]): TreeNode[] {
  const out: TreeNode[] = [];
  function walk(n: TreeNode) {
    out.push(n);
    n.children.forEach(walk);
  }
  roots.forEach(walk);
  return out;
}

export default async function RunFlowPage({
  params,
}: {
  params: Promise<{ run_id: string }>;
}) {
  const { run_id } = await params;
  const flow = await atlasFetch<RunFlow | null>(
    `/api/atlas/runs/${encodeURIComponent(run_id)}/flow`,
    null,
  );
  if (!flow) notFound();

  const nodes = flow.nodes ?? [];
  const trees = buildTree(nodes);
  const flat = flatten(trees);

  const totalCost = nodes.reduce((a, n) => a + (n.cost_usd ?? 0), 0);
  const totalDispatches = nodes.reduce((a, n) => a + (n.n_dispatches ?? 0), 0);
  const totalPages = nodes.reduce((a, n) => a + (n.pages_loaded ?? 0), 0);

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "runs" },
              { label: run_id.slice(0, 8) },
              { label: "flow" },
            ]}
          />
          <h1>run flow</h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <span className="atlas-chip">{run_id}</span>
            {flow.workflow_name && (
              <Link
                href={`/atlas/workflows/${flow.workflow_name}`}
                className="atlas-chip is-orchestrator"
              >
                {flow.workflow_name}
              </Link>
            )}
            <CrossLink to={`/traces/${run_id}`} chip>
              full trace
            </CrossLink>
          </div>
          <p className="atlas-lede">
            Every call in this run, indented by parent. Each node links to its
            call type&apos;s atlas profile.
          </p>
        </div>
      </div>

      {flow.workflow_name && (
        <div className="atlas-callout">
          See this run overlaid on the workflow diagram —{" "}
          <Link
            href={`/atlas/workflows/${encodeURIComponent(flow.workflow_name)}/runs/${encodeURIComponent(run_id)}`}
          >
            {flow.workflow_name} stage timeline →
          </Link>
        </div>
      )}

      <div className="atlas-stat-grid">
        <div className="atlas-stat">
          <span className="atlas-stat-num">{nodes.length}</span>
          <span className="atlas-stat-label">calls</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{totalDispatches}</span>
          <span className="atlas-stat-label">dispatches</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{totalPages}</span>
          <span className="atlas-stat-label">pages loaded</span>
        </div>
        <div className="atlas-stat">
          <span className="atlas-stat-num">{fmtCost(totalCost)}</span>
          <span className="atlas-stat-label">cost</span>
        </div>
      </div>

      {nodes.length === 0 ? (
        <div className="atlas-empty">
          <strong>no calls</strong>
          this run has no recorded calls.
        </div>
      ) : (
        <div className="atlas-rows" style={{ borderTop: "1px solid var(--a-line)" }}>
          {flat.map((n, i) => (
            <FlowNode key={n.call_id} n={n} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

function FlowNode({ n, index }: { n: TreeNode; index: number }) {
  const indent = "  ".repeat(n.depth);
  const glyph = n.depth === 0 ? "●" : "└─";
  return (
    <div className="atlas-flow-node" style={{ animationDelay: `${Math.min(index * 12, 240)}ms` }}>
      <div className="atlas-flow-tree">
        <span className="atlas-flow-tree-glyph">{indent}{glyph}</span>{" "}
        <Link href={`/atlas/calls/${encodeURIComponent(n.call_type)}`}>
          {n.call_type}
        </Link>
        {n.stage_id && (
          <span style={{ color: "var(--a-muted)", marginLeft: 8, fontSize: "0.7rem" }}>
            {n.stage_id}
          </span>
        )}
      </div>
      <div className="atlas-flow-summary">
        {n.summary ?? n.call_type_description ?? ""}
      </div>
      <div className="atlas-flow-stats">
        <span
          className={`atlas-chip ${
            n.status === "complete"
              ? "is-success"
              : n.status === "error"
                ? "is-warm"
                : "is-muted"
          }`}
          style={{ fontSize: "0.66rem" }}
        >
          {n.status ?? "—"}
        </span>
        <span>{n.pages_loaded ?? 0}p</span>
        <span>{n.n_dispatches ?? 0}d</span>
        {(n.n_llm_exchanges ?? 0) > 0 && (
          <Link
            href={`/atlas/calls/by_id/${encodeURIComponent(n.call_id)}/exchanges`}
            style={{ fontFamily: "var(--a-mono)" }}
            title="exchanges captured for this call"
          >
            {n.n_llm_exchanges}x →
          </Link>
        )}
        <span>{fmtCost(n.cost_usd ?? 0)}</span>
        <span>{fmtDuration(n.duration_seconds)}</span>
      </div>
    </div>
  );
}
