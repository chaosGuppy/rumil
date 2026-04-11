"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import type { Subgraph, SubgraphNode } from "@/api";

// Page types that have dedicated CSS color vars in globals.css. Unknown types
// fall back to neutral muted colors so the canvas still renders cleanly.
const KNOWN_PAGE_TYPES = [
  "question",
  "claim",
  "judgement",
  "source",
  "concept",
  "wiki",
] as const;

function typeColor(t: string): string {
  return (KNOWN_PAGE_TYPES as readonly string[]).includes(t)
    ? `var(--type-${t})`
    : "var(--color-muted)";
}

type Pos = { x: number; y: number };
type LaidOutNode = SubgraphNode & Pos;

const WIDTH = 720;
const HEIGHT = 520;
const CX = WIDTH / 2;
const CY = HEIGHT / 2;
const MAX_R = Math.min(WIDTH, HEIGHT) / 2 - 48;
const ANCHOR_R = 10;
const NODE_R = 6.5;
const ARROW_GAP = 4;

export function SubgraphView({
  data,
  anchorId,
}: {
  data: Subgraph;
  anchorId: string;
}) {
  const router = useRouter();
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const { laidOut, positions, maxDepth } = useMemo(() => {
    if (data.nodes.length === 0) {
      return {
        laidOut: [] as LaidOutNode[],
        positions: new Map<string, Pos>(),
        maxDepth: 0,
      };
    }

    const maxDepth = Math.max(0, ...data.nodes.map((n) => n.depth));
    const byDepth = new Map<number, SubgraphNode[]>();
    for (const n of data.nodes) {
      if (!byDepth.has(n.depth)) byDepth.set(n.depth, []);
      byDepth.get(n.depth)!.push(n);
    }
    // Stable angular ordering: group by type first so similar nodes cluster
    // within each ring, then break ties by id.
    for (const arr of byDepth.values()) {
      arr.sort((a, b) => {
        if (a.page_type !== b.page_type)
          return a.page_type < b.page_type ? -1 : 1;
        return a.id < b.id ? -1 : 1;
      });
    }

    const positions = new Map<string, Pos>();
    const laid: LaidOutNode[] = [];

    for (const [d, nodes] of byDepth) {
      const r = maxDepth === 0 ? 0 : (MAX_R * d) / maxDepth;
      const n = nodes.length;
      if (d === 0 || r === 0) {
        for (const node of nodes) {
          const p = { x: CX, y: CY };
          positions.set(node.id, p);
          laid.push({ ...node, ...p });
        }
      } else {
        // Offset alternating rings so nodes don't line up radially; makes
        // layered rings visually separable.
        const offset = ((d % 2) * Math.PI) / (n || 1);
        for (let i = 0; i < n; i++) {
          const theta = (2 * Math.PI * i) / n + offset - Math.PI / 2;
          const p = {
            x: CX + r * Math.cos(theta),
            y: CY + r * Math.sin(theta),
          };
          const node = nodes[i];
          positions.set(node.id, p);
          laid.push({ ...node, ...p });
        }
      }
    }

    return { laidOut: laid, positions, maxDepth };
  }, [data.nodes]);

  const nodeRadius = (id: string): number =>
    id === anchorId ? ANCHOR_R : NODE_R;

  // Trim each edge's endpoints so it starts at the rim of the source node
  // and ends a few pixels short of the target rim — the gap is where the
  // arrowhead marker sits, so arrows stay visible instead of being swallowed
  // by the target circle.
  const edges = useMemo(() => {
    return data.edges
      .map((e) => {
        const from = positions.get(e.from_page_id);
        const to = positions.get(e.to_page_id);
        if (!from || !to) return null;
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len;
        const uy = dy / len;
        const srcR = nodeRadius(e.from_page_id);
        const dstR = nodeRadius(e.to_page_id) + ARROW_GAP;
        return {
          ...e,
          x1: from.x + ux * srcR,
          y1: from.y + uy * srcR,
          x2: to.x - ux * dstR,
          y2: to.y - uy * dstR,
        };
      })
      .filter((e): e is NonNullable<typeof e> => e !== null);
  }, [data.edges, positions, anchorId]);

  const hoveredNode = useMemo(() => {
    if (!hoveredId) return null;
    return laidOut.find((n) => n.id === hoveredId) ?? null;
  }, [hoveredId, laidOut]);

  const hoveredNeighbors = useMemo(() => {
    if (!hoveredId) return new Set<string>();
    const s = new Set<string>();
    for (const e of data.edges) {
      if (e.from_page_id === hoveredId) s.add(e.to_page_id);
      if (e.to_page_id === hoveredId) s.add(e.from_page_id);
    }
    return s;
  }, [data.edges, hoveredId]);

  const ringR = (d: number): number =>
    maxDepth === 0 ? 0 : (MAX_R * d) / maxDepth;

  const typesPresent = useMemo(() => {
    const s = new Set<string>();
    for (const n of data.nodes) s.add(n.page_type);
    return Array.from(s).sort();
  }, [data.nodes]);

  if (data.nodes.length === 0) {
    return (
      <div className="subgraph-panel subgraph-empty">
        <style>{subgraphCss}</style>
        <div className="panel-title">Neighborhood</div>
        <div className="empty">no nodes in scope</div>
      </div>
    );
  }

  return (
    <div className="subgraph-panel">
      <style>{subgraphCss}</style>
      <div className="panel-header">
        <div className="panel-title">Neighborhood</div>
        <div className="panel-subtitle">
          {data.nodes.length} page{data.nodes.length === 1 ? "" : "s"} ·{" "}
          {data.edges.length} link{data.edges.length === 1 ? "" : "s"} ·{" "}
          {maxDepth} hop{maxDepth === 1 ? "" : "s"} deep
        </div>
      </div>
      <div
        className="canvas-wrap"
        onMouseLeave={() => setHoveredId(null)}
      >
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          width="100%"
          height="100%"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={`Neighborhood of ${data.nodes.length} pages within ${maxDepth} hops`}
        >
          <defs>
            <marker
              id="subgraph-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-muted)" />
            </marker>
            <marker
              id="subgraph-arrow-hover"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path
                d="M 0 0 L 10 5 L 0 10 z"
                fill="var(--color-foreground)"
              />
            </marker>
          </defs>

          <rect
            x={0}
            y={0}
            width={WIDTH}
            height={HEIGHT}
            fill="transparent"
            onMouseMove={() => setHoveredId(null)}
          />

          {Array.from({ length: maxDepth }, (_, i) => i + 1).map((d) => (
            <circle
              key={`ring-${d}`}
              cx={CX}
              cy={CY}
              r={ringR(d)}
              fill="none"
              stroke="var(--color-border)"
              strokeDasharray="2 4"
              strokeWidth={1}
              opacity={0.5}
            />
          ))}

          <g>
            {edges.map((e, i) => {
              const isHoveredEdge =
                hoveredId !== null &&
                (e.from_page_id === hoveredId || e.to_page_id === hoveredId);
              const dim = hoveredId !== null && !isHoveredEdge;
              return (
                <line
                  key={`edge-${i}`}
                  x1={e.x1}
                  y1={e.y1}
                  x2={e.x2}
                  y2={e.y2}
                  stroke={
                    isHoveredEdge
                      ? "var(--color-foreground)"
                      : "var(--color-muted)"
                  }
                  strokeWidth={isHoveredEdge ? 1.4 : 0.7}
                  opacity={isHoveredEdge ? 0.85 : dim ? 0.08 : 0.22}
                  markerEnd={
                    dim
                      ? undefined
                      : isHoveredEdge
                        ? "url(#subgraph-arrow-hover)"
                        : "url(#subgraph-arrow)"
                  }
                />
              );
            })}
          </g>

          <g>
            {laidOut.map((n) => {
              const isAnchor = n.id === anchorId;
              const isHovered = n.id === hoveredId;
              const isNeighbor = hoveredNeighbors.has(n.id);
              const dim = hoveredId !== null && !isHovered && !isNeighbor;
              const baseR = isAnchor ? ANCHOR_R : NODE_R;
              const r = baseR + (isHovered ? 2.5 : 0);
              return (
                <g
                  key={n.id}
                  transform={`translate(${n.x}, ${n.y})`}
                  onMouseEnter={() => setHoveredId(n.id)}
                  onClick={() => router.push(`/pages/${n.id}`)}
                  style={{
                    cursor: "pointer",
                    opacity: dim ? 0.18 : 1,
                    transition: "opacity 0.15s ease",
                  }}
                >
                  {isAnchor && (
                    <circle
                      r={baseR + 6}
                      fill="none"
                      stroke="var(--color-foreground)"
                      strokeWidth={1}
                      opacity={0.35}
                    />
                  )}
                  <circle
                    r={r}
                    fill={typeColor(n.page_type)}
                    fillOpacity={isAnchor || isHovered ? 1 : 0.6}
                    stroke={
                      isAnchor
                        ? "var(--color-foreground)"
                        : "var(--background)"
                    }
                    strokeWidth={isAnchor ? 2 : 1.5}
                  />
                </g>
              );
            })}
          </g>
        </svg>
        <div className={`hover-info${hoveredNode ? " visible" : ""}`}>
          {hoveredNode && (
            <>
              <span
                className="hover-type"
                style={{ color: typeColor(hoveredNode.page_type) }}
              >
                {hoveredNode.page_type}
              </span>
              <span className="hover-headline">
                {hoveredNode.headline ?? hoveredNode.id}
              </span>
              <span className="hover-depth">hop {hoveredNode.depth}</span>
            </>
          )}
        </div>
      </div>
      <div className="legend">
        <span className="legend-label">page types</span>
        {typesPresent.map((t) => (
          <span key={t} className="legend-item">
            <span
              className="swatch"
              style={{ background: typeColor(t) }}
            />
            {t}
          </span>
        ))}
        <span className="legend-hint">click a node to open</span>
      </div>
    </div>
  );
}

