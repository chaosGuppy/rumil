"use client";

import type { WorldviewNode } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { TextWithConcepts } from "./ConceptRef";

interface JudgementHistoryProps {
  supersededJudgements: WorldviewNode[];
}

export function JudgementHistory({ supersededJudgements }: JudgementHistoryProps) {
  if (supersededJudgements.length === 0) return null;

  return (
    <details className="judgement-history">
      <summary>
        {supersededJudgements.length} previous judgement{supersededJudgements.length > 1 ? "s" : ""}
      </summary>
      <div>
        {supersededJudgements.map((node, i) => (
          <div key={node.id ?? i} className="judgement-history-item">
            <div className="judgement-history-meta">
              <NodeTypeLabel type="judgement" />
              <CredenceBadge credence={node.credence} robustness={node.robustness} />
            </div>
            <h4>{node.headline}</h4>
            {node.content && (
              <div className="worldview-prose">
                <p style={{ margin: 0 }}>
                  <TextWithConcepts text={node.content} excludeConceptId={node.id} />
                </p>
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
