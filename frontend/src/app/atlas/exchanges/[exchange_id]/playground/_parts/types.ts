// Shared types for the playground editor — kept narrow and serializable
// so the wire shape (Anthropic-style messages + tools) is the source of
// truth.
//
// Messages are objects with a `role` and `content`; content can be either
// a string (the common case) or an array of content blocks (text /
// tool_use / tool_result / etc.). We don't try to model every block
// shape statically — the JSON editor handles arbitrary block arrays.

export type Msg = { role: string; content: unknown };

export type Tool = {
  name: string;
  description: string;
  input_schema: unknown;
};

export type DraftOverrides = {
  systemPrompt: string | null;
  messages: Msg[] | null;
  tools: Tool[] | null;
  model: string | null;
  temperature: number | null;
  maxTokens: number | null;
  thinkingOff: boolean | null;
};

export const EMPTY_DRAFT: DraftOverrides = {
  systemPrompt: null,
  messages: null,
  tools: null,
  model: null,
  temperature: null,
  maxTokens: null,
  thinkingOff: null,
};

export const ROLE_OPTIONS = ["user", "assistant", "system", "tool"] as const;
