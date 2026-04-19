"use client";

import { useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AbEvalReportOut, AbEvalDimensionOut } from "@/api/types.gen";

function prefKey(preference: string): string {
  const p = preference.toLowerCase();
  if (p.includes("a strongly")) return "a-strong";
  if (p.includes("a somewhat")) return "a-somewhat";
  if (p.includes("a slightly")) return "a-slight";
  if (p.includes("indifferent")) return "indifferent";
  if (p.includes("b slightly")) return "b-slight";
  if (p.includes("b somewhat")) return "b-somewhat";
  if (p.includes("b strongly")) return "b-strong";
  return "unknown";
}

function prefLabel(preference: string): string {
  const p = preference.toLowerCase();
  if (p.includes("a strongly")) return "A++";
  if (p.includes("a somewhat")) return "A+";
  if (p.includes("a slightly")) return "A";
  if (p.includes("indifferent")) return "—";
  if (p.includes("b slightly")) return "B";
  if (p.includes("b somewhat")) return "B+";
  if (p.includes("b strongly")) return "B++";
  return "?";
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

type DimTab = "comparison" | "report_a" | "report_b";

function traceHref(evalRunId: string | undefined, callId: string): string {
  if (evalRunId) return `/traces/${evalRunId}#call-${callId.slice(0, 8)}`;
  return `/traces/${callId}`;
}

function DimensionSection({ dim, evalRunId }: { dim: AbEvalDimensionOut; evalRunId?: string }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<DimTab>("comparison");

  const content =
    tab === "comparison"
      ? dim.comparison
      : tab === "report_a"
        ? dim.report_a
        : dim.report_b;

  return (
    <div className="ab-eval-dim" data-open={open}>
      <div className="ab-eval-dim-header" onClick={() => setOpen(!open)}>
        <span className="ab-eval-dim-toggle">&#9654;</span>
        <span className="ab-eval-dim-name">{dim.display_name}</span>
        <span
          className="ab-eval-pref-badge"
          data-pref={prefKey(dim.preference)}
        >
          {prefLabel(dim.preference)}
        </span>
      </div>
      <div className="ab-eval-dim-body">
        <div className="ab-eval-dim-tabs">
          {(
            [
              ["comparison", "Comparison"],
              ["report_a", "Run A Report"],
              ["report_b", "Run B Report"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              className="ab-eval-dim-tab"
              data-active={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="ab-eval-dim-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
        {(dim.call_id_a || dim.call_id_b || dim.comparison_call_id) && (
          <div className="ab-eval-dim-eval-links">
            {dim.call_id_a && (
              <Link
                href={traceHref(evalRunId, dim.call_id_a)}
                className="ab-eval-dim-eval-link"
              >
                eval trace A
              </Link>
            )}
            {dim.call_id_b && (
              <Link
                href={traceHref(evalRunId, dim.call_id_b)}
                className="ab-eval-dim-eval-link"
              >
                eval trace B
              </Link>
            )}
            {dim.comparison_call_id && (
              <Link
                href={traceHref(evalRunId, dim.comparison_call_id)}
                className="ab-eval-dim-eval-link"
              >
                comparison trace
              </Link>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ConfigDiff({
  configA,
  configB,
}: {
  configA: Record<string, unknown>;
  configB: Record<string, unknown>;
}) {
  const allKeys = Array.from(
    new Set([...Object.keys(configA), ...Object.keys(configB)]),
  ).sort();
  if (allKeys.length === 0) return null;

  return (
    <div className="ab-eval-config-diff">
      <div className="ab-eval-config-table">
        <div className="ab-eval-config-row ab-eval-config-header-row">
          <span className="ab-eval-config-key">param</span>
          <span className="ab-eval-config-val">arm a</span>
          <span className="ab-eval-config-val">arm b</span>
        </div>
        {allKeys.map((k) => {
          const a = String(configA[k] ?? "—");
          const b = String(configB[k] ?? "—");
          const differs = a !== b;
          return (
            <div
              key={k}
              className={`ab-eval-config-row${differs ? " ab-eval-config-diff-row" : ""}`}
            >
              <span className="ab-eval-config-key">{k}</span>
              <span
                className={`ab-eval-config-val${differs ? " ab-eval-config-val-diff" : ""}`}
              >
                {a}
              </span>
              <span
                className={`ab-eval-config-val${differs ? " ab-eval-config-val-diff" : ""}`}
              >
                {b}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function EvalDetail({ report }: { report: AbEvalReportOut }) {
  const hasConfig =
    Object.keys(report.config_a ?? {}).length > 0 ||
    Object.keys(report.config_b ?? {}).length > 0;

  return (
    <>
      <header className="ab-eval-detail-header">
        <h1>
          {report.question_headline ||
            `${report.run_id_a.slice(0, 8)} vs ${report.run_id_b.slice(0, 8)}`}
        </h1>
        <div className="ab-eval-meta">{formatDate(report.created_at)}</div>
      </header>

      <section className="ab-eval-traces-section">
        <div className="ab-eval-section-label">traces</div>
        <div className="ab-eval-runs-bar">
          <Link
            href={`/traces/${report.run_id_a}`}
            className="ab-eval-run-link"
          >
            <span className="ab-eval-run-label" data-arm="a">
              A
            </span>
            {report.run_id_a.slice(0, 8)}
          </Link>
          <Link
            href={`/traces/${report.run_id_b}`}
            className="ab-eval-run-link"
          >
            <span className="ab-eval-run-label" data-arm="b">
              B
            </span>
            {report.run_id_b.slice(0, 8)}
          </Link>
        </div>
      </section>

      {hasConfig && (
        <section className="ab-eval-config-section">
          <div className="ab-eval-section-label">configuration</div>
          <ConfigDiff
            configA={report.config_a ?? {}}
            configB={report.config_b ?? {}}
          />
        </section>
      )}

      <section className="ab-eval-pref-summary">
        <div className="ab-eval-section-label">preferences</div>
        <div className="ab-eval-pref-grid">
          {report.dimension_reports.map((dim) => (
            <div key={dim.name} className="ab-eval-pref-card">
              <div className="ab-eval-pref-card-name">{dim.display_name}</div>
              <span
                className="ab-eval-pref-badge"
                data-pref={prefKey(dim.preference)}
              >
                {dim.preference}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="ab-eval-assessment">
        <div className="ab-eval-section-label">overall assessment</div>
        <div className="ab-eval-assessment-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {report.overall_assessment}
          </ReactMarkdown>
        </div>
        {report.overall_assessment_call_id && (
          <div className="ab-eval-dim-eval-links">
            <Link
              href={traceHref(report.eval_run_id, report.overall_assessment_call_id)}
              className="ab-eval-dim-eval-link"
            >
              overall assessment trace
            </Link>
          </div>
        )}
      </section>

      <section>
        <div className="ab-eval-section-label">dimension reports</div>
        <div className="ab-eval-dimensions">
          {report.dimension_reports.map((dim) => (
            <DimensionSection key={dim.name} dim={dim} evalRunId={report.eval_run_id} />
          ))}
        </div>
      </section>
    </>
  );
}
