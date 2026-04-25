"use client";

import { useEffect, useMemo } from "react";
import type { RunTraceTreeOut, CallNodeOut } from "@/api/types.gen";
import { useRunTraceTree } from "@/lib/use-run-trace";
import { useStagedRun } from "@/lib/staged-run-context";
import { CallNode, HashTargetProvider, type TreeNode } from "./call-node";
import { PageLoadStats } from "./page-load-stats";

export type SequenceNode = {
  id: string;
  calls: TreeNode[];
};

function buildTree(calls: CallNodeOut[]): TreeNode[] {
  const byId = new Map<string, CallNodeOut>();
  for (const n of calls) byId.set(n.call.id, n);

  const childrenOf = new Map<string, CallNodeOut[]>();
  const roots: CallNodeOut[] = [];

  for (const n of calls) {
    if (!n.call.parent_call_id) {
      roots.push(n);
    } else {
      const list = childrenOf.get(n.call.parent_call_id) ?? [];
      list.push(n);
      childrenOf.set(n.call.parent_call_id, list);
    }
  }

  function toTreeNode(node: CallNodeOut): TreeNode {
    const kids = childrenOf.get(node.call.id) ?? [];
    const directChildren: CallNodeOut[] = [];
    const seqGroups = new Map<string, CallNodeOut[]>();

    for (const k of kids) {
      if (k.call.sequence_id) {
        const list = seqGroups.get(k.call.sequence_id) ?? [];
        list.push(k);
        seqGroups.set(k.call.sequence_id, list);
      } else {
        directChildren.push(k);
      }
    }

    const sequences: SequenceNode[] = [];
    for (const [seqId, seqCalls] of seqGroups) {
      const sorted = [...seqCalls].sort(
        (a, b) => (a.call.sequence_position ?? 0) - (b.call.sequence_position ?? 0),
      );
      sequences.push({
        id: seqId,
        calls: sorted.map(toTreeNode),
      });
    }

    const childTrees = directChildren.map(toTreeNode);
    const allDescendants = [
      ...childTrees,
      ...sequences.flatMap((s) => s.calls),
    ];
    const subtreeCost =
      (node.call.cost_usd ?? 0) +
      allDescendants.reduce((sum, t) => sum + (t.node.call.cost_usd ?? 0), 0);

    return {
      node: {
        ...node,
        call: {
          ...node.call,
          cost_usd: subtreeCost > 0 ? subtreeCost : null,
        },
      },
      children: childTrees,
      sequences,
    };
  }

  return roots.map(toTreeNode);
}

export function TraceViewer({
  initialTrace,
  runId,
  realtimeConfig,
}: {
  initialTrace: RunTraceTreeOut;
  runId: string;
  realtimeConfig: { url: string; anon_key: string } | null;
}) {
  const trace = useRunTraceTree(runId, initialTrace, realtimeConfig);
  const tree = useMemo(() => buildTree(trace.calls), [trace.calls]);
  const { setActiveStagedRunId } = useStagedRun();

  useEffect(() => {
    if (trace.staged) {
      setActiveStagedRunId(runId);
    }
  }, [trace.staged, runId, setActiveStagedRunId]);

  return (
    <HashTargetProvider>
      <div className="trace-root">
        {trace.cost_usd != null && (
          <div className="trace-run-cost">
            Total cost: ${trace.cost_usd.toFixed(4)}
          </div>
        )}
        {tree.map((t) => (
          <CallNode key={t.node.call.id} tree={t} depth={0} />
        ))}
        {tree.length === 0 && (
          <p className="trace-empty">
            No calls recorded for this run yet.
          </p>
        )}
        <PageLoadStats runId={runId} />
      </div>
    </HashTargetProvider>
  );
}
