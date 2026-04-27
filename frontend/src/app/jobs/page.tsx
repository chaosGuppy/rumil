import type { Metadata } from "next";
import type { JobListItem } from "@/api/types.gen";

type JobStatus = JobListItem["status"];
import { API_BASE, serverFetch } from "@/lib/api-base";
import "./jobs.css";

export const metadata: Metadata = {
  title: "Jobs",
};

async function getJobs(): Promise<{ items: JobListItem[]; error: string | null }> {
  const res = await serverFetch(`${API_BASE}/api/jobs`, { cache: "no-store" });
  if (!res.ok) {
    return { items: [], error: `Failed to load jobs (${res.status})` };
  }
  const items = (await res.json()) as JobListItem[];
  return { items, error: null };
}

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "queued",
  running: "running",
  failed: "failed",
  completed: "complete",
};

function statusColorVar(status: JobStatus): string {
  return `var(--status-${status})`;
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const diff = Date.now() - then;
  if (diff < 0) return "in the future";
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function statusTime(job: JobListItem): { label: string; iso: string | null } {
  if (job.status === "running" || job.status === "pending") {
    return { label: "started", iso: job.started_at ?? null };
  }
  return { label: "ended", iso: job.completed_at ?? null };
}

function durationLabel(job: JobListItem): string | null {
  const start = job.started_at ? new Date(job.started_at).getTime() : null;
  if (!start) return null;
  const end = job.completed_at ? new Date(job.completed_at).getTime() : Date.now();
  const secs = Math.floor((end - start) / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = secs % 60;
  if (mins < 60) return `${mins}m ${remSecs}s`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hrs}h ${remMins}m`;
}

export default async function JobsPage() {
  const { items, error } = await getJobs();

  return (
    <main className="jobs-page">
      <div className="jobs-header">
        <div>
          <h1>Jobs</h1>
          <div className="jobs-subtitle">orchestrator runs · k8s</div>
        </div>
        {items.length > 0 && (
          <div className="jobs-count">
            {items.length} job{items.length !== 1 ? "s" : ""}
          </div>
        )}
      </div>

      {error && <div className="jobs-error">{error}</div>}

      {!error && items.length === 0 && (
        <div className="jobs-empty">
          No jobs in the cluster.<br />
          Submit one with{" "}
          <code>uv run main.py &quot;...&quot; --executor prod --budget N</code>.
        </div>
      )}

      {!error && items.length > 0 && (
        <div className="jobs-list">
          {items.map((job, i) => {
            const { label: timeLabel, iso: timeIso } = statusTime(job);
            const duration = durationLabel(job);
            const logsDisabled = !job.logs_url;

            return (
              <div
                key={job.job_name}
                className="jobs-row"
                data-status={job.status}
                style={
                  {
                    animationDelay: `${Math.min(i * 25, 250)}ms`,
                    "--row-status-color": statusColorVar(job.status),
                  } as React.CSSProperties
                }
              >
                <div className="jobs-status">
                  <span className="jobs-status-label">{STATUS_LABEL[job.status]}</span>
                  {duration && <span className="jobs-status-time">{duration}</span>}
                </div>

                <div className="jobs-row-main">
                  <div className="jobs-row-title">
                    <span className="jobs-row-workspace">{job.workspace}</span>
                    <span className="jobs-row-sep">·</span>
                    <span className="jobs-row-question" title={job.question}>
                      {job.question}
                    </span>
                  </div>
                  <div className="jobs-row-meta">
                    <span className="jobs-row-meta-item" title={job.job_name}>
                      {job.job_name}
                    </span>
                    <span className="jobs-row-meta-item" title={job.run_id}>
                      run {job.run_id.slice(0, 8)}
                    </span>
                  </div>
                </div>

                <div className="jobs-row-time">
                  <span className="jobs-row-time-relative">
                    {relativeTime(timeIso ?? job.created_at)}
                  </span>
                  <span className="jobs-row-time-absolute">
                    {timeLabel} {absoluteTime(timeIso ?? job.created_at)}
                  </span>
                </div>

                <div className="jobs-row-actions">
                  <a className="jobs-row-link" href={job.trace_url} title="View trace">
                    trace
                  </a>
                  <a
                    className="jobs-row-link"
                    href={logsDisabled ? undefined : job.logs_url}
                    data-disabled={logsDisabled}
                    aria-disabled={logsDisabled}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={logsDisabled ? "GCP project not configured" : "View pod logs"}
                  >
                    logs
                  </a>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </main>
  );
}
