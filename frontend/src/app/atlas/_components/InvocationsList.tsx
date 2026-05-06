"use client";
import Link from "next/link";
import { useState } from "react";
import type {
  InvocationIndex,
  InvocationRecord,
  InvocationRequest,
  InvocationResponse,
} from "@/api";
import { fmtCost, fmtRelative, fmtWhen } from "../_lib/format";

type Kind = "call_type" | "dispatch" | "move";

const SHORT = (s: string | null | undefined, n = 8): string =>
  !s ? "—" : s.length > n ? s.slice(0, n) : s;

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function fmtTokens(n: number | undefined | null): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

function trunc(s: string | null | undefined, n: number): string {
  if (!s) return "";
  if (s.length <= n) return s;
  return `${s.slice(0, n)}…`;
}

function previewToolInput(
  toolName: string,
  toolInput: { [key: string]: unknown },
): string {
  const fields = ["headline", "reason", "claim", "question", "name", "summary", "title", "answer"];
  for (const f of fields) {
    const v = toolInput[f];
    if (typeof v === "string" && v.trim()) {
      return trunc(v.trim(), 80);
    }
  }
  // fallback: stringify first non-trivial value
  for (const [k, v] of Object.entries(toolInput)) {
    if (typeof v === "string" && v.trim()) {
      return `${k}: ${trunc(v.trim(), 70)}`;
    }
  }
  return `${toolName}(…)`;
}

function previewRecordHeadline(rec: InvocationRecord): string | null {
  if (rec.match) return previewToolInput(rec.match.tool_name, rec.match.tool_input);
  if (rec.response.error) return trunc(rec.response.error, 90);
  if (rec.response.response_text) return trunc(rec.response.response_text, 90);
  // first tool call if any
  const tcs = rec.response.tool_calls ?? [];
  if (tcs.length > 0) {
    const first = tcs[0];
    const name = (first.name as string | undefined) ?? "tool";
    const input = (first.input as { [key: string]: unknown } | undefined) ?? {};
    return previewToolInput(name, input);
  }
  return null;
}

export function InvocationsList({
  index,
  kind,
  showCallTypeChip = false,
}: {
  index: InvocationIndex | null;
  kind: Kind;
  showCallTypeChip?: boolean;
}) {
  const items = index?.items ?? [];
  const nScanned = index?.n_scanned ?? 0;
  const truncated = index?.truncated ?? false;

  return (
    <section className="atlas-section atlas-invocations">
      <div className="atlas-section-head">
        <h2>recent invocations</h2>
        <span className="atlas-section-meta">
          {nScanned} scanned
          <span className="atlas-sep">·</span>
          {items.length} matched
          <span className="atlas-sep">·</span>
          {kind.replace("_", " ")}
          {truncated && (
            <>
              <span className="atlas-sep">·</span>
              <span className="atlas-inv-truncated">older not surfaced</span>
            </>
          )}
        </span>
      </div>

      {items.length === 0 ? (
        <div className="atlas-inv-empty">
          no recent invocations
          <span className="atlas-inv-empty-sub">
            {nScanned > 0
              ? `scanned ${nScanned} exchange${nScanned === 1 ? "" : "s"} without a match`
              : "no exchanges in scan window"}
          </span>
        </div>
      ) : (
        <ol className="atlas-inv-list">
          {items.map((rec) => (
            <InvocationRow
              key={rec.exchange_id}
              rec={rec}
              showCallTypeChip={showCallTypeChip}
            />
          ))}
        </ol>
      )}
    </section>
  );
}

