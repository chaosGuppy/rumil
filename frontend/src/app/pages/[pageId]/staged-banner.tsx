"use client";

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

  return (
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
  );
}
