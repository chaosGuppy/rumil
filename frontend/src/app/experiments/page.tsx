import Link from "next/link";
import type { Metadata } from "next";
import type { ListExperimentsApiExperimentsGetResponse } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import "./experiments.css";

export const metadata: Metadata = {
  title: "Experiments",
};

type Experiment = ListExperimentsApiExperimentsGetResponse[number];
type AbEvalExperiment = Extract<Experiment, { kind: "ab_eval" }>;
type RunCallExperiment = Extract<Experiment, { kind: "run_call" }>;
type ContextEvalExperiment = Extract<Experiment, { kind: "context_eval" }>;

async function getExperiments(): Promise<Experiment[]> {
  const res = await serverFetch(`${API_BASE}/api/experiments`, {
    cache: "no-store",
  });
  if (!res.ok) return [];
  return res.json();
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

function AbEvalRow({ ev, delay }: { ev: AbEvalExperiment; delay: number }) {
  const fallback = `${ev.run_id_a.slice(0, 8)} vs ${ev.run_id_b.slice(0, 8)}`;
  return (
    <Link
      href={`/ab-evals/${ev.id}`}
      className="experiment-row"
      style={{ animationDelay: `${delay}ms` }}
    >
      <span className="experiment-kind-tag" data-kind="ab_eval">A/B</span>
      <div className="experiment-row-main">
        <div className="experiment-question">
          {ev.question_headline || fallback}
        </div>
        {ev.overall_assessment_preview && (
          <div className="experiment-preview">{ev.overall_assessment_preview}</div>
        )}
      </div>
      <div className="experiment-row-meta">
        <div className="experiment-pref-dots">
          {ev.preferences.map((p) => (
            <div
              key={p.name}
              className="experiment-pref-dot"
              data-pref={prefKey(p.preference)}
              title={`${p.display_name}: ${p.preference}`}
            />
          ))}
        </div>
        <span className="experiment-date">{formatDate(ev.created_at)}</span>
      </div>
    </Link>
  );
}

function ContextEvalRow({
  ev,
  delay,
}: {
  ev: ContextEvalExperiment;
  delay: number;
}) {
  const fallback = `${ev.gold_run_id.slice(0, 8)} vs ${ev.candidate_run_id.slice(0, 8)}`;
  return (
    <Link
      href={`/context-evals/${ev.gold_run_id}/vs/${ev.candidate_run_id}`}
      className="experiment-row"
      style={{ animationDelay: `${delay}ms` }}
    >
      <span className="experiment-kind-tag" data-kind="context_eval">
        CTX
      </span>
      <div className="experiment-row-main">
        <div className="experiment-question">
          {ev.question_headline || fallback}
        </div>
        <div className="experiment-ctx-builders">
          <span className="experiment-ctx-builder experiment-ctx-builder--gold">
            <span className="experiment-ctx-builder-tag">gold</span>
            {ev.gold_builder || "?"}
          </span>
          <span className="experiment-ctx-builder-vs">vs</span>
          <span className="experiment-ctx-builder experiment-ctx-builder--candidate">
            <span className="experiment-ctx-builder-tag">candidate</span>
            {ev.candidate_builder || "?"}
          </span>
        </div>
      </div>
      <div className="experiment-row-meta">
        <span className="experiment-date">{formatDate(ev.created_at)}</span>
      </div>
    </Link>
  );
}

function RunCallRow({ ev, delay }: { ev: RunCallExperiment; delay: number }) {
  const title = ev.question_headline || ev.name || ev.run_id.slice(0, 8);
  const subtitle = ev.question_headline && ev.name && ev.name !== ev.question_headline
    ? ev.name
    : null;
  const cfg = ev.config_summary ?? {};
  const cfgEntries = Object.entries(cfg).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return (
    <Link
      href={`/traces/${ev.run_id}`}
      className="experiment-row"
      style={{ animationDelay: `${delay}ms` }}
    >
      <span className="experiment-kind-tag" data-kind="run_call">RUN</span>
      <div className="experiment-row-main">
        <div className="experiment-question">
          {title}
          {ev.staged && <span className="experiment-staged-chip">staged</span>}
        </div>
        {subtitle && <div className="experiment-preview">{subtitle}</div>}
        {cfgEntries.length > 0 && (
          <div className="experiment-config">
            {cfgEntries.map(([k, v]) => (
              <span key={k} className="experiment-config-item">
                <span className="experiment-config-key">{k}</span>
                <span>{String(v)}</span>
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="experiment-row-meta">
        <span className="experiment-date">{formatDate(ev.created_at)}</span>
      </div>
    </Link>
  );
}

export default async function ExperimentsPage() {
  const experiments = await getExperiments();

  return (
    <main className="experiments-page">
      <div className="experiments-header">
        <h1>Experiments</h1>
        <div className="experiments-subtitle">research run feed</div>
      </div>

      {experiments.length === 0 ? (
        <div className="experiments-empty">
          No experiments found.<br />
          Run an A/B evaluation or a single call to see results here.
        </div>
      ) : (
        <>
          <div className="experiments-count">
            {experiments.length} experiment{experiments.length !== 1 ? "s" : ""}
          </div>
          <div className="experiments-list">
            {experiments.map((ev, i) => {
              const delay = Math.min(i * 30, 300);
              if (ev.kind === "run_call") {
                return <RunCallRow key={`run-${ev.run_id}`} ev={ev} delay={delay} />;
              }
              if (ev.kind === "context_eval") {
                return (
                  <ContextEvalRow
                    key={`ctx-${ev.gold_run_id}`}
                    ev={ev}
                    delay={delay}
                  />
                );
              }
              return <AbEvalRow key={`ab-${ev.id}`} ev={ev} delay={delay} />;
            })}
          </div>
        </>
      )}
    </main>
  );
}