function InvocationRow({
  rec,
  showCallTypeChip,
}: {
  rec: InvocationRecord;
  showCallTypeChip: boolean;
}) {
  const [open, setOpen] = useState(false);
  const headline = previewRecordHeadline(rec);
  const phaseLabel =
    rec.phase && rec.round != null
      ? `${rec.phase} · r${rec.round}`
      : rec.phase ?? null;

  return (
    <li className={`atlas-inv-item${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="atlas-inv-row"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="atlas-inv-chevron" aria-hidden>
          {open ? "▾" : "▸"}
        </span>

        <span
          className="atlas-inv-when"
          title={rec.created_at ? fmtWhen(rec.created_at) : ""}
        >
          {fmtRelative(rec.created_at)}
        </span>

        <span className="atlas-inv-model">{rec.request.model ?? "—"}</span>

        {phaseLabel && <span className="atlas-inv-phase">{phaseLabel}</span>}

        <span className="atlas-inv-cost">{fmtCost(rec.cost_usd)}</span>
        <span className="atlas-inv-dur">{fmtDuration(rec.duration_ms)}</span>

        {rec.has_error && <span className="atlas-inv-err-pill">err</span>}

        {headline && (
          <span className="atlas-inv-preview" title={headline}>
            {headline}
          </span>
        )}

        <span className="atlas-inv-spacer" />

        {showCallTypeChip && rec.call_type && (
          <Link
            href={`/atlas/calls/${encodeURIComponent(rec.call_type)}`}
            className="atlas-inv-link"
            onClick={(e) => e.stopPropagation()}
          >
            {rec.call_type}
          </Link>
        )}

        {rec.run_id && (
          <Link
            href={`/atlas/runs/${rec.run_id}/flow`}
            className="atlas-inv-id"
            onClick={(e) => e.stopPropagation()}
            title={rec.run_id}
          >
            run·{SHORT(rec.run_id)}
          </Link>
        )}

        <Link
          href={`/atlas/exchanges/${rec.exchange_id}/playground`}
          className="atlas-inv-id"
          onClick={(e) => e.stopPropagation()}
          title={rec.exchange_id}
        >
          ex·{SHORT(rec.exchange_id)}
        </Link>

        <Link
          href={`/atlas/exchanges/${rec.exchange_id}/playground`}
          className="atlas-inv-fork"
          onClick={(e) => e.stopPropagation()}
          title="open in fork playground"
        >
          fork →
        </Link>
      </button>

      {open && (
        <div className="atlas-inv-body">
          <RequestBlock req={rec.request} />
          <ResponseBlock
            res={rec.response}
            matchedToolUseId={rec.match?.tool_use_id ?? null}
            matchedBlockIndex={rec.match?.block_index ?? null}
          />
        </div>
      )}
    </li>
  );
}

function RequestBlock({ req }: { req: InvocationRequest }) {
  const messages = req.messages ?? [];
  const tools = req.tools ?? [];
  const system = req.system ?? "";
  const SYSTEM_INLINE_MAX = 2000;

  return (
    <div className="atlas-inv-block">
      <div className="atlas-inv-block-head">
        <span className="atlas-inv-block-tag">request</span>
        <span className="atlas-inv-block-meta">
          POST /v1/messages
          <span className="atlas-sep">·</span>
          {req.model ?? "—"}
          {req.temperature != null && (
            <>
              <span className="atlas-sep">·</span>
              temp {req.temperature}
            </>
          )}
          {req.max_tokens != null && (
            <>
              <span className="atlas-sep">·</span>
              max_tokens {req.max_tokens}
            </>
          )}
          {req.thinking && (
            <>
              <span className="atlas-sep">·</span>
              <span className="atlas-inv-tag-thinking">thinking</span>
            </>
          )}
        </span>
      </div>

      {system && (
        <SystemSection system={system} inlineMax={SYSTEM_INLINE_MAX} />
      )}

      {messages.length > 0 && (
        <div className="atlas-inv-messages">
          {messages.map((m, i) => (
            <MessageBlock key={i} message={m} />
          ))}
        </div>
      )}

      {tools.length > 0 && <ToolsSection tools={tools} />}
    </div>
  );
}

function SystemSection({
  system,
  inlineMax,
}: {
  system: string;
  inlineMax: number;
}) {
  const long = system.length > inlineMax;
  const [open, setOpen] = useState(!long);
  return (
    <div className="atlas-inv-msg">
      <div className="atlas-inv-msg-head">
        <span className="atlas-inv-role">system</span>
        <span className="atlas-inv-msg-meta">
          {system.length.toLocaleString()} chars
        </span>
        {long && (
          <button
            type="button"
            className="atlas-inv-mini-toggle"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "collapse" : "expand"}
          </button>
        )}
      </div>
      <pre className="atlas-inv-text">
        {open ? system : `${system.slice(0, 600)}…`}
      </pre>
    </div>
  );
}

function MessageBlock({ message }: { message: { [key: string]: unknown } }) {
  const role = (message.role as string | undefined) ?? "?";
  const content = message.content;
  return (
    <div className="atlas-inv-msg">
      <div className="atlas-inv-msg-head">
        <span className={`atlas-inv-role is-${role}`}>{role}</span>
      </div>
      <MessageContent content={content} />
    </div>
  );
}

function MessageContent({ content }: { content: unknown }) {
  if (typeof content === "string") {
    return <pre className="atlas-inv-text">{content}</pre>;
  }
  if (!Array.isArray(content)) {
    return (
      <pre className="atlas-inv-text">
        {JSON.stringify(content, null, 2)}
      </pre>
    );
  }
  return (
    <div className="atlas-inv-blocks">
      {content.map((b, i) => (
        <ContentBlock key={i} block={b as { [key: string]: unknown }} />
      ))}
    </div>
  );
}

function ContentBlock({ block }: { block: { [key: string]: unknown } }) {
  const type = (block.type as string | undefined) ?? "unknown";
  if (type === "text") {
    return <pre className="atlas-inv-text">{(block.text as string) ?? ""}</pre>;
  }
  if (type === "tool_use") {
    const name = (block.name as string | undefined) ?? "tool";
    const input = block.input ?? {};
    return (
      <div className="atlas-inv-tooluse">
        <div className="atlas-inv-tooluse-head">
          <span className="atlas-inv-tooluse-tag">tool_use</span>
          <span className="atlas-inv-tooluse-name">{name}</span>
        </div>
        <pre className="atlas-inv-text is-json">
          {JSON.stringify(input, null, 2)}
        </pre>
      </div>
    );
  }
  if (type === "tool_result") {
    const toolUseId = (block.tool_use_id as string | undefined) ?? "";
    const inner = block.content;
    return (
      <div className="atlas-inv-toolresult">
        <div className="atlas-inv-tooluse-head">
          <span className="atlas-inv-tooluse-tag is-result">tool_result</span>
          {toolUseId && (
            <span className="atlas-inv-tooluse-name">
              ↳ {SHORT(toolUseId, 10)}
            </span>
          )}
        </div>
        <MessageContent content={inner} />
      </div>
    );
  }
  if (type === "thinking") {
    return <ThinkingBlock text={(block.thinking as string) ?? ""} />;
  }
  return (
    <pre className="atlas-inv-text">
      {JSON.stringify(block, null, 2)}
    </pre>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const long = text.length > 400;
  const [open, setOpen] = useState(false);
  const shown = open || !long ? text : `${text.slice(0, 400)}…`;
  return (
    <div className="atlas-inv-thinking">
      <div className="atlas-inv-tooluse-head">
        <span className="atlas-inv-tooluse-tag is-thinking">thinking</span>
        <span className="atlas-inv-msg-meta">
          {text.length.toLocaleString()} chars
        </span>
        {long && (
          <button
            type="button"
            className="atlas-inv-mini-toggle"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "collapse" : "expand"}
          </button>
        )}
      </div>
      <pre className="atlas-inv-text is-muted">{shown}</pre>
    </div>
  );
}

function ToolsSection({
  tools,
}: {
  tools: Array<{ [key: string]: unknown }>;
}) {
  const [open, setOpen] = useState(false);
  const names = tools
    .map((t) => (t.name as string | undefined) ?? "?")
    .join(", ");
  return (
    <div className="atlas-inv-tools">
      <button
        type="button"
        className="atlas-inv-tools-head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="atlas-inv-chevron" aria-hidden>
          {open ? "▾" : "▸"}
        </span>
        <span className="atlas-inv-tooluse-tag">tools</span>
        <span className="atlas-inv-msg-meta">
          {tools.length} · {trunc(names, 90)}
        </span>
      </button>
      {open && (
        <pre className="atlas-inv-text is-json">
          {JSON.stringify(tools, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ResponseBlock({
  res,
  matchedToolUseId,
  matchedBlockIndex,
}: {
  res: InvocationResponse;
  matchedToolUseId: string | null;
  matchedBlockIndex: number | null;
}) {
  const content = res.content ?? [];
  const usage = res.usage ?? null;
  const inputTok = usage ? (usage["input_tokens"] as number | undefined) : null;
  const outputTok = usage ? (usage["output_tokens"] as number | undefined) : null;

  return (
    <div className="atlas-inv-block">
      <div className="atlas-inv-block-head">
        <span className="atlas-inv-block-tag is-response">response</span>
        <span className="atlas-inv-block-meta">
          {res.stop_reason ?? "—"}
          <span className="atlas-sep">·</span>
          in {fmtTokens(inputTok)} / out {fmtTokens(outputTok)}
        </span>
      </div>

      {res.error && (
        <div className="atlas-inv-error">
          <span className="atlas-inv-tooluse-tag is-err">error</span>
          <pre className="atlas-inv-text">{res.error}</pre>
        </div>
      )}

      {content.length > 0 ? (
        <div className="atlas-inv-blocks">
          {content.map((b, i) => {
            const block = b as { [key: string]: unknown };
            const isMatched =
              (matchedToolUseId &&
                (block.id as string | undefined) === matchedToolUseId) ||
              (matchedBlockIndex != null && matchedBlockIndex === i);
            return (
              <ResponseContentBlock
                key={i}
                block={block}
                matched={!!isMatched}
              />
            );
          })}
        </div>
      ) : (
        res.response_text && (
          <pre className="atlas-inv-text">{res.response_text}</pre>
        )
      )}
    </div>
  );
}

function ResponseContentBlock({
  block,
  matched,
}: {
  block: { [key: string]: unknown };
  matched: boolean;
}) {
  const type = (block.type as string | undefined) ?? "unknown";
  if (type === "thinking") {
    return <ThinkingBlock text={(block.thinking as string) ?? ""} />;
  }
  if (type === "text") {
    return <pre className="atlas-inv-text">{(block.text as string) ?? ""}</pre>;
  }
  if (type === "tool_use") {
    const name = (block.name as string | undefined) ?? "tool";
    const input = block.input ?? {};
    return (
      <div
        className={`atlas-inv-tooluse${matched ? " is-matched" : ""}`}
      >
        <div className="atlas-inv-tooluse-head">
          <span className="atlas-inv-tooluse-tag">tool_use</span>
          <span className="atlas-inv-tooluse-name">{name}</span>
          {matched && <span className="atlas-inv-matched-pill">matched</span>}
        </div>
        <pre className="atlas-inv-text is-json">
          {JSON.stringify(input, null, 2)}
        </pre>
      </div>
    );
  }
  return (
    <pre className="atlas-inv-text">{JSON.stringify(block, null, 2)}</pre>
  );
}
