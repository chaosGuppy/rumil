"use client";

import Link from "next/link";
import type { ContextEvalDiffOut, PageRef } from "@/api/types.gen";
import { ContextBuiltBody, PageList } from "@/components/context-built-body";
import "../../../../traces/[runId]/trace.css";
import "./context-eval-diff.css";

type ArmKind = "gold" | "candidate";

const ARM_DESCRIPTIONS: Record<ArmKind, { tagline: string; help: string }> = {
  gold: {
    tagline: "the reference build — what we'd ideally include",
    help:
      "Produced by the expensive ImpactFilteredContext builder. " +
      "Treat it as the gold standard the candidate is being judged against.",
  },
  candidate: {
    tagline: "the builder under test — what it actually loaded",
    help:
      "The cheaper builder we're evaluating. " +
      "Compare its picks against the gold to see what it caught and what it missed.",
  },
};

function ArmHeader({
  kind,
  builderName,
  runId,
}: {
  kind: ArmKind;
  builderName: string;
  runId: string;
}) {
  const { tagline } = ARM_DESCRIPTIONS[kind];
  const roleLabel = kind === "gold" ? "Gold standard" : "Candidate";
  return (
    <div className={`ce-col-head ce-col-head--${kind}`}>
      <div className="ce-col-head-top">
        <span className={`ce-arm-pill ce-arm-pill--${kind}`}>{roleLabel}</span>
        <Link href={`/traces/${runId}`} className="ce-col-head-trace">
          trace ↗
        </Link>
      </div>
      <div className="ce-col-head-builder">{builderName || "?"}</div>
      <div className="ce-col-head-tagline">{tagline}</div>
    </div>
  );
}

type BucketKind = "missed" | "shared" | "extra";

const BUCKET_META: Record<
  BucketKind,
  { title: string; subtitle: string; valence: "bad" | "good" | "warn" }
> = {
  missed: {
    title: "Missed by candidate",
    subtitle: "in gold, not in candidate — pages the candidate failed to surface",
    valence: "bad",
  },
  shared: {
    title: "Captured by both",
    subtitle: "in gold and candidate — pages both builders agreed on",
    valence: "good",
  },
  extra: {
    title: "Only in candidate",
    subtitle: "in candidate, not in gold — extra picks the gold didn't rate",
    valence: "warn",
  },
};

function PageBucket({
  kind,
  pages,
  percentiles,
}: {
  kind: BucketKind;
  pages: PageRef[];
  percentiles: { [page_id: string]: number } | null;
}) {
  const meta = BUCKET_META[kind];
  return (
    <div className={`ce-bucket ce-bucket--${kind}`}>
      <div className="ce-bucket-head">
        <div className="ce-bucket-head-row">
          <span className="ce-bucket-title">{meta.title}</span>
          <span
            className={`ce-bucket-count ce-bucket-count--${meta.valence}`}
          >
            {pages.length}
          </span>
        </div>
        <div className="ce-bucket-subtitle">{meta.subtitle}</div>
      </div>
      <div className="ce-bucket-body">
        {pages.length === 0 ? (
          <span className="ce-bucket-empty">none</span>
        ) : (
          <PageList
            pages={pages}
            pageImpactPercentiles={percentiles}
            sortByPercentile={!!percentiles}
          />
        )}
      </div>
    </div>
  );
}

function StatStrip({
  goldTotal,
  candidateTotal,
  shared,
  missed,
  extra,
}: {
  goldTotal: number;
  candidateTotal: number;
  shared: number;
  missed: number;
  extra: number;
}) {
  const recall = goldTotal === 0 ? 0 : Math.round((shared / goldTotal) * 100);
  const precision =
    candidateTotal === 0 ? 0 : Math.round((shared / candidateTotal) * 100);
  return (
    <div className="ce-stats">
      <div className="ce-stat">
        <div className="ce-stat-value">{recall}%</div>
        <div className="ce-stat-label">candidate recall</div>
        <div className="ce-stat-help">
          {shared} of {goldTotal} gold pages captured
        </div>
      </div>
      <div className="ce-stat">
        <div className="ce-stat-value">{precision}%</div>
        <div className="ce-stat-label">candidate overlap</div>
        <div className="ce-stat-help">
          {shared} of {candidateTotal} candidate pages also in gold
        </div>
      </div>
      <div className="ce-stat ce-stat--bad">
        <div className="ce-stat-value">{missed}</div>
        <div className="ce-stat-label">missed</div>
        <div className="ce-stat-help">in gold but not in candidate</div>
      </div>
      <div className="ce-stat ce-stat--warn">
        <div className="ce-stat-value">{extra}</div>
        <div className="ce-stat-label">extra</div>
        <div className="ce-stat-help">in candidate but not in gold</div>
      </div>
    </div>
  );
}

