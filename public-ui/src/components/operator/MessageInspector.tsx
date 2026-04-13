"use client";

import { useState, useCallback } from "react";
import type { MessageParam, ContentBlock } from "@/lib/operator-types";

function ContentBlockView({ block }: { block: ContentBlock }) {
  if (block.type === "text") {
    return <pre className="op-msg-text">{block.text}</pre>;
  }
  if (block.type === "tool_use") {
    return (
      <div className="op-msg-tool-use">
        <div className="op-msg-tool-use-header">
          <span className="op-msg-tool-name">{block.name}</span>
          <span className="op-msg-tool-id">{block.id}</span>
        </div>
        <pre className="op-msg-tool-input">
          {JSON.stringify(block.input, null, 2)}
        </pre>
      </div>
    );
  }
  if (block.type === "tool_result") {
    return (
      <div className="op-msg-tool-result">
        <div className="op-msg-tool-result-label">
          result for {block.tool_use_id}
        </div>
        <pre className="op-msg-text">{block.content}</pre>
      </div>
    );
  }
  return null;
}

function MessageView({ message }: { message: MessageParam }) {
  const [expanded, setExpanded] = useState(false);

  const content = message.content;
  const isString = typeof content === "string";
  const preview = isString
    ? content.slice(0, 200)
    : `${(content as ContentBlock[]).length} block(s)`;
  const charCount = isString
    ? content.length
    : (content as ContentBlock[]).reduce(
        (n, b) =>
          n +
          (b.text?.length ?? 0) +
          (b.content?.length ?? 0) +
          JSON.stringify(b.input ?? "").length,
        0,
      );
  const isLong = charCount > 300;

  const toggle = useCallback(() => setExpanded((v) => !v), []);

  return (
    <div className={`op-msg op-msg-${message.role}`}>
      <button className="op-msg-header" onClick={toggle} type="button">
        <span className="op-msg-role">{message.role}</span>
        <span className="op-msg-chars">{charCount.toLocaleString()} chars</span>
        <span className="op-msg-chevron">{expanded ? "\u25BC" : "\u25B6"}</span>
      </button>
      {expanded ? (
        <div className="op-msg-body">
          {isString ? (
            <pre className="op-msg-text">{content}</pre>
          ) : (
            (content as ContentBlock[]).map((block, i) => (
              <ContentBlockView key={i} block={block} />
            ))
          )}
        </div>
      ) : isLong ? null : (
        <div className="op-msg-preview">{preview}{charCount > 200 ? "\u2026" : ""}</div>
      )}
    </div>
  );
}

export function MessageInspector({
  messages,
}: {
  messages: MessageParam[];
}) {
  const systemMsg = messages.find((m) => m.role === "system");
  const nonSystem = messages.filter((m) => m.role !== "system");

  return (
    <div className="op-msg-inspector">
      {systemMsg && (
        <MessageView
          message={systemMsg}
        />
      )}
      {nonSystem.map((msg, i) => (
        <MessageView key={i} message={msg} />
      ))}
    </div>
  );
}
