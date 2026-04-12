"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import type { WorldviewNode, Worldview } from "@/lib/types";
import { WorldviewNodeCard } from "./WorldviewNode";

interface PaneState {
  parentNode: WorldviewNode;
  parentIndex: number;
  depth: number;
}

function resolveNode(
  worldview: Worldview,
  path: string,
): WorldviewNode | null {
  const parts = path.split(".").map(Number);
  if (parts.some(isNaN)) return null;

  let current: WorldviewNode | undefined = worldview.nodes[parts[0]];
  for (let i = 1; i < parts.length; i++) {
    if (!current) return null;
    current = current.children[parts[i]];
  }
  return current ?? null;
}

function buildPath(panes: PaneState[], worldview: Worldview): string {
  if (panes.length === 0) return "";

  const paths: string[] = [];
  for (const pane of panes) {
    const rootIndex = worldview.nodes.indexOf(pane.parentNode);
    if (rootIndex >= 0) {
      paths.push(String(rootIndex));
    } else {
      paths.push(String(pane.parentIndex));
    }
  }
  return paths.join(",");
}

function findNodeIndex(
  nodes: WorldviewNode[],
  shortId: string,
): number | null {
  function searchTree(node: WorldviewNode): boolean {
    if (node.headline.includes(shortId)) return true;
    return node.children.some(searchTree);
  }
  for (let i = 0; i < nodes.length; i++) {
    if (searchTree(nodes[i])) return i;
  }
  return null;
}

function NodePane({
  pane,
  onExpand,
  activePanePath,
  onClose,
  depth,
  onFocusNode,
  focusedNodeId,
}: {
  pane: PaneState;
  onExpand: (node: WorldviewNode, index: number) => void;
  activePanePath: string | null;
  onClose: () => void;
  depth: number;
  onFocusNode?: (nodeId: string) => void;
  focusedNodeId?: string | null;
}) {
  return (
    <div style={{ padding: "28px 28px 32px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: "20px",
          paddingBottom: "16px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <h2
          style={{
            fontSize: "15px",
            fontWeight: 500,
            margin: 0,
            color: "var(--fg)",
            lineHeight: 1.4,
          }}
        >
          {pane.parentNode.headline}
        </h2>
        <button
          onClick={onClose}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            padding: "4px 8px",
            color: "var(--fg-dim)",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "11px",
            letterSpacing: "0.04em",
            flexShrink: 0,
          }}
          onMouseEnter={(e) =>
            (e.currentTarget.style.color = "var(--fg-muted)")
          }
          onMouseLeave={(e) =>
            (e.currentTarget.style.color = "var(--fg-dim)")
          }
          aria-label="Close pane"
        >
          close
        </button>
      </div>

      {pane.parentNode.children.map((child, i) => (
        <WorldviewNodeCard
          key={i}
          node={child}
          index={i}
          onExpandPane={
            child.children.length > 0
              ? (node: WorldviewNode, idx: number) => onExpand(node, idx)
              : undefined
          }
          onFocus={onFocusNode}
          isActive={activePanePath === String(i)}
          isFocused={focusedNodeId ? child.headline.includes(focusedNodeId) : false}
          activeDepth={depth + 1}
        />
      ))}
    </div>
  );
}

interface StackedPanesProps {
  worldview: Worldview;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
}

