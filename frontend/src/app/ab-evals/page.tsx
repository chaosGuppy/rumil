import Link from "next/link";
import type { AbEvalReportListItemOut } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import "./ab-evals.css";

async function getABEvals(): Promise<AbEvalReportListItemOut[]> {
  const res = await serverFetch(`${API_BASE}/api/ab-evals`, {
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

export default async function ABEvalsPage() {
  const evals = await getABEvals();

  return (
    <main className="ab-evals-page">
      <div className="ab-evals-header">
        <h1>A/B Evaluations</h1>
        <div className="ab-evals-subtitle">comparative run analysis</div>
      </div>

      {evals.length === 0 ? (
        <div className="ab-evals-empty">
          No evaluations found.<br />
          Run an A/B evaluation to see results here.
        </div>
      ) : (
        <>
          <div className="ab-evals-count">
            {evals.length} evaluation{evals.length !== 1 ? "s" : ""}
          </div>
          <div className="ab-evals-list">
            {evals.map((ev, i) => (
              <Link
                key={ev.id}
                href={`/ab-evals/${ev.id}`}
                className="ab-eval-row"
                style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
              >
                <div className="ab-eval-row-main">
                  <div className="ab-eval-question">
                    {ev.question_headline || `${ev.run_id_a.slice(0, 8)} vs ${ev.run_id_b.slice(0, 8)}`}
                  </div>
                  {ev.overall_assessment_preview && (
                    <div className="ab-eval-preview">
                      {ev.overall_assessment_preview}
                    </div>
                  )}
                </div>
                <div className="ab-eval-row-meta">
                  <div className="ab-eval-pref-dots">
                    {ev.preferences.map((p) => (
                      <div
                        key={p.name}
                        className="ab-eval-pref-dot"
                        data-pref={prefKey(p.preference)}
                        title={`${p.display_name}: ${p.preference}`}
                      />
                    ))}
                  </div>
                  <span className="ab-eval-date">{formatDate(ev.created_at)}</span>
                </div>
              </Link>
            ))}
          </div>
        </>
      )}
    </main>
  );
}
