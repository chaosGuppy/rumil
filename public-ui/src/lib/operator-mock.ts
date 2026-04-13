import type {
  RunSummary,
  RunDetail,
  TraceEvent,
  TokenUsage,
  ToolDef,
} from "./operator-types";

const CHAT_TOOLS: ToolDef[] = [
  { name: "search_workspace", description: "Search workspace nodes by keyword" },
  { name: "get_node", description: "Get a node and its subtree by ID" },
  { name: "create_node", description: "Create a new worldview node" },
  { name: "list_workspace", description: "List the entire workspace tree" },
  { name: "get_suggestions", description: "View pending suggestions" },
  { name: "run_orchestrator", description: "Trigger orchestrator on a branch" },
];

const ORCHESTRATOR_TOOLS: ToolDef[] = [
  { name: "add_node", description: "Add a new node to the current branch" },
  { name: "suggest_change", description: "Queue a cross-branch suggestion" },
  { name: "relevel_node", description: "Change a node's importance level" },
  { name: "inspect_branch", description: "View branch context and health" },
];

function sumUsage(events: TraceEvent[]): TokenUsage {
  const usage: TokenUsage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 };
  for (const e of events) {
    if (e.event_type === "model") {
      usage.input_tokens += e.usage.input_tokens;
      usage.output_tokens += e.usage.output_tokens;
      usage.cache_read_tokens += e.usage.cache_read_tokens;
      usage.cache_write_tokens += e.usage.cache_write_tokens;
    }
  }
  return usage;
}

function sumCost(events: TraceEvent[]): number {
  return events.reduce((s, e) => s + (e.event_type === "model" ? e.cost_usd : 0), 0);
}

