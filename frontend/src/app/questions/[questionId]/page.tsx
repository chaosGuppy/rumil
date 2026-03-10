import Link from "next/link";
import type { QuestionTreeOut, ConsiderationOut, PageOut } from "@/api";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function getQuestionTree(
  questionId: string,
): Promise<QuestionTreeOut | null> {
  const res = await fetch(
    `${API_BASE}/api/questions/${questionId}/tree?depth=3`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return res.json();
}

function directionColor(direction: string | null) {
  if (direction === "supports") return "text-green-600";
  if (direction === "opposes") return "text-red-600";
  return "text-gray-500";
}

function ConsiderationCard({ c }: { c: ConsiderationOut }) {
  return (
    <div className="border rounded p-3">
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-sm font-medium ${directionColor(c.link.direction)}`}>
          {c.link.direction || "neutral"}
        </span>
        <span className="text-gray-400 text-xs">
          strength {c.link.strength}/5
        </span>
      </div>
      <p className="text-sm">{c.page.summary}</p>
      <p className="text-xs text-gray-500 mt-1 line-clamp-3">{c.page.content}</p>
    </div>
  );
}

function JudgementCard({ j }: { j: PageOut }) {
  return (
    <div className="border border-amber-300 rounded p-3 bg-amber-50 dark:bg-amber-950">
      <p className="text-sm font-medium">{j.summary}</p>
      <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
        Epistemic status: {j.epistemic_status}/5
      </p>
      <p className="text-xs text-gray-500 mt-1 line-clamp-3">{j.content}</p>
    </div>
  );
}

function QuestionNode({ tree, depth = 0 }: { tree: QuestionTreeOut; depth?: number }) {
  return (
    <div className={depth > 0 ? "ml-6 mt-4 border-l-2 border-gray-200 pl-4" : ""}>
      <h2 className={`font-semibold ${depth === 0 ? "text-2xl mb-2" : "text-lg mb-1"}`}>
        {tree.question.summary}
      </h2>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        {tree.question.content}
      </p>

      {tree.judgements.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-semibold text-amber-700 mb-2">Judgements</h3>
          <div className="space-y-2">
            {tree.judgements.map((j) => (
              <JudgementCard key={j.id} j={j} />
            ))}
          </div>
        </div>
      )}

      {tree.considerations.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Considerations ({tree.considerations.length})
          </h3>
          <div className="grid gap-2 sm:grid-cols-2">
            {tree.considerations.map((c) => (
              <ConsiderationCard key={c.link.id} c={c} />
            ))}
          </div>
        </div>
      )}

      {tree.child_questions.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
            Sub-questions ({tree.child_questions.length})
          </h3>
          {tree.child_questions.map((child) => (
            <QuestionNode key={child.question.id} tree={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export default async function QuestionPage({
  params,
}: {
  params: Promise<{ questionId: string }>;
}) {
  const { questionId } = await params;
  const tree = await getQuestionTree(questionId);

  if (!tree) {
    return (
      <main className="max-w-4xl mx-auto p-8">
        <p className="text-red-500">Question not found.</p>
        <Link href="/" className="text-blue-600 hover:underline text-sm">
          &larr; Back
        </Link>
      </main>
    );
  }

  return (
    <main className="max-w-4xl mx-auto p-8">
      <Link
        href={`/projects/${tree.question.project_id}`}
        className="text-blue-600 hover:underline text-sm"
      >
        &larr; Questions
      </Link>
      <div className="mt-4">
        <QuestionNode tree={tree} />
      </div>
    </main>
  );
}
