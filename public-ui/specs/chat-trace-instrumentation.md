# Chat Trace Instrumentation

## Context

Orchestrator runs are traced (ModelEvents, ToolEvents, spans), but chat runs are not. Chat runs go through `POST /api/chat/stream` which uses a different code path (streaming with `client.messages.stream()` instead of `client.messages.create()`). The operator trace viewer shows chat runs with zero trace data.

## What exists

- `RunTracer` class in `orchestrator/tracing.py` — records events to `trace_events` table
- Orchestrator runs create a tracer and pass it to `run_step()`
- Chat runs create a `runs` record but no trace events
- Chat streaming handler in serve.py processes SSE events from Anthropic's streaming API

## What to build

### 1. Instrument the streaming chat handler

The chat streaming handler (the `generate()` async generator inside `chat_stream()`) needs to record trace events:

**Per-round span**: wrap each tool loop iteration in span_begin/span_end

**ModelEvent**: After `stream.get_final_message()`, extract:
- `response.usage` — input/output/cache tokens
- `response.content` — output blocks
- `response.stop_reason`
- Duration (measure wall time around the stream)
- The messages array at that point (input)
- The tools offered

**ToolEvent**: The tool execution already happens in the loop — wrap each `execute_tool` call with timing.

### 2. Key challenge: streaming timing

With streaming, the model response arrives in chunks. The ModelEvent should capture:
- Start time: when we called `client.messages.stream()`
- End time: when `stream.get_final_message()` returns
- Duration = end - start
- Full usage from `get_final_message().usage`

### 3. Non-streaming chat endpoint

The `POST /api/chat` (non-streaming) endpoint is simpler — it uses `client.messages.create()` like the orchestrator. Add a tracer there too.

### 4. Implementation sketch

```python
tracer = RunTracer(conn=conn, run_id=run_id)
root_span = new_id()
tracer.span_begin(root_span, "chat_turn", "chat response")

for round in range(10):
    round_span = new_id()
    tracer.span_begin(round_span, "round", f"round {round+1}", parent_span_id=root_span)
    
    t0 = time.monotonic()
    async with client.messages.stream(...) as stream:
        # ... yield SSE events ...
        response = await stream.get_final_message()
    duration_ms = int((time.monotonic() - t0) * 1000)
    
    tracer.record_model_event(round_span, model=model_id, usage=..., duration_ms=duration_ms, ...)
    
    for tc in tool_calls:
        t_tool = time.monotonic()
        result = execute_tool(...)
        tracer.record_tool_event(round_span, function_name=tc.name, ...)
    
    tracer.span_end(round_span)

tracer.span_end(root_span)
tracer.finalize()
```

### Files to modify
- `public-ui/serve.py` — instrument both chat endpoints
- No frontend changes needed (operator UI already renders trace data)

### Testing
1. Start a chat, send a message that triggers tool use
2. Go to /traces — the chat run should now show model calls, tool calls, token usage
3. Verify token counts and costs look reasonable
