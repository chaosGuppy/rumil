import Link from "next/link";
import type { Metadata } from "next";
import type { ContextEvalDiffOut } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { truncateHeadline } from "@/lib/page-titles";
import { ContextEvalDiff } from "./eval-diff";

async function getDiff(
  goldRunId: string,
  candidateRunId: string,
): Promise<ContextEvalDiffOut | null> {
  const res = await serverFetch(
    `${API_BASE}/api/context-evals/${goldRunId}/vs/${candidateRunId}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return res.json();
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ goldRunId: string; candidateRunId: string }>;
}): Promise<Metadata> {
  const { goldRunId, candidateRunId } = await params;
  const diff = await getDiff(goldRunId, candidateRunId);
  if (!diff) {
    return {
      title: `context-eval ${goldRunId.slice(0, 8)} vs ${candidateRunId.slice(0, 8)}`,
    };
  }
  const headline = truncateHeadline(diff.question?.headline, 40);
  const label = headline || `${goldRunId.slice(0, 8)} vs ${candidateRunId.slice(0, 8)}`;
  return { title: `context-eval "${label}"` };
}

export default async function ContextEvalPage({
  params,
}: {
  params: Promise<{ goldRunId: string; candidateRunId: string }>;
}) {
  const { goldRunId, candidateRunId } = await params;
  const diff = await getDiff(goldRunId, candidateRunId);

  if (!diff) {
    return (
      <main className="ce-page">
        <Link href="/" className="ce-back-link">
          &larr; Home
        </Link>
        <p>Context-eval not found: {goldRunId} vs {candidateRunId}</p>
      </main>
    );
  }

  return <ContextEvalDiff data={diff} />;
}