const CHAT_EVENTS: TraceEvent[] = [
  {
    event_type: "span_begin",
    id: "sb-1",
    span_id: "span-turn-1",
    parent_span_id: null,
    span_type: "chat_turn",
    name: "Turn 1",
    timestamp: "2026-04-12T14:30:00.000Z",
  },
  {
    event_type: "model",
    id: "m-1",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:00.100Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.7, max_tokens: 4096 },
    input_messages: [
      {
        role: "system",
        content:
          "You are a research assistant helping explore a worldview workspace. "
          + "You have tools to search, read, and create nodes in the workspace. "
          + "The workspace investigates: What governance structures best handle AI development?\n\n"
          + "## Current Worldview\n\n"
          + "- [claim] International coordination is necessary but insufficient (credence: 7/9)\n"
          + "  - [evidence] Historical precedent from nuclear governance shows limitations\n"
          + "  - [uncertainty] Whether competitive dynamics can be overcome\n"
          + "- [hypothesis] Domestic regulation will lead international efforts (credence: 5/9)\n"
          + "  - [evidence] EU AI Act as first-mover\n"
          + "  - [evidence] US executive orders on AI safety",
      },
      {
        role: "user",
        content: "What evidence do we have about industry self-regulation?",
      },
    ],
    tools_offered: CHAT_TOOLS,
    output_content: [
      {
        type: "text",
        text: "Let me search the workspace for existing evidence about industry self-regulation.",
      },
      {
        type: "tool_use",
        id: "tu-1",
        name: "search_workspace",
        input: { query: "industry self-regulation" },
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 1847, output_tokens: 89, cache_read_tokens: 1200, cache_write_tokens: 400 },
    cost_usd: 0.0021,
    duration_ms: 2340,
  },
  {
    event_type: "tool",
    id: "t-1",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:02.500Z",
    function_name: "search_workspace",
    arguments: { query: "industry self-regulation" },
    result:
      "Found 2 results:\n"
      + "1. [evidence] Voluntary AI commitments (2023) had limited follow-through — node a3f8\n"
      + "2. [uncertainty] Whether market incentives align with safety — node b7c2",
    duration_ms: 45,
  },
  {
    event_type: "model",
    id: "m-2",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:02.600Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.7, max_tokens: 4096 },
    input_messages: [
      {
        role: "system",
        content:
          "You are a research assistant helping explore a worldview workspace. "
          + "You have tools to search, read, and create nodes in the workspace. "
          + "The workspace investigates: What governance structures best handle AI development?\n\n"
          + "## Current Worldview\n\n"
          + "- [claim] International coordination is necessary but insufficient (credence: 7/9)\n"
          + "  - [evidence] Historical precedent from nuclear governance shows limitations\n"
          + "  - [uncertainty] Whether competitive dynamics can be overcome\n"
          + "- [hypothesis] Domestic regulation will lead international efforts (credence: 5/9)\n"
          + "  - [evidence] EU AI Act as first-mover\n"
          + "  - [evidence] US executive orders on AI safety",
      },
      { role: "user", content: "What evidence do we have about industry self-regulation?" },
      {
        role: "assistant",
        content: [
          { type: "text", text: "Let me search the workspace for existing evidence about industry self-regulation." },
          { type: "tool_use", id: "tu-1", name: "search_workspace", input: { query: "industry self-regulation" } },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tu-1",
            content:
              "Found 2 results:\n"
              + "1. [evidence] Voluntary AI commitments (2023) had limited follow-through — node a3f8\n"
              + "2. [uncertainty] Whether market incentives align with safety — node b7c2",
          },
        ],
      },
    ],
    tools_offered: CHAT_TOOLS,
    output_content: [
      {
        type: "tool_use",
        id: "tu-2",
        name: "get_node",
        input: { node_id: "a3f8" },
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 2103, output_tokens: 34, cache_read_tokens: 1847, cache_write_tokens: 200 },
    cost_usd: 0.0013,
    duration_ms: 1120,
  },
  {
    event_type: "tool",
    id: "t-2",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:03.800Z",
    function_name: "get_node",
    arguments: { node_id: "a3f8" },
    result:
      "[evidence] Voluntary AI commitments (2023) had limited follow-through\n"
      + "Content: In 2023, major AI labs made voluntary safety commitments. "
      + "A year later, implementation was inconsistent: some companies published safety "
      + "frameworks while others quietly deprioritized earlier pledges. "
      + "The lack of enforcement mechanisms meant accountability was purely reputational.\n"
      + "Credence: 8/9, Robustness: 3/5",
    duration_ms: 12,
  },
  {
    event_type: "model",
    id: "m-3",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:03.900Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.7, max_tokens: 4096 },
    input_messages: [
      { role: "system", content: "(system prompt — same as above)" },
      { role: "user", content: "What evidence do we have about industry self-regulation?" },
      {
        role: "assistant",
        content: [
          { type: "text", text: "Let me search the workspace for existing evidence about industry self-regulation." },
          { type: "tool_use", id: "tu-1", name: "search_workspace", input: { query: "industry self-regulation" } },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tu-1",
            content: "Found 2 results:\n1. [evidence] Voluntary AI commitments — a3f8\n2. [uncertainty] Market incentives — b7c2",
          },
        ],
      },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "tu-2", name: "get_node", input: { node_id: "a3f8" } }],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tu-2",
            content:
              "[evidence] Voluntary AI commitments (2023) had limited follow-through\n"
              + "Content: In 2023, major AI labs made voluntary safety commitments...",
          },
        ],
      },
    ],
    tools_offered: CHAT_TOOLS,
    output_content: [
      {
        type: "text",
        text:
          "We have some relevant evidence in the workspace:\n\n"
          + "**Existing evidence on self-regulation:**\n\n"
          + "1. **Voluntary AI commitments (2023) had limited follow-through** (credence 8/9, robustness 3/5) "
          + "— Major labs made safety commitments in 2023, but implementation was inconsistent a year later. "
          + "Without enforcement, accountability was purely reputational.\n\n"
          + "2. There's also an open uncertainty about **whether market incentives align with safety**.\n\n"
          + "The workspace currently doesn't have much on *successful* self-regulation examples. "
          + "We could strengthen this area by looking at analogies from other industries "
          + "(pharmaceutical, financial services, aviation) where self-regulation has been tried. "
          + "Want me to create nodes for those comparisons?",
      },
    ],
    stop_reason: "end_turn",
    usage: { input_tokens: 2456, output_tokens: 187, cache_read_tokens: 2103, cache_write_tokens: 300 },
    cost_usd: 0.0038,
    duration_ms: 3890,
  },
  {
    event_type: "span_end",
    id: "se-1",
    span_id: "span-turn-1",
    timestamp: "2026-04-12T14:30:07.800Z",
  },
];