export function StackedPanes({
  worldview,
  focusNodeId,
  onFocusHandled,
}: StackedPanesProps) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const containerRef = useRef<HTMLDivElement>(null);
  const lastPaneRef = useRef<HTMLDivElement>(null);

  const [panes, setPanes] = useState<PaneState[]>(() => {
    const panesParam = searchParams.get("panes");
    if (!panesParam) return [];

    const paths = panesParam.split(",");
    const resolved: PaneState[] = [];
    for (const path of paths) {
      const node = resolveNode(worldview, path);
      if (node && node.children.length > 0) {
        const index = parseInt(path.split(".").pop() ?? "0", 10);
        resolved.push({
          parentNode: node,
          parentIndex: index,
          depth: resolved.length + 1,
        });
      }
    }
    return resolved;
  });

  const [activeRootIndex, setActiveRootIndex] = useState<number | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const updateUrl = useCallback(
    (newPanes: PaneState[]) => {
      const path = buildPath(newPanes, worldview);
      const params = new URLSearchParams(searchParams.toString());
      if (path) {
        params.set("panes", path);
      } else {
        params.delete("panes");
      }
      const query = params.toString();
      router.replace(`${pathname}${query ? `?${query}` : ""}`, {
        scroll: false,
      });
    },
    [searchParams, router, pathname, worldview],
  );

  const handleExpandFromRoot = useCallback(
    (node: WorldviewNode, index: number) => {
      const newPanes = [
        { parentNode: node, parentIndex: index, depth: 1 },
      ];
      setPanes(newPanes);
      setActiveRootIndex(index);
      setFocusedId(null);
      updateUrl(newPanes);
    },
    [updateUrl],
  );

  const handleFocusRoot = useCallback(
    (index: number) => {
      setActiveRootIndex(index);
      setPanes([]);
      setFocusedId(null);
      updateUrl([]);
      const el = document.getElementById(`node-${index}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    },
    [updateUrl],
  );

  const handleExpandFromPane = useCallback(
    (paneDepth: number, node: WorldviewNode, childIndex: number) => {
      const newPanes = [
        ...panes.slice(0, paneDepth),
        {
          parentNode: node,
          parentIndex: childIndex,
          depth: paneDepth + 1,
        },
      ];
      setPanes(newPanes);
      setFocusedId(null);
      updateUrl(newPanes);
    },
    [panes, updateUrl],
  );

  const handleClosePane = useCallback(
    (depth: number) => {
      const newPanes = panes.slice(0, depth);
      setPanes(newPanes);
      updateUrl(newPanes);
      if (newPanes.length === 0) {
        setActiveRootIndex(null);
      }
    },
    [panes, updateUrl],
  );

  // Handle external focus requests (from chat refs)
  useEffect(() => {
    if (!focusNodeId) return;

    setFocusedId(focusNodeId);

    const rootIdx = findNodeIndex(worldview.nodes, focusNodeId);
    if (rootIdx !== null) {
      setActiveRootIndex(rootIdx);
      setPanes([]);
      updateUrl([]);
      setTimeout(() => {
        const el = document.getElementById(`node-${rootIdx}`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }, 50);
    }

    // Clear focus highlight after a few seconds
    const timer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => clearTimeout(timer);
  }, [focusNodeId, worldview.nodes, updateUrl, onFocusHandled]);

  useEffect(() => {
    if (lastPaneRef.current && panes.length > 0) {
      lastPaneRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "start",
      });
    }
  }, [panes.length]);

  return (
    <div
      ref={containerRef}
      className="pane-container"
    >
      {/* Root pane — always full width */}
      <div className="pane">
        <div style={{ padding: "48px 36px" }}>
          <header style={{ marginBottom: "40px" }}>
            <h1
              style={{
                fontSize: "28px",
                fontWeight: 400,
                lineHeight: 1.3,
                margin: "0 0 20px 0",
                fontFamily: "var(--font-body-stack)",
                letterSpacing: "-0.01em",
              }}
            >
              {worldview.question_headline}
            </h1>
            <div
              className="worldview-prose"
              style={{
                fontSize: "15px",
                color: "var(--fg-muted)",
                borderLeft: "2px solid var(--border)",
                paddingLeft: "16px",
              }}
            >
              <p style={{ margin: 0 }}>{worldview.summary}</p>
            </div>
            <div
              style={{
                marginTop: "12px",
                fontFamily: "var(--font-mono-stack)",
                fontSize: "10px",
                color: "var(--fg-dim)",
                letterSpacing: "0.04em",
              }}
            >
              Generated{" "}
              {new Date(worldview.generated_at).toLocaleDateString("en-US", {
                year: "numeric",
                month: "long",
                day: "numeric",
              })}
            </div>
          </header>

          <div>
            {worldview.nodes.map((node, i) => (
              <div key={i} id={`node-${i}`}>
                <WorldviewNodeCard
                  node={node}
                  index={i}
                  onExpandPane={
                    node.children.length > 0
                      ? () => handleExpandFromRoot(node, i)
                      : undefined
                  }
                  onFocus={() => handleFocusRoot(i)}
                  isActive={activeRootIndex === i}
                  isFocused={focusedId ? node.headline.includes(focusedId) : false}
                  activeDepth={0}
                />
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Detail panes — continuous surface with source */}
      {panes.map((pane, i) => {
        const isLast = i === panes.length - 1;
        const depthIndex = i % 5;

        return (
          <div
            key={`${pane.parentNode.headline}-${i}`}
            ref={isLast ? lastPaneRef : undefined}
            className={`pane pane-connected ${isLast ? "pane-entering" : ""}`}
            style={{ "--active-tint": `var(--active-${depthIndex})` } as React.CSSProperties}
          >
            <NodePane
              pane={pane}
              depth={i}
              onExpand={(node, childIdx) =>
                handleExpandFromPane(i + 1, node, childIdx)
              }
              activePanePath={
                i + 1 < panes.length
                  ? String(panes[i + 1].parentIndex)
                  : null
              }
              onClose={() => handleClosePane(i)}
              focusedNodeId={focusedId}
            />
          </div>
        );
      })}
    </div>
  );
}
