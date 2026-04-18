import type { Metadata } from "next";
import type { Page } from "@/api";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { ReadView } from "./read-view";

export type ViewItemShape = {
  page: Page;
  links: unknown[];
  section: string;
};

export type ViewSectionShape = {
  name: string;
  description: string;
  items: ViewItemShape[];
};

export type ViewShape = {
  question: Page;
  sections: ViewSectionShape[];
  health: {
    total_pages: number;
    missing_credence: number;
    missing_importance: number;
    child_questions_without_judgements: number;
    max_depth: number;
  };
};

async function getView(questionId: string): Promise<ViewShape | null> {
  const res = await serverFetch(
    `${API_BASE}/api/questions/${questionId}/view`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  return (await res.json()) as ViewShape;
}

async function getConfig(): Promise<{ enable_flag_issue: boolean }> {
  const res = await serverFetch(`${API_BASE}/api/config`, {
    cache: "no-store",
  });
  if (!res.ok) return { enable_flag_issue: false };
  return (await res.json()) as { enable_flag_issue: boolean };
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ questionId: string }>;
}): Promise<Metadata> {
  const { questionId } = await params;
  const view = await getView(questionId);
  const title = view?.question.headline ?? "Reader";
  return { title };
}

export default async function ReadPage({
  params,
}: {
  params: Promise<{ questionId: string }>;
}) {
  const { questionId } = await params;
  const [view, config] = await Promise.all([getView(questionId), getConfig()]);

  if (!view) {
    return (
      <main className="read-main">
        <div className="read-container">
          <p className="read-missing">
            No view available for question <code>{questionId}</code>.
          </p>
        </div>
      </main>
    );
  }

  return (
    <ReadView
      view={view}
      flaggingEnabled={config.enable_flag_issue}
      apiBase={API_BASE}
    />
  );
}