const ORCHESTRATE_EVENTS: TraceEvent[] = [
  {
    event_type: "span_begin",
    id: "sb-o1",
    span_id: "span-orch-1",
    parent_span_id: null,
    span_type: "orchestrate_step",
    name: "Orchestrate step 1",
    timestamp: "2026-04-12T14:15:00.000Z",
  },
  {
    event_type: "model",
    id: "m-o1",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:00.100Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.5, max_tokens: 4096 },
    input_messages: [
      {
        role: "system",
        content:
          "You are a research orchestrator. Your job is to improve the depth and quality "
          + "of a worldview branch. You can add new nodes, suggest cross-branch changes, "
          + "and adjust importance levels. Be thorough but precise.",
      },
      {
        role: "user",
        content:
          "# Branch: International coordination is necessary but insufficient\n\n"
          + "## Current state\n"
          + "- [claim] International coordination is necessary but insufficient (credence: 7/9)\n"
          + "  - [evidence] Historical precedent from nuclear governance shows limitations\n"
          + "  - [uncertainty] Whether competitive dynamics can be overcome\n\n"
          + "## Health\n"
          + "- Evidence count: 1 (thin)\n"
          + "- Missing: counter-evidence, concrete mechanisms\n"
          + "- Depth: 1 level\n\n"
          + "Improve this branch. Add evidence, identify gaps, raise uncertainties.",
      },
    ],
    tools_offered: ORCHESTRATOR_TOOLS,
    output_content: [
      {
        type: "text",
        text:
          "This branch has thin evidence. Let me inspect it more carefully, "
          + "then add supporting and counter-evidence.",
      },
      {
        type: "tool_use",
        id: "tu-o1",
        name: "inspect_branch",
        input: {},
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 892, output_tokens: 76, cache_read_tokens: 0, cache_write_tokens: 892 },
    cost_usd: 0.0048,
    duration_ms: 1890,
  },
  {
    event_type: "tool",
    id: "t-o1",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:02.000Z",
    function_name: "inspect_branch",
    arguments: {},
    result:
      "Branch: International coordination is necessary but insufficient\n"
      + "Nodes: 3 (1 claim, 1 evidence, 1 uncertainty)\n"
      + "Gaps: No counter-evidence. No concrete policy mechanisms. "
      + "Nuclear governance comparison could be expanded.\n"
      + "Suggested additions: comparative case studies, specific institution analysis",
    duration_ms: 38,
  },
  {
    event_type: "model",
    id: "m-o2",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:02.100Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.5, max_tokens: 4096 },
    input_messages: [
      { role: "system", content: "(orchestrator system prompt)" },
      { role: "user", content: "(branch context)" },
      {
        role: "assistant",
        content: [
          { type: "text", text: "This branch has thin evidence. Let me inspect it more carefully..." },
          { type: "tool_use", id: "tu-o1", name: "inspect_branch", input: {} },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "tu-o1",
            content: "Branch: International coordination...\nGaps: No counter-evidence...",
          },
        ],
      },
    ],
    tools_offered: ORCHESTRATOR_TOOLS,
    output_content: [
      {
        type: "tool_use",
        id: "tu-o2",
        name: "add_node",
        input: {
          node_type: "evidence",
          headline: "IAEA model shows international coordination can work for narrow technical domains",
          content:
            "The International Atomic Energy Agency successfully coordinates nuclear inspections "
            + "across 180+ states. However, its mandate is narrow (non-proliferation verification), "
            + "and even this limited scope has required decades of institutional development. "
            + "AI governance would need broader scope, covering development practices, deployment "
            + "standards, and potentially compute governance.",
          credence: 7,
          robustness: 4,
        },
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 1234, output_tokens: 145, cache_read_tokens: 892, cache_write_tokens: 300 },
    cost_usd: 0.0031,
    duration_ms: 3200,
  },
  {
    event_type: "tool",
    id: "t-o2",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:05.300Z",
    function_name: "add_node",
    arguments: {
      node_type: "evidence",
      headline: "IAEA model shows international coordination can work for narrow technical domains",
    },
    result: "Created node e4a1 under current branch",
    duration_ms: 23,
  },
  {
    event_type: "model",
    id: "m-o3",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:05.400Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.5, max_tokens: 4096 },
    input_messages: [
      { role: "system", content: "(orchestrator system prompt)" },
      { role: "user", content: "(branch context)" },
      { role: "assistant", content: "(previous turn with inspect_branch)" },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "tu-o1", content: "Branch: International coordination..." }] },
      { role: "assistant", content: [{ type: "tool_use", id: "tu-o2", name: "add_node", input: { node_type: "evidence", headline: "IAEA model..." } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "tu-o2", content: "Created node e4a1 under current branch" }] },
    ],
    tools_offered: ORCHESTRATOR_TOOLS,
    output_content: [
      {
        type: "tool_use",
        id: "tu-o3",
        name: "add_node",
        input: {
          node_type: "uncertainty",
          headline: "Whether AI governance can be scoped narrowly enough for international coordination",
          content:
            "Unlike nuclear non-proliferation, AI capabilities are dual-use by default and "
            + "the boundary between safe and dangerous capabilities is unclear. International "
            + "coordination may require a broader mandate than any existing institution has achieved.",
        },
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 1567, output_tokens: 112, cache_read_tokens: 1234, cache_write_tokens: 290 },
    cost_usd: 0.0028,
    duration_ms: 2780,
  },
  {
    event_type: "tool",
    id: "t-o3",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:08.200Z",
    function_name: "add_node",
    arguments: {
      node_type: "uncertainty",
      headline: "Whether AI governance can be scoped narrowly enough for international coordination",
    },
    result: "Created node f9b3 under current branch",
    duration_ms: 19,
  },
  {
    event_type: "model",
    id: "m-o4",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:08.300Z",
    config: { model: "claude-sonnet-4-6", temperature: 0.5, max_tokens: 4096 },
    input_messages: [
      { role: "system", content: "(orchestrator system prompt)" },
      { role: "user", content: "(accumulated conversation)" },
    ],
    tools_offered: ORCHESTRATOR_TOOLS,
    output_content: [
      {
        type: "tool_use",
        id: "tu-o4",
        name: "suggest_change",
        input: {
          suggestion_type: "add_to_branch",
          target_branch: "Domestic regulation will lead international efforts",
          reasoning:
            "The IAEA evidence I added to the international coordination branch also relates to "
            + "domestic regulation — if international bodies are slow, domestic regulators may "
            + "create de facto international standards. The EU AI Act is an example.",
          payload: {
            node_type: "evidence",
            headline: "EU AI Act as de facto international standard via market power",
          },
        },
      },
    ],
    stop_reason: "tool_use",
    usage: { input_tokens: 1890, output_tokens: 134, cache_read_tokens: 1567, cache_write_tokens: 280 },
    cost_usd: 0.0033,
    duration_ms: 2450,
  },
  {
    event_type: "tool",
    id: "t-o4",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:10.800Z",
    function_name: "suggest_change",
    arguments: {
      suggestion_type: "add_to_branch",
      target_branch: "Domestic regulation will lead international efforts",
    },
    result: "Suggestion s-001 queued for review",
    duration_ms: 31,
  },
  {
    event_type: "info",
    id: "i-o1",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:10.900Z",
    message: "Orchestrator completed: added 2 nodes, queued 1 suggestion",
    data: { nodes_added: 2, suggestions_queued: 1 },
  },
  {
    event_type: "span_end",
    id: "se-o1",
    span_id: "span-orch-1",
    timestamp: "2026-04-12T14:15:11.000Z",
  },
];

