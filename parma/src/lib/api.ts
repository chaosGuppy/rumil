import type {
  AdversarialVerdictSummary,
  Project,
  ProjectSummary,
  Page,
  PageLink,
  QuestionView,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/api/projects`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const projects: Project[] = await res.json();
  return projects.filter((p) => !p.hidden);
}

// Landing-page summary: one row per project with question/claim/call counts
// and last_activity_at, computed server-side by the list_projects_summary
// RPC in a single SQL query.
export async function fetchProjectsSummary(): Promise<ProjectSummary[]> {
  const res = await fetch(`${API_BASE}/api/projects/summary`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
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

export interface LinkedPage {
  page: Page;
  link: PageLink;
}

export interface PageDetail {
  page: Page;
  links_from: LinkedPage[];
  links_to: LinkedPage[];
}

// Fetch a page + its outgoing/incoming links by full id. Used by InspectPanel
// after resolving a short id via fetchPageByShortId. The API doesn't expose
// a single short-id→detail endpoint today, so we do two hops.
export async function fetchPageDetail(
  pageId: string,
): Promise<PageDetail | null> {
  const res = await fetch(`${API_BASE}/api/pages/${pageId}/detail`);
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

export type ChatStreamEventType = "text" | "tool_use_start" | "tool_use_result" | "orchestrator_progress" | "done" | "error" | "conversation";

export interface ChatStreamEvent {
  type: ChatStreamEventType;
  data: Record<string, unknown>;
}

export async function streamChatMessage(
  questionId: string,
  messages: { role: string; content: string }[],
  onEvent: (event: ChatStreamEvent) => void,
  workspace: string = "default",
  model: string = "sonnet",
  conversationId?: string,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question_id: questionId,
      messages,
      workspace,
      model,
      conversation_id: conversationId ?? null,
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

export interface ChatConversationSummary {
  id: string;
  project_id: string;
  question_id: string | null;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatConversationDetail extends ChatConversationSummary {
  messages: Array<{
    id: string;
    role: string;
    content: Record<string, unknown>;
    seq: number;
    ts: string;
  }>;
}

export async function listChatConversations(
  projectId: string,
  questionId?: string,
): Promise<ChatConversationSummary[]> {
  const params = new URLSearchParams({ project_id: projectId });
  if (questionId) params.set("question_id", questionId);
  const res = await fetch(`${API_BASE}/api/chat/conversations?${params}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function getChatConversation(
  conversationId: string,
): Promise<ChatConversationDetail> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function createChatConversation(
  projectId: string,
  questionId?: string,
  firstMessage?: string,
): Promise<ChatConversationSummary> {
  const res = await fetch(`${API_BASE}/api/chat/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      question_id: questionId ?? null,
      first_message: firstMessage ?? null,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function renameChatConversation(
  conversationId: string,
  title: string,
): Promise<ChatConversationSummary> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function deleteChatConversation(conversationId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
}

export { API_BASE };
