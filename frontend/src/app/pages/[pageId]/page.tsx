import Link from "next/link";
import type { PageDetailOut, LinkedPageOut, Page } from "@/api";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function getPageDetail(pageId: string): Promise<PageDetailOut | null> {
  const res = await fetch(`${API_BASE}/api/pages/${pageId}/detail`, {
    cache: "no-store",
  });
  if (!res.ok) return null;
  return res.json();
}

function pageHref(page: Page): string {
  if (page.page_type === "question") return `/questions/${page.id}`;
  return `/pages/${page.id}`;
}

function pageTypeBadge(pageType: string) {
  const colors: Record<string, string> = {
    claim: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
    question: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
    judgement: "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    source: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200",
    concept: "bg-teal-100 text-teal-800 dark:bg-teal-900 dark:text-teal-200",
    wiki: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  };
  const cls = colors[pageType] || "bg-gray-100 text-gray-800";
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded ${cls}`}>
      {pageType}
    </span>
  );
}

function directionColor(direction: string | null) {
  if (direction === "supports") return "text-green-600";
  if (direction === "opposes") return "text-red-600";
  return "text-gray-500";
}

function LinkedPageCard({ lp }: { lp: LinkedPageOut }) {
  return (
    <div className="border rounded p-3">
      <div className="flex items-center gap-2 mb-1">
        {pageTypeBadge(lp.page.page_type)}
        {lp.link.direction && (
          <span className={`text-xs font-medium ${directionColor(lp.link.direction)}`}>
            {lp.link.direction}
          </span>
        )}
        {lp.link.strength > 0 && (
          <span className="text-gray-400 text-xs">
            strength {lp.link.strength}/5
          </span>
        )}
        <span className="text-gray-400 text-xs">{lp.link.link_type}</span>
      </div>
      <Link href={pageHref(lp.page)} className="text-sm font-medium hover:underline">
        {lp.page.summary}
      </Link>
      {lp.link.reasoning && (
        <p className="text-xs text-gray-500 mt-1 line-clamp-2">{lp.link.reasoning}</p>
      )}
    </div>
  );
}

function ClaimDetail({ detail }: { detail: PageDetailOut }) {
  const { page, links_from, links_to } = detail;
  const questionsBearedOn = links_from.filter(
    (lp) => lp.link.link_type === "consideration",
  );
  const otherOutgoing = links_from.filter(
    (lp) => lp.link.link_type !== "consideration",
  );
  const incoming = links_to;

  return (
    <>
      <div className="mb-6">
        <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
          {page.content}
        </p>
        <div className="mt-3 flex gap-4 text-xs text-gray-500">
          <span>Epistemic: {page.epistemic_status}/5 ({page.epistemic_type})</span>
          <span>Layer: {page.layer}</span>
        </div>
      </div>

      {questionsBearedOn.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Bears on ({questionsBearedOn.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {questionsBearedOn.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}

      {otherOutgoing.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Links from this claim ({otherOutgoing.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {otherOutgoing.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}

      {incoming.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Linked from ({incoming.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {incoming.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function JudgementDetail({ detail }: { detail: PageDetailOut }) {
  const { page, links_from, links_to } = detail;
  const questions = links_from.filter(
    (lp) => lp.page.page_type === "question",
  );
  const otherOutgoing = links_from.filter(
    (lp) => lp.page.page_type !== "question",
  );
  const incoming = links_to;

  return (
    <>
      <div className="mb-6">
        <div className="border border-amber-300 rounded p-4 bg-amber-50 dark:bg-amber-950">
          <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {page.content}
          </p>
          <div className="mt-3 flex gap-4 text-xs text-gray-500">
            <span>Epistemic: {page.epistemic_status}/5 ({page.epistemic_type})</span>
            <span>Layer: {page.layer}</span>
          </div>
        </div>
      </div>

      {questions.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-amber-700 mb-2">
            Judges ({questions.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {questions.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}

      {otherOutgoing.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Links from this judgement ({otherOutgoing.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {otherOutgoing.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}

      {incoming.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Linked from ({incoming.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {incoming.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function GenericDetail({ detail }: { detail: PageDetailOut }) {
  const { page, links_from, links_to } = detail;

  return (
    <>
      <div className="mb-6">
        <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
          {page.content}
        </p>
        <div className="mt-3 flex gap-4 text-xs text-gray-500">
          <span>Epistemic: {page.epistemic_status}/5 ({page.epistemic_type})</span>
          <span>Layer: {page.layer}</span>
        </div>
      </div>

      {links_from.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Outgoing links ({links_from.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {links_from.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}

      {links_to.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Incoming links ({links_to.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {links_to.map((lp) => (
              <LinkedPageCard key={lp.link.id} lp={lp} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

export default async function PageDetailPage({
  params,
}: {
  params: Promise<{ pageId: string }>;
}) {
  const { pageId } = await params;
  const detail = await getPageDetail(pageId);

  if (!detail) {
    return (
      <main className="max-w-4xl mx-auto p-8">
        <p className="text-red-500">Page not found.</p>
        <Link href="/" className="text-blue-600 hover:underline text-sm">
          &larr; Back
        </Link>
      </main>
    );
  }

  const { page } = detail;

  return (
    <main className="max-w-4xl mx-auto p-8">
      <Link
        href={`/projects/${page.project_id}`}
        className="text-blue-600 hover:underline text-sm"
      >
        &larr; Project
      </Link>
      <div className="mt-4">
        <div className="flex items-center gap-3 mb-2">
          {pageTypeBadge(page.page_type)}
          <span className="text-xs text-gray-400 font-mono">{page.id.slice(0, 8)}</span>
          {page.is_superseded && (
            <span className="text-xs text-red-500 font-medium">superseded</span>
          )}
        </div>
        <h1 className="text-2xl font-semibold mb-4">{page.summary}</h1>

        {page.page_type === "claim" && <ClaimDetail detail={detail} />}
        {page.page_type === "judgement" && <JudgementDetail detail={detail} />}
        {page.page_type !== "claim" && page.page_type !== "judgement" && (
          <GenericDetail detail={detail} />
        )}

        <div className="mt-6 pt-4 border-t text-xs text-gray-400 space-y-1">
          <p>Created: {new Date(page.created_at).toLocaleString()}</p>
          <p>Provenance: {page.provenance_call_type} via {page.provenance_model}</p>
          {page.superseded_by && (
            <p>
              Superseded by:{" "}
              <Link
                href={`/pages/${page.superseded_by}`}
                className="text-blue-600 hover:underline font-mono"
              >
                {page.superseded_by.slice(0, 8)}
              </Link>
            </p>
          )}
        </div>
      </div>
    </main>
  );
}
