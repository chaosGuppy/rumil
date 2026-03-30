import Link from "next/link";

export function WorkspaceIndicator({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName?: string;
}) {
  return (
    <>
      <style>{`
        .ws-indicator {
          display: flex;
          align-items: center;
          gap: 0.4rem;
          margin-bottom: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.65rem;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: var(--color-dim);
          user-select: none;
        }
        .ws-indicator a {
          color: inherit;
          text-decoration: none;
          transition: color 0.15s;
        }
        .ws-indicator a:hover {
          color: var(--color-muted);
        }
        .ws-indicator-sep {
          opacity: 0.4;
        }
      `}</style>
      <div className="ws-indicator">
        <Link href="/">rumil</Link>
        <span className="ws-indicator-sep">/</span>
        <Link href={`/projects/${projectId}`}>
          {projectName ?? projectId.slice(0, 8)}
        </Link>
      </div>
    </>
  );
}
