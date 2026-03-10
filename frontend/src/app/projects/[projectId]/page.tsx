import Link from "next/link";
import type { PageOut } from "@/api";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function getRootQuestions(projectId: string): Promise<PageOut[]> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/questions`,
    { cache: "no-store" },
  );
  if (!res.ok) return [];
  return res.json();
}

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const questions = await getRootQuestions(projectId);

  return (
    <main className="max-w-4xl mx-auto p-8">
      <Link href="/" className="text-blue-600 hover:underline text-sm">
        &larr; Projects
      </Link>
      <h1 className="text-3xl font-bold mt-4 mb-8">Questions</h1>
      {questions.length === 0 ? (
        <p className="text-gray-500">No questions found for this project.</p>
      ) : (
        <ul className="space-y-3">
          {questions.map((q) => (
            <li key={q.id}>
              <Link
                href={`/questions/${q.id}`}
                className="block p-4 border rounded-lg hover:bg-gray-50 dark:hover:bg-gray-900 transition-colors"
              >
                <span className="font-medium">{q.summary}</span>
                <div className="text-gray-500 text-sm mt-1">
                  Epistemic status: {q.epistemic_status}/5
                  <span className="ml-3">
                    {new Date(q.created_at).toLocaleDateString()}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
