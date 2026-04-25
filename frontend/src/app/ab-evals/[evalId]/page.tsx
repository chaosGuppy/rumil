import Link from "next/link";
import type { Metadata } from "next";
import type { AbEvalReportOut } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { EvalDetail } from "./eval-detail";
import { truncateHeadline } from "@/lib/page-titles";
import "../ab-evals.css";

async function getABEval(evalId: string): Promise<AbEvalReportOut | null> {
  const res = await serverFetch(`${API_BASE}/api/ab-evals/${evalId}`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ evalId: string }>;
}): Promise<Metadata> {
  const { evalId } = await params;
  const report = await getABEval(evalId);
  if (!report) return { title: `eval ${evalId.slice(0, 8)}` };
  const headline = truncateHeadline(report.question_headline, 45);
  const label = headline || evalId.slice(0, 8);
  return { title: `eval "${label}"` };
}

export default async function ABEvalDetailPage({
  params,
}: {
  params: Promise<{ evalId: string }>;
}) {
  const { evalId } = await params;
  const report = await getABEval(evalId);

  if (!report) {
    return (
      <main className="ab-eval-detail">
        <Link href="/ab-evals" className="ab-eval-back">
          &larr; Back to evaluations
        </Link>
        <p>Evaluation not found: {evalId}</p>
      </main>
    );
  }

  return (
    <main className="ab-eval-detail">
      <Link href="/ab-evals" className="ab-eval-back">
        &larr; All evaluations
      </Link>
      <EvalDetail report={report} />
    </main>
  );
}
