"use client";

import { useState, useEffect, useCallback, useImperativeHandle, forwardRef } from "react";
import { useSearchParams, useRouter, usePathname } from "next/navigation";
import type { WorldviewNode, Worldview } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";

interface VerticalViewProps {
  worldview: Worldview;
  focusNodeId?: string | null;
  onFocusHandled?: () => void;
}

function findNodePath(
  nodes: WorldviewNode[],
  match: string,
  prefix = "",
): string | null {
  for (let i = 0; i < nodes.length; i++) {
    const path = prefix ? `${prefix}.${i}` : String(i);
    if (nodes[i].headline.includes(match)) return path;
    const found = findNodePath(nodes[i].children, match, path);
    if (found) return found;
  }
  return null;
}

function ancestorPaths(path: string): string[] {
  const parts = path.split(".");
  return parts.map((_, i) => parts.slice(0, i + 1).join("."));
}

function collectAllPaths(
  nodes: WorldviewNode[],
  prefix = "",
): string[] {
  const paths: string[] = [];
  for (let i = 0; i < nodes.length; i++) {
    const path = prefix ? `${prefix}.${i}` : String(i);
    if (nodes[i].children.length > 0) {
      paths.push(path);
      paths.push(...collectAllPaths(nodes[i].children, path));
    }
  }
  return paths;
}

function VerticalNode({
  node,
  path,
  depth,
  expanded,
  onToggle,
  onFocus,
  focusedId,
}: {
  node: WorldviewNode;
  path: string;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  onFocus: (headline: string) => void;
  focusedId: string | null;
}) {
  const isExpanded = expanded.has(path);
  const isRoot = depth === 0;
  const hasChildren = node.children.length > 0;
  const isFocused = focusedId ? node.headline.includes(focusedId) : false;

  return (
    <div
      className={`vertical-node ${isFocused ? "node-focused" : ""}`}
      data-path={path}
    >
      <div className="vertical-node-inner">
        <div className="vertical-node-header">
          <NodeTypeLabel type={node.node_type} />
          <span
            className="vertical-headline"
            onClick={() => {
              if (hasChildren) onToggle(path);
              onFocus(node.headline);
            }}
          >
            {node.headline}
          </span>
          <CredenceBadge
            credence={node.credence}
            robustness={node.robustness}
          />
          {node.importance !== undefined && node.importance > 0 && (
            <span className="vertical-label-dim">L{node.importance}</span>
          )}
        </div>

        {(isRoot || isExpanded) && (
          <div className="worldview-prose vertical-content">
            <p>{node.content}</p>
          </div>
        )}

        {hasChildren && (
          <button className="vertical-toggle" onClick={() => onToggle(path)}>
            <span className={`vertical-chevron ${isExpanded ? "open" : ""}`}>
              {isExpanded ? "\u25BE" : "\u25B8"}
            </span>
            <span>
              {node.children.length}{" "}
              {node.children.length === 1 ? "child" : "children"}
            </span>
          </button>
        )}
      </div>

      {isExpanded && (
        <div
          className="vertical-children"
          style={{ borderColor: `var(--active-${depth % 5})` }}
        >
          {node.children.map((child, i) => (
            <VerticalNode
              key={i}
              node={child}
              path={`${path}.${i}`}
              depth={depth + 1}
              expanded={expanded}
              onToggle={onToggle}
              onFocus={onFocus}
              focusedId={focusedId}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export interface VerticalViewHandle {
  expandAll: () => void;
  collapseAll: () => void;
}

export const VerticalView = forwardRef<VerticalViewHandle, VerticalViewProps>(
  function VerticalView({ worldview, focusNodeId, onFocusHandled }, ref) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [focusedId, setFocusedId] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const param = searchParams.get("expanded");
    return param ? new Set(param.split(",")) : new Set<string>();
  });

  const updateUrl = useCallback(
    (next: Set<string>) => {
      const params = new URLSearchParams(searchParams.toString());
      if (next.size > 0) {
        params.set("expanded", [...next].join(","));
      } else {
        params.delete("expanded");
      }
      const query = params.toString();
      router.replace(`${pathname}${query ? `?${query}` : ""}`, {
        scroll: false,
      });
    },
    [searchParams, router, pathname],
  );

  const expandAll = useCallback(() => {
    const all = new Set(collectAllPaths(worldview.nodes));
    setExpanded(all);
    updateUrl(all);
  }, [worldview.nodes, updateUrl]);

  const collapseAll = useCallback(() => {
    const empty = new Set<string>();
    setExpanded(empty);
    updateUrl(empty);
  }, [updateUrl]);

  useImperativeHandle(ref, () => ({ expandAll, collapseAll }), [expandAll, collapseAll]);

  const toggleExpand = useCallback(
    (path: string) => {
      const next = new Set(expanded);
      if (next.has(path)) {
        for (const p of next) {
          if (p === path || p.startsWith(path + ".")) next.delete(p);
        }
      } else {
        next.add(path);
      }
      setExpanded(next);
      updateUrl(next);
    },
    [expanded, updateUrl],
  );

  useEffect(() => {
    if (!focusNodeId) return;
    setFocusedId(focusNodeId);

    const path = findNodePath(worldview.nodes, focusNodeId);
    if (path) {
      const next = new Set(expanded);
      for (const p of ancestorPaths(path)) next.add(p);
      setExpanded(next);
      updateUrl(next);
    }

    const scrollTimer = setTimeout(() => {
      if (path) {
        document
          .querySelector(`[data-path="${path}"]`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }, 50);
    const clearTimer = setTimeout(() => {
      setFocusedId(null);
      onFocusHandled?.();
    }, 3000);
    return () => {
      clearTimeout(scrollTimer);
      clearTimeout(clearTimer);
    };
  }, [focusNodeId, worldview.nodes, updateUrl, onFocusHandled]);

  return (
    <div className="vertical-layout">
      <div className="vertical-scroll">
        <header className="vertical-header">
          <h1>{worldview.question_headline}</h1>
          <div
            className="worldview-prose"
            style={{
              color: "var(--fg-muted)",
              borderLeft: "2px solid var(--border)",
              paddingLeft: "16px",
            }}
          >
            <p style={{ margin: 0 }}>{worldview.summary}</p>
          </div>
          <div className="vertical-date">
            Generated{" "}
            {new Date(worldview.generated_at).toLocaleDateString("en-US", {
              year: "numeric",
              month: "long",
              day: "numeric",
            })}
          </div>
        </header>

        <div className="vertical-tree">
          {worldview.nodes.map((node, i) => (
            <VerticalNode
              key={i}
              node={node}
              path={String(i)}
              depth={0}
              expanded={expanded}
              onToggle={toggleExpand}
              onFocus={setFocusedId}
              focusedId={focusedId}
            />
          ))}
        </div>
      </div>
    </div>
  );
});
