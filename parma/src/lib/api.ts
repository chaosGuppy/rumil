import type {
  AdversarialVerdictSummary,
  Project,
  ProjectSummary,
  Page,
  PageLink,
  QuestionView,
  SearchResult,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/api/projects`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const projects: Project[] = await res.json();
  return projects.filter((p) => !p.hidden);
}

export interface CreateProjectResult {
  project: Project;
  // Server-side flag: true if a new row was inserted, false if a workspace
  // with the same name already existed and was returned unchanged. The
  // landing modal surfaces the latter with a subtle "already exists" hint.
  created: boolean;
}

export async function createProject(
  name: string,
): Promise<CreateProjectResult> {
  const res = await fetch(`${API_BASE}/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    // Try to lift the FastAPI error detail; fall back to generic message.
    let detail: string | null = null;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
        detail = String(body.detail[0].msg);
      }
    } catch {
      // ignore — res.json() may fail on empty bodies
    }
    throw new Error(detail ?? `API error: ${res.status}`);
  }
  return res.json();
}

// Landing-page summary: one row per project with question/claim/call counts
// and last_activity_at, computed server-side by the list_projects_summary
// RPC in a single SQL query. Pass `includeHidden` to surface soft-deleted
// workspaces (the landing "show hidden" toggle).
export async function fetchProjectsSummary(
  includeHidden: boolean = false,
): Promise<ProjectSummary[]> {
  const qs = includeHidden ? "?include_hidden=true" : "";
  const res = await fetch(`${API_BASE}/api/projects/summary${qs}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// PATCH a workspace: flip `hidden` and/or rename. Both fields are optional
// but at least one must be supplied server-side (422 otherwise). 409 on a
// name collision with another workspace; the caller should surface the
// detail inline so the user can retry with a different name.
export async function updateProject(
  projectId: string,
  patch: { hidden?: boolean; name?: string },
): Promise<Project> {
  const res = await fetch(`${API_BASE}/api/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
        detail = String(body.detail[0].msg);
      }
    } catch {
      // non-JSON body
    }
    const err = new Error(detail ?? `API error: ${res.status}`) as Error & {
      status?: number;
    };
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// PATCH a run's hidden flag. Returns the refreshed hidden value so the
// RunPicker can update its row without a full refetch. 404 if the run
// doesn't exist (shouldn't happen — the UI only surfaces runs it already
// has in hand).
export async function updateRunHidden(
  runId: string,
  hidden: boolean,
): Promise<{ run_id: string; hidden: boolean }> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden }),
  });
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

export async function searchWorkspace(
  projectId: string,
  query: string,
  limit: number = 30,
): Promise<SearchResult[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/search?${params.toString()}`,
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  const body = await res.json();
  return body.results ?? [];
}

// Create a bare root question in a workspace. No research is triggered —
// the Page is seeded with provenance_model='human' and the caller is
// expected to navigate into it and start asking via chat (/orchestrate,
// /dispatch, /ask).
//
// `content` is optional; if omitted the backend uses the headline as the
// page body so the question still renders sensibly before chat fleshes it out.
export async function createRootQuestion(
  projectId: string,
  headline: string,
  content?: string,
): Promise<Page> {
  const res = await fetch(
    `${API_BASE}/api/projects/${projectId}/questions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        headline,
        content: content ?? null,
      }),
    },
  );
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
        detail = String(body.detail[0].msg);
      }
    } catch {
      // empty/non-JSON body
    }
    throw new Error(detail ?? `API error: ${res.status}`);
  }
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

export type ChatStreamEventType =
  | "text"
  | "tool_use_start"
  | "tool_use_result"
  | "orchestrator_progress"
  | "turn_costs"
  | "done"
  | "error"
  | "conversation";

export interface ChatStreamEvent {
  type: ChatStreamEventType;
  data: Record<string, unknown>;
}

export interface ChatTurnCosts {
  chat_usd: number;
  research_usd: number;
  research_by_call_type: Record<string, number>;
}

