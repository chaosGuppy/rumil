import { CLIENT_API_BASE } from "@/api-config";
import type {
  BranchConversationRequest,
  ChatRequest,
  ConversationDetail,
  ConversationListItem,
  CreateConversationRequest,
} from "@/api";

export type StreamEvent =
  | { event: "conversation"; data: { conversation_id: string } }
  | { event: "assistant_text_delta"; data: { text: string } }
  | { event: "tool_use_start"; data: { id: string; name: string } }
  | {
      event: "tool_use_result";
      data: { id: string; name: string; result: string };
    }
  | { event: "done"; data: Record<string, never> }
  | { event: "error"; data: { message: string } };

export type ConvEvent =
  | { event: "subscribed"; data: { conversation_id: string } }
  | {
      event: "dispatch_completed";
      data: {
        tool_use_id: string;
        run_id: string;
        call_id?: string;
        call_type: string;
        question_id?: string | null;
        headline: string;
        status: "completed" | "failed";
        summary: string;
        error?: string;
        trace_url: string;
      };
    };

function parseFrames(buffer: string): { frames: StreamEvent[]; rest: string } {
  const frames: StreamEvent[] = [];
  let rest = buffer;
  while (true) {
    const idx = rest.indexOf("\n\n");
    if (idx < 0) break;
    const raw = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    let event = "message";
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) continue;
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
    }
    if (dataLines.length === 0) continue;
    try {
      const data = JSON.parse(dataLines.join("\n"));
      frames.push({ event, data } as StreamEvent);
    } catch {
      // malformed frame, skip
    }
  }
  return { frames, rest };
}

export async function* streamChatTurn(
  req: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${CLIENT_API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`chat stream failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { frames, rest } = parseFrames(buffer);
    buffer = rest;
    for (const frame of frames) yield frame;
  }
  const tail = buffer + decoder.decode();
  if (tail.trim()) {
    const { frames } = parseFrames(tail + "\n\n");
    for (const frame of frames) yield frame;
  }
}

export function subscribeConvEvents(
  conversationId: string,
  onEvent: (e: ConvEvent) => void,
): () => void {
  const url = `${CLIENT_API_BASE}/api/chat/conversations/${conversationId}/events`;
  const es = new EventSource(url);
  const handler = (eventName: ConvEvent["event"]) => (msg: MessageEvent) => {
    try {
      const data = JSON.parse(msg.data);
      onEvent({ event: eventName, data } as ConvEvent);
    } catch {
      // drop
    }
  };
  es.addEventListener("subscribed", handler("subscribed"));
  es.addEventListener("dispatch_completed", handler("dispatch_completed"));
  es.onerror = () => {
    // EventSource auto-reconnects; noop here
  };
  return () => es.close();
}

export async function listConversations(
  projectId: string,
  opts: { questionId?: string | null; limit?: number } = {},
): Promise<ConversationListItem[]> {
  const params = new URLSearchParams({ project_id: projectId });
  if (opts.questionId) params.set("question_id", opts.questionId);
  if (opts.limit) params.set("limit", String(opts.limit));
  const res = await fetch(
    `${CLIENT_API_BASE}/api/chat/conversations?${params.toString()}`,
  );
  if (!res.ok) throw new Error(`list failed: ${res.status}`);
  return res.json();
}

export async function getConversation(
  conversationId: string,
): Promise<ConversationDetail> {
  const res = await fetch(
    `${CLIENT_API_BASE}/api/chat/conversations/${conversationId}`,
  );
  if (!res.ok) throw new Error(`get failed: ${res.status}`);
  return res.json();
}

export async function createConversation(
  body: CreateConversationRequest,
): Promise<ConversationListItem> {
  const res = await fetch(`${CLIENT_API_BASE}/api/chat/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`create failed: ${res.status}`);
  return res.json();
}

export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<ConversationListItem> {
  const res = await fetch(
    `${CLIENT_API_BASE}/api/chat/conversations/${conversationId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    },
  );
  if (!res.ok) throw new Error(`rename failed: ${res.status}`);
  return res.json();
}

export async function deleteConversation(
  conversationId: string,
): Promise<void> {
  const res = await fetch(
    `${CLIENT_API_BASE}/api/chat/conversations/${conversationId}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(`delete failed: ${res.status}`);
}

export async function branchConversation(
  conversationId: string,
  body: BranchConversationRequest,
): Promise<ConversationDetail> {
  const res = await fetch(
    `${CLIENT_API_BASE}/api/chat/conversations/${conversationId}/branch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) throw new Error(`branch failed: ${res.status}`);
  return res.json();
}
