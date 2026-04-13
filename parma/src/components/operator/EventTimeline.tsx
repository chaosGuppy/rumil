"use client";

import type {
  TraceEvent,
  SpanBeginEvent,
  ModelEvent,
  ToolEvent,
  InfoEvent,
  ErrorEvent,
} from "@/lib/operator-types";
import { SpanGroup } from "./SpanGroup";
import { ModelEventCard } from "./ModelEventCard";
import { ToolEventCard } from "./ToolEventCard";

interface SpanNode {
  begin: SpanBeginEvent;
  endTimestamp?: string;
  children: (SpanNode | ModelEvent | ToolEvent | InfoEvent | ErrorEvent)[];
  events: TraceEvent[];
}

function buildSpanTree(events: TraceEvent[]): SpanNode[] {
  const spans = new Map<string, SpanNode>();
  const roots: SpanNode[] = [];

  for (const event of events) {
    if (event.event_type === "span_begin") {
      const node: SpanNode = {
        begin: event,
        children: [],
        events: [],
      };
      spans.set(event.span_id, node);

      if (event.parent_span_id && spans.has(event.parent_span_id)) {
        spans.get(event.parent_span_id)!.children.push(node);
      } else {
        roots.push(node);
      }
    } else if (event.event_type === "span_end") {
      const span = spans.get(event.span_id);
      if (span) span.endTimestamp = event.timestamp;
    } else {
      const span = spans.get(event.span_id);
      if (span) {
        span.children.push(event);
        span.events.push(event);
      }
    }
  }

  return roots;
}

function isSpanNode(
  item: SpanNode | ModelEvent | ToolEvent | InfoEvent | ErrorEvent,
): item is SpanNode {
  return "begin" in item;
}

function TimelineItem({
  item,
}: {
  item: SpanNode | ModelEvent | ToolEvent | InfoEvent | ErrorEvent;
}) {
  if (isSpanNode(item)) {
    return (
      <SpanGroup
        spanId={item.begin.span_id}
        spanType={item.begin.span_type}
        name={item.begin.name}
        beginTimestamp={item.begin.timestamp}
        endTimestamp={item.endTimestamp}
        events={item.events}
      >
        {item.children.map((child, i) => (
          <TimelineItem key={i} item={child} />
        ))}
      </SpanGroup>
    );
  }

  if (item.event_type === "model") {
    return <ModelEventCard event={item} />;
  }

  if (item.event_type === "tool") {
    return <ToolEventCard event={item} />;
  }

  if (item.event_type === "info") {
    return (
      <div className="op-info-event">
        <span className="op-info-icon">i</span>
        <span className="op-info-message">{item.message}</span>
      </div>
    );
  }

  if (item.event_type === "error") {
    return (
      <div className="op-error-event">
        <span className="op-error-icon">!</span>
        <span className="op-error-message">{item.message}</span>
        {item.traceback && (
          <pre className="op-error-traceback">{item.traceback}</pre>
        )}
      </div>
    );
  }

  return null;
}

export function EventTimeline({ events }: { events: TraceEvent[] }) {
  const tree = buildSpanTree(events);

  if (tree.length === 0) {
    return <div className="op-timeline-empty">No trace events recorded.</div>;
  }

  return (
    <div className="op-timeline">
      {tree.map((node, i) => (
        <TimelineItem key={i} item={node} />
      ))}
    </div>
  );
}
