"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useStagedRun } from "@/lib/staged-run-context";

export default function StagedBanner({
  runId,
  pageUrl,
}: {
  runId: string;
  pageUrl: string;
}) {
  const { setActiveStagedRunId } = useStagedRun();
  const router = useRouter();

  useEffect(() => {
    setActiveStagedRunId(runId);
  }, [runId, setActiveStagedRunId]);

  return (
    <>
      <style>{`
        .staged-banner {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          padding: 0.45rem 0.75rem;
          margin-bottom: 1rem;
          background: rgba(90, 138, 122, 0.06);
          border: 1px solid rgba(90, 138, 122, 0.2);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
          color: #5a8a7a;
          animation: bannerSlideIn 0.2s ease both;
        }
        @keyframes bannerSlideIn {
          from { opacity: 0; transform: translateY(-4px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .staged-banner-indicator {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: #5a8a7a;
          flex-shrink: 0;
          animation: indicatorPulse 2s ease infinite;
        }
        @keyframes indicatorPulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }
        .staged-banner-text {
          flex: 1;
          letter-spacing: 0.02em;
        }
        .staged-banner-clear {
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: #5a8a7a;
          background: none;
          border: none;
          cursor: pointer;
          padding: 0.2rem 0.4rem;
          opacity: 0.7;
          transition: opacity 0.12s ease;
        }
        .staged-banner-clear:hover {
          opacity: 1;
        }
      `}</style>
      <div className="staged-banner">
        <span className="staged-banner-indicator" />
        <span className="staged-banner-text">
          Viewing staged run {runId.slice(0, 8)}
        </span>
        <button
          className="staged-banner-clear"
          onClick={() => {
            setActiveStagedRunId(null);
            router.replace(pageUrl);
          }}
        >
          Clear
        </button>
      </div>
    </>
  );
}
