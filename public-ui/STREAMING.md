# Streaming Chat — Implementation Spec

The chat currently waits for the full response before displaying anything.
With tool use loops, this can mean 10-30s of silence. Streaming fixes this.

## Architecture

```
Browser (ChatPanel)
  ↓ POST /api/chat/stream
  ↓ SSE connection
Server (serve.py)
  ↓ Anthropic streaming API
  ↓ Tool execution between chunks
  ↓ SSE events back to browser
```

## Server Changes (serve.py)

### New endpoint: `POST /api/chat/stream`

Same request body as `/api/chat` (`ChatRequest`). Returns an SSE stream
instead of a JSON response.

Use Anthropic's streaming API:
```python
async with client.messages.stream(
    model=model_id,
    max_tokens=4096,
    system=full_system,
    messages=messages,
    tools=TOOLS,
) as stream:
    async for event in stream:
        # yield SSE events
```

### SSE event types

```
event: text
data: {"content": "The weakest claim appears to be"}

event: tool_use_start
data: {"name": "search_workspace", "input": {"query": "governance"}}

event: tool_use_result
data: {"name": "search_workspace", "result": "Found 3 nodes..."}

event: done
data: {}

event: error
data: {"message": "API error: ..."}
```

### Tool use during streaming

When the model emits a tool_use block mid-stream:
1. Send `tool_use_start` event to the browser
2. Execute the tool synchronously (our tools are fast — SQLite queries)
3. Send `tool_use_result` event
4. Continue the stream with the tool result appended to messages
5. The model resumes generating text

This means the browser sees: text → tool indicator → tool result → more text,
all incrementally.

### Implementation pattern

Use FastAPI's `StreamingResponse` with an async generator:

```python
from fastapi.responses import StreamingResponse

@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    async def generate():
        # ... setup (same as current /api/chat)
        for round in range(10):
            async with client.messages.stream(...) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        yield f"event: text\ndata: {json.dumps({'content': event.delta.text})}\n\n"
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        # tool call starting
                        pass
                # after stream completes, check for tool calls
                response = await stream.get_final_message()
                # ... execute tools, continue loop
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

## Frontend Changes (ChatPanel.tsx)

### Replace fetch with EventSource

Instead of:
```typescript
const res = await fetch(`${API_BASE}/api/chat`, { method: "POST", body: ... });
const data = await res.json();
```

Use:
```typescript
const res = await fetch(`${API_BASE}/api/chat/stream`, { method: "POST", body: ... });
const reader = res.body!.getReader();
const decoder = new TextDecoder();

// Parse SSE events from the stream
let currentText = "";
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  const chunk = decoder.decode(value);
  // parse SSE format: "event: type\ndata: {...}\n\n"
  for (const event of parseSSE(chunk)) {
    if (event.type === "text") {
      currentText += event.data.content;
      updateMessage(currentText);  // re-render incrementally
    } else if (event.type === "tool_use_start") {
      addToolIndicator(event.data);
    } else if (event.type === "tool_use_result") {
      updateToolIndicator(event.data);
    }
  }
}
```

### Incremental message rendering

The assistant message should update in real-time as text chunks arrive.
ReactMarkdown can re-render efficiently on each chunk.

Add a `streaming` flag to `Message` so the UI can show a cursor/indicator
while text is still arriving.

### Tool use indicators during streaming

When a `tool_use_start` event arrives, show a compact indicator in the
message flow:
```
Rumil: The weakest claim appears to be...
  ⟳ searching workspace ("governance")
  ✓ found 3 nodes
  ...based on the search results, I'd say...
```

This replaces the current post-hoc "used tool(...)" display for streaming
messages. Non-streaming messages (from history) keep the current format.

## SSE parsing helper

```typescript
interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

function* parseSSE(raw: string): Generator<SSEEvent> {
  for (const block of raw.split("\n\n")) {
    const lines = block.split("\n");
    let type = "";
    let data = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) data = line.slice(6);
    }
    if (type && data) {
      try { yield { type, data: JSON.parse(data) }; } catch { /* skip */ }
    }
  }
}
```

## Migration path

1. Add `/api/chat/stream` endpoint alongside existing `/api/chat`
2. Update ChatPanel to use streaming for new messages
3. Keep `/api/chat` for any non-streaming uses (testing, scripts)
4. The thinking indicator already exists — just swap it for the
   streaming text as chunks arrive