/**
 * Navigation directive emitted by the backend `set_view` tool.
 *
 * The backend wraps this in a JSON payload inside the `tool_use_result`
 * event (keyed on `__navigate__`). The chat panel parses each tool_result
 * result string, and when it finds this shape, invokes `onNavigate`
 * so the host page can update the URL.
 *
 * All IDs are resolved full UUIDs (or null). `*_short` fields are the
 * 8-char prefixes used in URLs.
 */
export interface NavigateDirective {
  view: string;
  run_id?: string | null;
  run_id_short?: string | null;
  call_id?: string | null;
  call_id_short?: string | null;
  question_id?: string | null;
  question_id_short?: string | null;
  panes?: string[];
}

export interface ChatUiSnapshot {
  viewMode?: string;
  openRunId?: string;
  openCallId?: string;
  openPageIds?: string[];
  drawerPageId?: string;
  activeSection?: string;
  reviewOpen?: boolean;
}

export async function streamChatMessage(
  questionId: string,
  messages: { role: string; content: string }[],
  onEvent: (event: ChatStreamEvent) => void,
  workspace: string = "default",
  model: string = "sonnet",
  conversationId?: string,
  ui?: ChatUiSnapshot,
  signal?: AbortSignal,
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
      open_run_id: ui?.openRunId ?? null,
      open_page_ids: ui?.openPageIds ?? [],
      view_mode: ui?.viewMode ?? null,
      open_call_id: ui?.openCallId ?? null,
      drawer_page_id: ui?.drawerPageId ?? null,
      active_section: ui?.activeSection ?? null,
      review_open: ui?.reviewOpen ?? false,
    }),
    signal,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) break;
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
  } finally {
    // Release the reader so the underlying stream can be cancelled by the
    // fetch abort — otherwise the body keeps a lock and the connection lingers.
    if (signal?.aborted) {
      await reader.cancel().catch(() => {});
    } else {
      reader.releaseLock();
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
  // Branching metadata. Null on original, non-branched conversations.
  parent_conversation_id?: string | null;
  branched_at_seq?: number | null;
}

export interface ChatConversationDetail extends ChatConversationSummary {
  messages: Array<{
    id: string;
    role: string;
    content: Record<string, unknown>;
    seq: number;
    ts: string;
    question_id: string | null;
  }>;
}

export async function listChatConversations(
  projectId: string,
  questionId?: string,
): Promise<ChatConversationSummary[]> {
  // Conversations are project-scoped — see ChatPanel. `questionId` is kept
  // as an optional filter for callers that want a question-only slice (e.g.
  // future per-question views); the default chat-panel listing omits it so
  // the sidebar shows every project conversation.
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

/**
 * Branch a conversation at a specific message seq, creating a new
 * conversation seeded with messages 0..atSeq. The parent conversation
 * is preserved intact — branching is non-destructive.
 *
 * Returns the new conversation in detail shape (includes the copied
 * messages) so the caller can swap the active conversation and render
 * the truncated transcript without a second round trip.
 */
export async function branchChatConversation(
  conversationId: string,
  atSeq: number,
  title?: string,
): Promise<ChatConversationDetail> {
  const res = await fetch(
    `${API_BASE}/api/chat/conversations/${conversationId}/branch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        at_seq: atSeq,
        title: title ?? null,
      }),
    },
  );
  if (!res.ok) {
    const errText = await res.text().catch(() => "");
    throw new Error(`branch failed (${res.status}): ${errText}`);
  }
  return res.json();
}

// Trace-related types. Mirror subsets of the API schemas (CallSummary,
// CallNodeOut, RunTraceTreeOut, TraceEventOut, LLMExchange*). We hand-type
// these rather than using codegen — parma doesn't share the generated SDK
// with the rumil frontend, and keeping TRACE self-contained keeps the
// coupling small.

export interface TraceCallSummary {
  id: string;
  call_type: string;
  status: string;
  parent_call_id: string | null;
  scope_page_id: string | null;
  call_params: Record<string, unknown> | null;
  created_at: string;
  completed_at: string | null;
  sequence_id: string | null;
  sequence_position: number | null;
  cost_usd: number | null;
}

export interface TraceCallNode {
  call: TraceCallSummary;
  scope_page_summary: string | null;
  warning_count: number;
  error_count: number;
}

export interface RunTraceTree {
  run_id: string;
  question: Page | null;
  calls: TraceCallNode[];
  cost_usd: number | null;
  staged: boolean;
  config: Record<string, unknown>;
}

// TraceEvent is intentionally typed as a loose shape. The backend uses a
// discriminated union; in the UI we render generically (pretty-print JSON
// with a couple of event-specific shortcuts in TraceView). Typing every
// event variant would more than double the component's surface for little
// ergonomic gain — events are read, not written, here.
export interface TraceEvent {
  event: string;
  ts: string;
  call_id: string;
  [key: string]: unknown;
}

export interface LLMExchangeSummary {
  id: string;
  phase: string;
  round: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
}

export interface LLMExchangeToolCall {
  name: string;
  input: Record<string, unknown> | string;
}

export interface LLMExchangeDetail {
  id: string;
  call_id: string;
  phase: string;
  round: number | null;
  system_prompt: string | null;
  user_message: string | null;
  user_messages: Array<Record<string, unknown>> | null;
  response_text: string | null;
  tool_calls: LLMExchangeToolCall[];
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
}

export async function fetchRunTraceTree(runId: string): Promise<RunTraceTree> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/trace-tree`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface RunSpendByCallType {
  call_type: string;
  count: number;
  cost_usd: number;
  duration_ms: number;
}

export interface RunSpend {
  run_id: string;
  run_id_short: string;
  total_cost_usd: number;
  total_duration_ms: number;
  total_calls: number;
  by_call_type: RunSpendByCallType[];
}

export async function fetchRunSpend(runId: string): Promise<RunSpend> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/spend`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface RefineIterationVerdict {
  claim_holds: boolean;
  claim_confidence: number;
  dissents: string[];
  concurrences: string[];
  stronger_side: string;
}

export interface RefineIteration {
  iteration: number;
  draft_page_id: string;
  draft_short_id: string;
  content: string;
  headline: string;
  verdict: RefineIterationVerdict | null;
  created_at: string;
}

export interface PageIterations {
  page_id: string;
  iterations: RefineIteration[];
}

// Fetch refine-artifact iterations for an artifact page. Returns null if
// the page isn't a refine-artifact (server responds 400) so the caller can
// silently suppress the panel instead of treating it as an error.
export async function fetchPageIterations(
  pageId: string,
): Promise<PageIterations | null> {
  const res = await fetch(`${API_BASE}/api/pages/${pageId}/iterations`);
  if (res.status === 400) return null;
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchCallEvents(callId: string): Promise<TraceEvent[]> {
  const res = await fetch(`${API_BASE}/api/calls/${callId}/events`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchCallLLMExchanges(
  callId: string,
): Promise<LLMExchangeSummary[]> {
  const res = await fetch(`${API_BASE}/api/calls/${callId}/llm-exchanges`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchLLMExchange(
  exchangeId: string,
): Promise<LLMExchangeDetail> {
  const res = await fetch(`${API_BASE}/api/llm-exchanges/${exchangeId}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// Recent runs for the TRACE mode picker. We reuse the existing
// /api/projects/{id}/runs endpoint — one SQL call for the active project's
// runs, ordered most-recent first.
export interface RunListItem {
  run_id: string | null;
  created_at: string;
  name: string;
  config: Record<string, unknown> | null;
  question_summary: string | null;
  staged: boolean;
  hidden: boolean;
}

export async function fetchProjectRuns(
  projectId: string,
): Promise<RunListItem[]> {
  const res = await fetch(`${API_BASE}/api/projects/${projectId}/runs`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// Friendly-user feature flags surfaced by GET /api/config. The flag UI hides
// itself when enable_flag_issue is false, mirroring the server-side 403.
export interface AppConfig {
  enable_flag_issue: boolean;
}

export async function fetchAppConfig(): Promise<AppConfig> {
  const res = await fetch(`${API_BASE}/api/config`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

// Telemetry: record that a friendly user dwelled on a view-item for
// `dwellSeconds`. The backend writes a reputation_events row tagged
// read_time. This helper MUST NEVER throw — telemetry failures should
// never break the reader UX. Errors are swallowed after a debug log so a
// broken proxy / offline tab doesn't cascade into visible errors.
export async function recordViewItemRead(
  viewItemId: string,
  dwellSeconds: number,
): Promise<void> {
  const url = `${API_BASE}/api/view-items/${encodeURIComponent(viewItemId)}/read`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds: dwellSeconds }),
      // keepalive lets the request survive a page unload when fired from a
      // visibilitychange/beforeunload handler.
      keepalive: true,
    }).catch(() => {});
  } catch {
    // defensive: fetch() itself can synchronously throw in exotic cases
    // (invalid URL, etc.). Telemetry is strictly best-effort.
  }
}

// Flag a view-item with a short category + freeform note. Returns the new
// flag id so the caller can offer an inline "undo" within a grace window.
// 403 when the server has enable_flag_issue=false.
export async function flagViewItem(
  viewItemId: string,
  params: { category: string; message: string; suggestedFix?: string },
): Promise<{ flag_id: string; page_id: string }> {
  const res = await fetch(
    `${API_BASE}/api/view-items/${encodeURIComponent(viewItemId)}/flag`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        category: params.category,
        message: params.message,
        suggested_fix: params.suggestedFix ?? "",
      }),
    },
  );
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail ?? `API error: ${res.status}`);
  }
  const body = await res.json();
  return { flag_id: body.flag_id, page_id: body.page_id };
}