export function ContextEvalDiff({ data }: { data: ContextEvalDiffOut }) {
  const goldOnly = data.pages_only_in_gold ?? [];
  const candOnly = data.pages_only_in_candidate ?? [];
  const both = data.pages_in_both ?? [];
  const goldTotal = goldOnly.length + both.length;
  const candidateTotal = candOnly.length + both.length;
  const goldPercentiles = data.gold.context_built.impact_percentiles ?? null;

  return (
    <main className="ce-page">
      <header className="ce-header">
        {data.question && (
          <Link href={`/pages/${data.question.id}`} className="ce-back-link">
            ← {data.question.headline}
          </Link>
        )}
        <h1 className="ce-title">Context-builder comparison</h1>
        <p className="ce-subtitle">
          One question, two context builds. The{" "}
          <span className="ce-inline-tag ce-inline-tag--gold">gold</span> arm is
          the reference; the{" "}
          <span className="ce-inline-tag ce-inline-tag--candidate">
            candidate
          </span>{" "}
          is the builder under test.
        </p>

        <div className="ce-arms">
          <div className="ce-arm ce-arm--gold">
            <div className="ce-arm-row">
              <span className="ce-arm-pill ce-arm-pill--gold">
                Gold standard
              </span>
              <Link
                href={`/traces/${data.gold.run_id}`}
                className="ce-arm-trace"
              >
                {data.gold.builder_name || "?"}
              </Link>
              <code className="ce-arm-id">
                {data.gold.run_id.slice(0, 8)}
              </code>
            </div>
            <div className="ce-arm-help">{ARM_DESCRIPTIONS.gold.help}</div>
          </div>
          <div className="ce-arm ce-arm--candidate">
            <div className="ce-arm-row">
              <span className="ce-arm-pill ce-arm-pill--candidate">
                Candidate
              </span>
              <Link
                href={`/traces/${data.candidate.run_id}`}
                className="ce-arm-trace"
              >
                {data.candidate.builder_name || "?"}
              </Link>
              <code className="ce-arm-id">
                {data.candidate.run_id.slice(0, 8)}
              </code>
            </div>
            <div className="ce-arm-help">
              {ARM_DESCRIPTIONS.candidate.help}
            </div>
          </div>
        </div>
      </header>

      <StatStrip
        goldTotal={goldTotal}
        candidateTotal={candidateTotal}
        shared={both.length}
        missed={goldOnly.length}
        extra={candOnly.length}
      />

      <section className="ce-section">
        <div className="ce-section-head">
          <h2 className="ce-section-title">Page diff</h2>
          <p className="ce-section-help">
            Union of working-context, preloaded, and scope-linked pages from
            each arm, bucketed by where they appear.
          </p>
        </div>
        <div className="ce-bucket-grid">
          <PageBucket
            kind="missed"
            pages={goldOnly}
            percentiles={goldPercentiles}
          />
          <PageBucket
            kind="shared"
            pages={both}
            percentiles={goldPercentiles}
          />
          <PageBucket
            kind="extra"
            pages={candOnly}
            percentiles={goldPercentiles}
          />
        </div>
      </section>

      <section className="ce-section">
        <div className="ce-section-head">
          <h2 className="ce-section-title">Side-by-side context</h2>
          <p className="ce-section-help">
            Tiered breakdown of what each builder actually loaded — pages,
            char budgets, and (expandable) the rendered prompt text.
          </p>
        </div>
        <div className="ce-context-grid">
          <div className="ce-context-col ce-context-col--gold">
            <ArmHeader
              kind="gold"
              builderName={data.gold.builder_name}
              runId={data.gold.run_id}
            />
            <div className="ce-context-body">
              <ContextBuiltBody event={data.gold.context_built} />
            </div>
          </div>
          <div className="ce-context-col ce-context-col--candidate">
            <ArmHeader
              kind="candidate"
              builderName={data.candidate.builder_name}
              runId={data.candidate.run_id}
            />
            <div className="ce-context-body">
              <ContextBuiltBody
                event={data.candidate.context_built}
                pageImpactPercentiles={goldPercentiles}
                sortByPercentile={!!goldPercentiles}
              />
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
