"use client";

import type { Page } from "@/lib/types";
import { CredenceBadge } from "./CredenceBadge";
import { NodeTypeLabel } from "./NodeTypeLabel";
import { PageContent } from "./PageContent";

interface JudgementHistoryProps {
  supersededJudgements: Page[];
}

export function JudgementHistory({ supersededJudgements }: JudgementHistoryProps) {
  if (supersededJudgements.length === 0) return null;

  return (
    <details className="judgement-history">
      <summary>
        {supersededJudgements.length} previous judgement{supersededJudgements.length > 1 ? "s" : ""}
      </summary>
      <div>
        {supersededJudgements.map((page, i) => (
          <div key={page.id ?? i} className="judgement-history-item">
            <div className="judgement-history-meta">
              <NodeTypeLabel type="judgement" />
              <CredenceBadge credence={page.credence} robustness={page.robustness} />
            </div>
            <h4>{page.headline}</h4>
            {page.content && (
              <PageContent text={page.content} excludeConceptId={page.id} />
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
