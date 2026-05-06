export type GraphNode = {
  id: string;
  label: string;
  kind: string;
  href?: string;
};

export type GraphEdge = {
  from_id: string;
  to_id: string;
  via_stage?: string | null;
};

type Layout = {
  width: number;
  height: number;
  nodes: Map<string, { x: number; y: number; w: number; h: number; node: GraphNode }>;
};

const NODE_W = 150;
const NODE_H = 32;
const COL_GAP = 280;
const ROW_GAP = 50;
const PAD_X = 60;
const PAD_TOP = 36;
const PAD_BOTTOM = 16;

function layoutColumns(
  nodes: GraphNode[],
  groupKey: (n: GraphNode) => string,
): Layout {
  const groups = new Map<string, GraphNode[]>();
  for (const n of nodes) {
    const k = groupKey(n);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(n);
  }
  const groupKeys = Array.from(groups.keys());
  const positioned = new Map<string, { x: number; y: number; w: number; h: number; node: GraphNode }>();
  let maxRows = 0;
  groupKeys.forEach((k, ci) => {
    const list = groups.get(k)!;
    if (list.length > maxRows) maxRows = list.length;
    list.forEach((n, ri) => {
      positioned.set(n.id, {
        x: PAD_X + ci * COL_GAP,
        y: PAD_TOP + ri * ROW_GAP,
        w: NODE_W,
        h: NODE_H,
        node: n,
      });
    });
  });
  const width = PAD_X * 2 + Math.max(0, groupKeys.length - 1) * COL_GAP + NODE_W;
  const height = PAD_TOP + PAD_BOTTOM + maxRows * ROW_GAP;
  return { width, height, nodes: positioned };
}

function edgePath(
  fromBox: { x: number; y: number; w: number; h: number },
  toBox: { x: number; y: number; w: number; h: number },
  selfIdx = 0,
): { d: string; isSelf: boolean } {
  const isSelf = fromBox === toBox;
  if (isSelf) {
    const cy = fromBox.y + fromBox.h / 2;
    const startX = fromBox.x + fromBox.w;
    const startY = cy - 4;
    const endX = startX;
    const endY = cy + 4;
    const loopOut = 28 + selfIdx * 8;
    const d = `M ${startX} ${startY} C ${startX + loopOut} ${startY - 14}, ${startX + loopOut} ${endY + 14}, ${endX} ${endY}`;
    return { d, isSelf: true };
  }
  const sx = fromBox.x + fromBox.w;
  const sy = fromBox.y + fromBox.h / 2;
  const tx = toBox.x;
  const ty = toBox.y + toBox.h / 2;
  const midX = (sx + tx) / 2;
  const d = `M ${sx} ${sy} C ${midX} ${sy}, ${midX} ${ty}, ${tx} ${ty}`;
  return { d, isSelf: false };
}

function endpointForArrow(
  fromBox: { x: number; y: number; w: number; h: number },
  toBox: { x: number; y: number; w: number; h: number },
): { x: number; y: number; angle: number } {
  if (fromBox === toBox) {
    return {
      x: toBox.x + toBox.w + 1,
      y: toBox.y + toBox.h / 2 + 4,
      angle: 180,
    };
  }
  return {
    x: toBox.x,
    y: toBox.y + toBox.h / 2,
    angle: 0,
  };
}

export function RecurseGraph({
  nodes,
  edges,
  groupOrder,
  groupLabels,
  height = 280,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  groupOrder?: string[];
  groupLabels?: Record<string, string>;
  height?: number;
}) {
  if (nodes.length === 0) return null;

  const layout = layoutColumns(nodes, (n) => n.kind);

  // groupOrder may reorder columns: rebuild positions if specified
  if (groupOrder) {
    const groups = new Map<string, GraphNode[]>();
    nodes.forEach((n) => {
      if (!groups.has(n.kind)) groups.set(n.kind, []);
      groups.get(n.kind)!.push(n);
    });
    layout.nodes.clear();
    let maxRows = 0;
    groupOrder.forEach((k, ci) => {
      const list = groups.get(k) ?? [];
      if (list.length > maxRows) maxRows = list.length;
      list.forEach((n, ri) => {
        layout.nodes.set(n.id, {
          x: PAD_X + ci * COL_GAP,
          y: PAD_TOP + ri * ROW_GAP,
          w: NODE_W,
          h: NODE_H,
          node: n,
        });
      });
    });
    layout.width = PAD_X * 2 + Math.max(0, groupOrder.length - 1) * COL_GAP + NODE_W;
    layout.height = PAD_TOP + PAD_BOTTOM + maxRows * ROW_GAP;
  }

  // count self-loops per node so we can offset stacked self-loops
  const selfCounters = new Map<string, number>();
  const renderedEdges = edges.map((e) => {
    const f = layout.nodes.get(e.from_id);
    const t = layout.nodes.get(e.to_id);
    if (!f || !t) return null;
    let selfIdx = 0;
    if (e.from_id === e.to_id) {
      selfIdx = selfCounters.get(e.from_id) ?? 0;
      selfCounters.set(e.from_id, selfIdx + 1);
    }
    const { d, isSelf } = edgePath(f, t, selfIdx);
    const arrow = endpointForArrow(f, t);
    const isCrossKind = f.node.kind !== t.node.kind;
    return { d, isSelf, isCrossKind, arrow, edge: e };
  }).filter((x): x is NonNullable<typeof x> => x != null);

  const cols: { kind: string; x: number }[] = [];
  if (groupOrder) {
    groupOrder.forEach((k, ci) => {
      cols.push({ kind: k, x: PAD_X + ci * COL_GAP + NODE_W / 2 });
    });
  }

  const renderedHeight = Math.max(height, layout.height);

  return (
    <div className="atlas-recurse-graph" style={{ height: `${renderedHeight}px` }}>
      <svg
        viewBox={`0 0 ${layout.width} ${renderedHeight}`}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <marker
            id="atlas-graph-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" className="atlas-graph-arrowhead" />
          </marker>
        </defs>

        {cols.map((c) => (
          <text
            key={c.kind}
            className="atlas-graph-column-label"
            x={c.x}
            y={18}
            textAnchor="middle"
          >
            {(groupLabels?.[c.kind] ?? c.kind).replace("_", " ")}
          </text>
        ))}

        {renderedEdges.map((re, i) => (
          <path
            key={i}
            d={re.d}
            className={`atlas-graph-edge ${re.isSelf ? "is-self" : ""} ${re.isCrossKind ? "is-cross-kind" : ""}`}
            markerEnd="url(#atlas-graph-arrow)"
          />
        ))}

        {Array.from(layout.nodes.values()).map((p) => {
          const inner = (
            <g>
              <rect
                x={p.x}
                y={p.y}
                width={p.w}
                height={p.h}
                rx={2}
                className={`atlas-graph-node-rect is-${p.node.kind === "orchestrator" ? "orchestrator" : p.node.kind === "versus_workflow" ? "versus" : "default"}`}
              />
              <text
                x={p.x + p.w / 2}
                y={p.y + p.h / 2 + 4}
                textAnchor="middle"
                className="atlas-graph-node-label"
              >
                {p.node.label}
              </text>
            </g>
          );
          if (p.node.href) {
            return (
              <a key={p.node.id} href={p.node.href}>
                {inner}
              </a>
            );
          }
          return <g key={p.node.id}>{inner}</g>;
        })}
      </svg>
    </div>
  );
}
