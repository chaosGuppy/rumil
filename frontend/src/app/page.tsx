import Link from "next/link";
import type { Project } from "@/api";
import { API_BASE, serverFetch } from "@/lib/api-base";

async function getProjects(): Promise<Project[]> {
  const res = await serverFetch(`${API_BASE}/api/projects`, {
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
  });
}

function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diff = now - then;
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return `${months}mo ago`;
}

export default async function Home() {
  const projects = await getProjects();

  return (
    <main className="workspace-index">
      <style>{`
        .workspace-index {
          max-width: 54rem;
          margin: 0 auto;
          padding: 2rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }

        .ws-header {
          margin-bottom: 2.5rem;
        }
        .ws-header h1 {
          font-size: 1.6rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          margin: 0 0 0.25rem 0;
        }
        .ws-header .subtitle {
          font-size: 0.85rem;
          color: var(--color-muted, #888);
          font-family: var(--font-geist-mono), monospace;
        }

        .ws-results-meta {
          font-size: 0.75rem;
          color: var(--color-muted, #888);
          margin-bottom: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
        }

        .ws-grid {
          display: grid;
          gap: 1px;
          background: var(--color-border, #ddd);
          border: 1px solid var(--color-border, #ddd);
          overflow: hidden;
        }

        .ws-row {
          display: grid;
          grid-template-columns: 1fr auto;
          align-items: center;
          gap: 1.5rem;
          padding: 0.75rem 0.85rem;
          background: var(--color-background, #fff);
          text-decoration: none;
          color: inherit;
          transition: background 0.1s ease;
          animation: wsRowFadeIn 0.2s ease both;
        }
        .ws-row:hover {
          background: var(--color-surface, #f6f6f6);
        }

        @keyframes wsRowFadeIn {
          from { opacity: 0; transform: translateY(2px); }
          to { opacity: 1; transform: translateY(0); }
        }

        .ws-row-name {
          font-size: 0.9rem;
          font-weight: 500;
          line-height: 1.35;
        }

        .ws-row-meta {
          display: flex;
          align-items: center;
          gap: 1rem;
          white-space: nowrap;
        }
        .ws-row-date {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted, #888);
          opacity: 0.7;
        }
        .ws-row-id {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          color: var(--color-muted, #888);
          opacity: 0.5;
        }

        .ws-empty {
          padding: 4rem 1rem;
          text-align: center;
          color: var(--color-muted, #888);
          font-size: 0.85rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.02em;
          line-height: 1.6;
        }

        @media (prefers-color-scheme: dark) {
          .workspace-index {
            --color-muted: #6a6a6a;
            --color-border: #222;
            --color-surface: #111;
            --color-background: #0a0a0a;
          }
        }
        @media (prefers-color-scheme: light) {
          .workspace-index {
            --color-muted: #888;
            --color-border: #ddd;
            --color-surface: #f6f6f6;
            --color-background: #fff;
          }
        }
      `}</style>

      <div className="ws-header">
        <h1>Rumil</h1>
        <div className="subtitle">workspaces</div>
      </div>

      {projects.length === 0 ? (
        <div className="ws-empty">
          No workspaces found.<br />
          Make sure the API server is running and the database has data.
        </div>
      ) : (
        <>
          <div className="ws-results-meta">
            {projects.length} workspace{projects.length !== 1 ? "s" : ""}
          </div>
          <div className="ws-grid">
            {projects.map((project, i) => (
              <Link
                key={project.id}
                href={`/projects/${project.id}`}
                className="ws-row"
                style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
              >
                <span className="ws-row-name">{project.name}</span>
                <div className="ws-row-meta">
                  <span className="ws-row-date">
                    {formatDate(project.created_at)}
                  </span>
                  <span className="ws-row-id">
                    {project.id.slice(0, 8)}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        </>
      )}
    </main>
  );
}
