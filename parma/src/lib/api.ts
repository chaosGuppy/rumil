import type {
  Project,
  Page,
  QuestionView,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/api/projects`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const projects: Project[] = await res.json();
  return projects.filter((p) => !p.hidden);
}

export async function fetchRootQuestions(
  projectId: string,
): Promise<Page[]> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/questions`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchQuestionView(
  questionId: string,
  importanceThreshold: number = 3,
): Promise<QuestionView> {
  const res = await fetch(
    `${API_BASE}/api/questions/${questionId}/view?importance_threshold=${importanceThreshold}`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface ProjectStats {
  pages_total: number;
  pages_by_type: Record<string, number>;
}

export async function fetchProjectStats(
  projectId: string,
): Promise<ProjectStats> {
  const res = await fetch(`${API_BASE}/api/projects/${projectId}/stats`);
  if (!res.ok) return { pages_total: 0, pages_by_type: {} };
  return res.json();
}

export interface Suggestion {
  id: string;
  suggestion_type: string;
  target_page_id: string;
  target_headline: string | null;
  source_page_id: string | null;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
}

export async function fetchSuggestions(
  projectId: string,
  status: string = "pending",
): Promise<Suggestion[]> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/suggestions?status=${status}`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function respondToSuggestion(
  id: string,
  action: "accept" | "reject",
): Promise<Record<string, unknown>> {
  const status = action === "accept" ? "accepted" : "rejected";
  const res = await fetch(
    `${API_BASE}/api/suggestions/${id}/review?status=${status}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchConcepts(
  projectId: string,
): Promise<Page[]> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/pages?page_type=concept&limit=200`,
  );
  if (!res.ok) return [];
  const data = await res.json();
  return data.items ?? [];
}

export async function fetchSources(
  projectId: string,
): Promise<Page[]> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/pages?page_type=source&limit=200`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const data = await res.json();
  return data.items ?? [];
}

export async function fetchPageByShortId(
  shortId: string,
): Promise<Page | null> {
  const res = await fetch(`${API_BASE}/api/pages/short/${shortId}`);
  if (!res.ok) return null;
  return res.json();
}

export interface ChatToolUse {
  name: string;
  input: Record<string, unknown>;
  result: string;
}

export interface ChatResponse {
  response: string;
  tool_uses: ChatToolUse[];
}

export async function sendChatMessage(
  questionId: string,
  messages: { role: string; content: string }[],
  workspace: string = "default",
): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question_id: questionId,
      messages,
      workspace,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export type ChatStreamEventType = "text" | "tool_use_start" | "tool_use_result" | "done" | "error";

export interface ChatStreamEvent {
  type: ChatStreamEventType;
  data: Record<string, unknown>;
}

export async function streamChatMessage(
  questionId: string,
  messages: { role: string; content: string }[],
  onEvent: (event: ChatStreamEvent) => void,
  workspace: string = "default",
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question_id: questionId,
      messages,
      workspace,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const blocks = buffer.split("\n\n");
    buffer = blocks.pop()!;
    for (const block of blocks) {
      const lines = block.split("\n");
      let eventType = "";
      let data = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (eventType && data) {
        try {
          onEvent({ type: eventType as ChatStreamEventType, data: JSON.parse(data) });
        } catch { /* skip malformed */ }
      }
    }
  }
}

export { API_BASE };