export const MOCK_RUNS: RunSummary[] = [
  {
    id: "run-chat-001",
    workspace_id: "ws-governance",
    workspace_name: "ai-governance",
    run_type: "chat",
    status: "completed",
    started_at: "2026-04-12T14:30:00.000Z",
    completed_at: "2026-04-12T14:30:07.800Z",
    description: "What evidence do we have about industry self-regulation?",
    total_cost_usd: sumCost(CHAT_EVENTS),
    total_usage: sumUsage(CHAT_EVENTS),
    model_call_count: 3,
    tool_call_count: 2,
    duration_ms: 7800,
  },
  {
    id: "run-orch-001",
    workspace_id: "ws-governance",
    workspace_name: "ai-governance",
    run_type: "orchestrate",
    status: "completed",
    started_at: "2026-04-12T14:15:00.000Z",
    completed_at: "2026-04-12T14:15:11.000Z",
    description: "orchestrate: International coordination is necessary but insufficient",
    scope_node_headline: "International coordination is necessary but insufficient",
    total_cost_usd: sumCost(ORCHESTRATE_EVENTS),
    total_usage: sumUsage(ORCHESTRATE_EVENTS),
    model_call_count: 4,
    tool_call_count: 4,
    duration_ms: 11000,
  },
  {
    id: "run-chat-002",
    workspace_id: "ws-governance",
    workspace_name: "ai-governance",
    run_type: "chat",
    status: "running",
    started_at: "2026-04-12T14:35:00.000Z",
    completed_at: null,
    description: "Can you compare this to climate governance structures?",
    total_cost_usd: 0.0014,
    total_usage: { input_tokens: 1200, output_tokens: 45, cache_read_tokens: 900, cache_write_tokens: 200 },
    model_call_count: 1,
    tool_call_count: 0,
    duration_ms: 0,
  },
];

export const MOCK_RUN_DETAILS: Record<string, RunDetail> = {
  "run-chat-001": {
    ...MOCK_RUNS[0],
    events: CHAT_EVENTS,
    config: { model: "sonnet", temperature: 0.7 },
  },
  "run-orch-001": {
    ...MOCK_RUNS[1],
    events: ORCHESTRATE_EVENTS,
    config: { model: "sonnet", temperature: 0.5, dry_run: false },
  },
};
