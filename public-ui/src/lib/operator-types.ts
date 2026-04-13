export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface ModelConfig {
  model: string;
  temperature: number;
  max_tokens: number;
  top_p?: number;
  stop_sequences?: string[];
}

export interface ContentBlock {
  type: "text" | "tool_use" | "tool_result";
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string;
}

export interface MessageParam {
  role: "user" | "assistant" | "system";
  content: string | ContentBlock[];
}

export interface ToolDef {
  name: string;
  description: string;
}

export interface ModelEvent {
  event_type: "model";
  id: string;
  span_id: string;
  timestamp: string;
  config: ModelConfig;
  input_messages: MessageParam[];
  tools_offered: ToolDef[];
  output_content: ContentBlock[];
  stop_reason: string;
  usage: TokenUsage;
  cost_usd: number;
  duration_ms: number;
}

export interface ToolEvent {
  event_type: "tool";
  id: string;
  span_id: string;
  timestamp: string;
  function_name: string;
  arguments: Record<string, unknown>;
  result: string;
  error?: string;
  duration_ms: number;
}

export interface SpanBeginEvent {
  event_type: "span_begin";
  id: string;
  span_id: string;
  parent_span_id: string | null;
  span_type: string;
  name: string;
  timestamp: string;
}

export interface SpanEndEvent {
  event_type: "span_end";
  id: string;
  span_id: string;
  timestamp: string;
}

export interface InfoEvent {
  event_type: "info";
  id: string;
  span_id: string;
  timestamp: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface ErrorEvent {
  event_type: "error";
  id: string;
  span_id: string;
  timestamp: string;
  message: string;
  traceback?: string;
}

export type TraceEvent =
  | ModelEvent
  | ToolEvent
  | SpanBeginEvent
  | SpanEndEvent
  | InfoEvent
  | ErrorEvent;

export interface RunSummary {
  id: string;
  workspace_id: string;
  workspace_name: string;
  run_type: "chat" | "orchestrate";
  status: "running" | "completed" | "error";
  started_at: string;
  completed_at: string | null;
  description: string | null;
  scope_node_headline?: string;
  total_cost_usd: number;
  total_usage: TokenUsage;
  model_call_count: number;
  tool_call_count: number;
  duration_ms: number;
}

export interface RunDetail extends RunSummary {
  events: TraceEvent[];
  config: Record<string, unknown>;
}
