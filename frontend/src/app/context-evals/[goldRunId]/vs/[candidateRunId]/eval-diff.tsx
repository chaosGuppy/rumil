"use client";

import Link from "next/link";
import type { ContextEvalDiffOut, PageRef } from "@/api/types.gen";
import {
  ContextBuiltBody,
  PageList,
} from "@/components/context-built-body";
import "../../../../traces/[runId]/trace.css";
import "./context-eval-diff.css";

function PageBucket({
  label,
  pages,
  arm,
}: {
  label: string;
  pages: PageRef[];
  arm: "gold" | "candidate" | "both";
}) {
  return (
    <div className={`ce-bucket ce-bucket--${arm}`}>
      <div className="ce-bucket-header">
        <span className="ce-bucket-label">{label}</span>
        <span className="ce-bucket-count">{pages.length}</span>
      </div>
      <div className="ce-bucket-body">
        <PageList pages={pages} />
      </div>
    </div>
  );
}

function ArmConfigSummary({
  config,
}: {
  config: { [key: string]: unknown };
}) {
  const evalMeta = (config?.eval ?? {}) as { [key: string]: unknown };
  const role = String(evalMeta.role ?? "");
  const builder = String(evalMeta.context_builder ?? "");
  return (
    <div className="ce-arm-summary">
      <span className="ce-arm-role">{role}</span>
      <span className="ce-arm-builder">{builder}</span>
    </div>
  );
}

export function ContextEvalDiff({ data }: { data: ContextEvalDiffOut }) {
  const goldOnly = data.pages_only_in_gold ?? [];
  const candOnly = data.pages_only_in_candidate ?? [];
  const both = data.pages_in_both ?? [];

  return (
    <main className="ce-page">
      <header className="ce-header">
        {data.question && (
          <Link href={`/pages/${data.question.id}`} className="ce-back-link">
            &larr; {data.question.headline}
          </Link>
        )}
        <h1 className="ce-title">Context-builder comparison</h1>
        <div className="ce-arms-bar">
          <div className="ce-arm-card ce-arm-card--gold">
            <Link
              href={`/traces/${data.gold.run_id}`}
              className="ce-arm-trace-link"
            >
              gold · {data.gold.builder_name || "?"}
            </Link>
            <span className="ce-arm-id">{data.gold.run_id.slice(0, 8)}</span>
          </div>
          <div className="ce-arm-card ce-arm-card--candidate">
            <Link
              href={`/traces/${data.candidate.run_id}`}
              className="ce-arm-trace-link"
            >
              candidate · {data.candidate.builder_name || "?"}
            </Link>
            <span className="ce-arm-id">
              {data.candidate.run_id.slice(0, 8)}
            </span>
          </div>
        </div>
      </header>

      <section className="ce-diff-section">
        <div className="ce-section-label">page diff</div>
        <div className="ce-bucket-grid">
          <PageBucket label="in gold only" pages={goldOnly} arm="gold" />
          <PageBucket label="in both" pages={both} arm="both" />
          <PageBucket
            label="in candidate only"
            pages={candOnly}
            arm="candidate"
          />
        </div>
      </section>

      <section className="ce-context-section">
        <div className="ce-section-label">side-by-side context</div>
        <div className="ce-context-grid">
          <div className="ce-context-col ce-context-col--gold">
            <div className="ce-context-col-header">
              <span className="ce-context-col-arm">gold</span>
              <ArmConfigSummary config={data.gold.config ?? {}} />
            </div>
            <ContextBuiltBody event={data.gold.context_built} />
          </div>
          <div className="ce-context-col ce-context-col--candidate">
            <div className="ce-context-col-header">
              <span className="ce-context-col-arm">candidate</span>
              <ArmConfigSummary config={data.candidate.config ?? {}} />
            </div>
            <ContextBuiltBody event={data.candidate.context_built} />
          </div>
        </div>
      </section>
    </main>
  );
}