const subgraphCss = `
  .subgraph-panel {
    border: 1px solid var(--color-border);
    background: var(--color-background);
    margin-top: 1rem;
    margin-bottom: 1rem;
    padding: 1rem 1.25rem 1.25rem 1.25rem;
  }
  .subgraph-panel .panel-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
  }
  .subgraph-panel .panel-title {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--color-foreground);
  }
  .subgraph-panel .panel-subtitle {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    color: var(--color-muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .subgraph-panel .canvas-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 720 / 520;
    max-height: 520px;
    border: 1px solid var(--color-border);
    background:
      radial-gradient(circle at center, rgba(0,0,0,0.022) 1px, transparent 1.5px) 0 0 / 18px 18px,
      var(--color-background);
  }
  @media (prefers-color-scheme: dark) {
    .subgraph-panel .canvas-wrap {
      background:
        radial-gradient(circle at center, rgba(255,255,255,0.035) 1px, transparent 1.5px) 0 0 / 18px 18px,
        var(--color-background);
    }
  }
  .subgraph-panel .hover-info {
    position: absolute;
    left: 0.75rem;
    bottom: 0.75rem;
    right: 0.75rem;
    display: flex;
    align-items: baseline;
    gap: 0.65rem;
    padding: 0.5rem 0.75rem;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-left: 3px solid var(--color-accent);
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.15s ease, transform 0.15s ease;
    pointer-events: none;
  }
  .subgraph-panel .hover-info.visible {
    opacity: 1;
    transform: translateY(0);
  }
  .subgraph-panel .hover-type {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .subgraph-panel .hover-headline {
    font-size: 0.8rem;
    color: var(--color-foreground);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 auto;
    min-width: 0;
  }
  .subgraph-panel .hover-depth {
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.6rem;
    color: var(--color-muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .subgraph-panel .legend {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.8rem;
    margin-top: 0.8rem;
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.65rem;
    color: var(--color-muted);
    letter-spacing: 0.04em;
  }
  .subgraph-panel .legend-label {
    color: var(--color-dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    font-size: 0.6rem;
  }
  .subgraph-panel .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .subgraph-panel .legend-item .swatch {
    width: 0.75rem;
    height: 0.75rem;
    border: 1.25px solid transparent;
    border-radius: 999px;
  }
  .subgraph-panel .legend-hint {
    margin-left: auto;
    color: var(--color-dim);
    font-size: 0.6rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .subgraph-panel.subgraph-empty .empty {
    padding: 2rem 0;
    text-align: center;
    color: var(--color-muted);
    font-family: var(--font-geist-mono), monospace;
    font-size: 0.75rem;
    letter-spacing: 0.06em;
  }
`;
