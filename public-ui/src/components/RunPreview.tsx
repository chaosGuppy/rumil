"use client";

interface PreviewNode {
  id: string;
  full_id?: string;
  headline: string;
  node_type: string;
  importance: number;
  credence?: number | null;
  robustness?: number | null;
  depth?: number;
  layer?: string;
  reason?: string;
}

interface RunPreviewData {
  scope_node: PreviewNode;
  root_node: PreviewNode;
  context_nodes: PreviewNode[];
  filtered_nodes: PreviewNode[];
  sibling_nodes: PreviewNode[];
  health: {
    total: number;
    claims: number;
    hypotheses: number;
    evidence: number;
    uncertainties: number;
    questions: number;
    max_depth: number;
    leafs_without_content: number;
    no_credence: number;
  };
  run_type: string;
  config: {
    max_rounds: number;
    temperature: number;
    dry_run: boolean;
  };
  tools_available: string[];
  error?: string;
}

interface RunPreviewProps {
  data: RunPreviewData;
  onAction?: (message: string) => void;
  onNodeRef?: (id: string) => void;
}

function NodeDot({ type }: { type: string }) {
  return (
    <span
      className="rp-dot"
      style={{ background: `var(--node-${type}, var(--fg-dim))` }}
    />
  );
}

function ScorePill({ credence, robustness }: { credence?: number | null; robustness?: number | null }) {
  if (!credence && !robustness) return null;
  return (
    <span className="rp-score">
      {credence != null && `C${credence}`}
      {credence != null && robustness != null && "/"}
      {robustness != null && `R${robustness}`}
    </span>
  );
}

function HealthStat({ label, value, warn }: { label: string; value: number; warn?: boolean }) {
  return (
    <span className={`rp-health-stat ${warn ? "rp-health-warn" : ""}`}>
      <span className="rp-health-val">{value}</span>
      <span className="rp-health-label">{label}</span>
    </span>
  );
}

export function RunPreview({ data, onAction, onNodeRef }: RunPreviewProps) {
  if (data.error) {
    return <div className="rp-container rp-error">{data.error}</div>;
  }

  const scopeId = data.scope_node.id;
  const runType = data.run_type;

  // Build tree: scope node's children at depth > 0
  const branchNodes = data.context_nodes.filter((n) => n.depth != null && n.depth > 0);
  // Combine branch + filtered for display, sorted by depth
  const allBranch = [
    ...branchNodes.map((n) => ({ ...n, inContext: true })),
    ...data.filtered_nodes.map((n) => ({ ...n, inContext: false, depth: n.depth ?? 1 })),
  ].sort((a, b) => (a.depth ?? 0) - (b.depth ?? 0));

  return (
    <div className="rp-container">
      {/* Header */}
      <div className="rp-header">
        <span className="rp-header-label">context preview</span>
        <span className="rp-run-type" data-type={runType}>{runType}</span>
      </div>

      {/* Root */}
      <div className="rp-root">
        <span className="rp-root-label">root</span>
        <span className="rp-root-headline">{data.root_node.headline}</span>
      </div>

      {/* Scope branch */}
      <div className="rp-branch">
        {/* Scope node itself */}
        <div className="rp-node rp-node-scope">
          <NodeDot type={data.scope_node.node_type} />
          <button
            className="rp-node-headline"
            onClick={() => onNodeRef?.(scopeId)}
          >
            {data.scope_node.headline}
          </button>
          <span className="rp-level">L{data.scope_node.importance}</span>
          <ScorePill credence={data.scope_node.credence} robustness={data.scope_node.robustness} />
          <span className="rp-scope-tag">scope</span>
        </div>

        {/* Branch children */}
        {allBranch.map((node) => (
          <div
            key={node.id}
            className={`rp-node ${node.inContext ? "" : "rp-node-filtered"}`}
            style={{ paddingLeft: `${(node.depth ?? 1) * 14 + 4}px` }}
          >
            <NodeDot type={node.node_type} />
            <button
              className="rp-node-headline"
              onClick={() => onNodeRef?.(node.id)}
            >
              {node.headline}
            </button>
            <span className="rp-level">L{node.importance}</span>
            <ScorePill credence={node.credence} robustness={node.robustness} />
            {!node.inContext && <span className="rp-filtered-tag">filtered</span>}
          </div>
        ))}
      </div>

      {/* Siblings */}
      {data.sibling_nodes.length > 0 && (
        <div className="rp-siblings">
          <span className="rp-section-label">sibling branches</span>
          {data.sibling_nodes.map((s) => (
            <div key={s.id} className="rp-sibling">
              <NodeDot type={s.node_type} />
              <button
                className="rp-node-headline"
                onClick={() => onNodeRef?.(s.id)}
              >
                {s.headline}
              </button>
              <span className="rp-level">L{s.importance}</span>
            </div>
          ))}
        </div>
      )}

      {/* Health */}
      <div className="rp-health">
        <HealthStat label="nodes" value={data.health.total} />
        <HealthStat label="claims" value={data.health.claims} />
        <HealthStat label="evidence" value={data.health.evidence} />
        <HealthStat label="no cred." value={data.health.no_credence} warn={data.health.no_credence > 0} />
        <HealthStat label="empty" value={data.health.leafs_without_content} warn={data.health.leafs_without_content > 0} />
      </div>

      {/* Config + tools */}
      <div className="rp-config">
        <span>{data.config.max_rounds} rounds</span>
        <span className="rp-config-sep">·</span>
        <span>{data.tools_available.length} tools</span>
        <span className="rp-config-sep">·</span>
        <span>temp {data.config.temperature}</span>
      </div>

      {/* Actions */}
      <div className="rp-actions">
        <button
          className="rp-action rp-action-dry"
          onClick={() => onAction?.(`run the orchestrator on ${scopeId} as ${runType} with dry_run`)}
        >
          Run dry
        </button>
        <button
          className="rp-action rp-action-live"
          onClick={() => onAction?.(`run the orchestrator on ${scopeId} as ${runType}, live (not dry run)`)}
        >
          Run
        </button>
      </div>
    </div>
  );
}