export async function unflagViewItem(flagId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/view-items/flags/${encodeURIComponent(flagId)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(`API error: ${res.status}`);
}

// Lift a FastAPI-style error detail off a failed response. Mirrors the
// pattern used by createProject/createRootQuestion/etc. — returns a string
// suitable for an inline error message, or a fallback when the body is
// empty/unparseable.
async function liftFastApiError(res: Response, fallback?: string): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
      return String(body.detail[0].msg);
    }
  } catch {
    // non-JSON body
  }
  return fallback ?? `API error: ${res.status}`;
}

// Operator action: retroactively stage a completed run. 409 if already
// staged, 404 if the run doesn't exist. Returns the updated staged flag
// so the caller can refresh its local view without a second fetch.
export async function stageRun(
  runId: string,
): Promise<{ run_id: string; staged: boolean }> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/stage`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await liftFastApiError(res));
  return res.json();
}

// Operator action: commit a staged run, making its effects visible to all
// readers. 409 if the run isn't staged.
export async function commitRun(
  runId: string,
): Promise<{ run_id: string; staged: boolean }> {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/commit`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await liftFastApiError(res));
  return res.json();
}

// Kick off an orchestrator run against an existing question. Backend
// returns 202 with the new run_id immediately (orchestrator runs in the
// background). Caller should navigate to the trace so the user can watch
// it materialize.
export async function continueResearch(
  questionId: string,
  budget: number,
): Promise<{ run_id: string; question_id: string; budget: number }> {
  const res = await fetch(
    `${API_BASE}/api/questions/${encodeURIComponent(questionId)}/continue`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ budget }),
    },
  );
  if (!res.ok) throw new Error(await liftFastApiError(res));
  return res.json();
}

// Kick off an AB-eval comparing two runs. Backend returns 202 immediately;
// the eval runs in the background. The final ab_eval_report id isn't known
// yet — callers typically navigate to /ab-evals and poll.
export async function startAbEval(
  runIdA: string,
  runIdB: string,
): Promise<{ run_id_a: string; run_id_b: string; status: string }> {
  const res = await fetch(`${API_BASE}/api/ab-evals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id_a: runIdA, run_id_b: runIdB }),
  });
  if (!res.ok) throw new Error(await liftFastApiError(res));
  return res.json();
}

export interface ABEvalDimensionSummary {
  name: string;
  display_name: string;
  preference: string;
}

export interface ABEvalReportListItem {
  id: string;
  run_id_a: string;
  run_id_b: string;
  question_id_a: string;
  question_id_b: string;
  question_headline: string;
  overall_assessment_preview: string;
  preferences: ABEvalDimensionSummary[];
  created_at: string;
}

export async function fetchAbEvals(): Promise<ABEvalReportListItem[]> {
  const res = await fetch(`${API_BASE}/api/ab-evals`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export { API_BASE };
