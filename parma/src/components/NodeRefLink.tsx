"use client";

import type { ReactNode } from "react";

// 8-char lowercase hex short IDs with word boundaries. Matches both bare
// refs (`abc12345`) and bracketed refs (`[abc12345]`) because the brackets
// are not word characters. The capture group is the id.
export const NODE_ID_RE = /\b([0-9a-f]{8})\b/g;

export function TextWithNodeRefs({
  text,
  onNodeRef,
}: {
  text: string;
  onNodeRef?: (id: string) => void;
}) {
  if (!onNodeRef) return <>{text}</>;
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(NODE_ID_RE);
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const id = match[1];
    parts.push(
      <button
        key={match.index}
        type="button"
        onClick={() => onNodeRef(id)}
        className="node-ref-link"
        title={`Inspect ${id}`}
      >
        {id}
      </button>,
    );
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <>{parts}</>;
}

// Walk the direct children of a react-markdown node and linkify any 8-char
// hex refs embedded in the string children. Non-string children (already
// wrapped <strong>, <em>, <a>, etc.) are passed through — react-markdown
// will recurse into them with the same overrides, so their text children
// get processed too.
export function processChildren(
  children: ReactNode,
  onNodeRef?: (id: string) => void,
): ReactNode {
  if (!onNodeRef) return children;
  if (!Array.isArray(children)) {
    if (typeof children === "string") {
      return <TextWithNodeRefs text={children} onNodeRef={onNodeRef} />;
    }
    return children;
  }
  return children.map((child, i) => {
    if (typeof child === "string") {
      return <TextWithNodeRefs key={i} text={child} onNodeRef={onNodeRef} />;
    }
    return child;
  });
}
