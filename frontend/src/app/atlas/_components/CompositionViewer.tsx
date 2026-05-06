"use client";
import Link from "next/link";
import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { PromptComposition, PromptPart, PromptSection } from "@/api";

type Mode = "concise" | "expanded" | "raw";

function fmtChars(n: number | undefined | null): string {
  if (!n) return "0";
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return `${n}`;
}

function shortenName(name: string): string {
  return name.replace(/\.md$/, "");
}

export function CompositionViewer({
  composition,
  initialMode = "concise",
}: {
  composition: PromptComposition;
  initialMode?: Mode;
}) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const parts = composition.parts ?? [];
  const systemParts = parts.filter((p) => (p.location ?? "system") === "system");
  const userParts = parts.filter((p) => p.location === "user");

  return (
    <div className="atlas-comp">
      <div className="atlas-comp-head">
        <span className="atlas-comp-head-title">system prompt composition</span>
        <span className="atlas-comp-head-meta">
          {parts.length} part{parts.length === 1 ? "" : "s"} ·{" "}
          {fmtChars(composition.total_chars)} chars
        </span>
        <div className="atlas-comp-toggle" role="tablist">
          {(["concise", "expanded", "raw"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              onClick={() => setMode(m)}
              className={mode === m ? "is-active" : ""}
            >
              {m}
            </button>
          ))}
        </div>
      </div>
      <div className="atlas-comp-body">
        {mode === "concise" && (
          <ConciseView systemParts={systemParts} userParts={userParts} />
        )}
        {mode === "expanded" && (
          <ExpandedView systemParts={systemParts} userParts={userParts} />
        )}
        {mode === "raw" && (
          <RawView parts={parts} />
        )}
      </div>
    </div>
  );
}

function ConciseView({
  systemParts,
  userParts,
}: {
  systemParts: PromptPart[];
  userParts: PromptPart[];
}) {
  return (
    <>
      {systemParts.length > 0 && (
        <div className="atlas-comp-row">
          <span className="atlas-comp-row-label">system message</span>
          <div className="atlas-comp-row-chips">
            {systemParts.map((p, i) => <PartPill key={`${p.name}-${i}`} part={p} />)}
          </div>
        </div>
      )}
      {userParts.length > 0 && (
        <div className="atlas-comp-row">
          <span className="atlas-comp-row-label">user message</span>
          <div className="atlas-comp-row-chips">
            {userParts.map((p, i) => <PartPill key={`${p.name}-${i}`} part={p} />)}
          </div>
        </div>
      )}
    </>
  );
}

function PartPill({ part }: { part: PromptPart }) {
  const exists = part.exists !== false;
  const optional = !!part.optional;
  const role = part.role ?? "?";
  const cls = [
    "atlas-comp-pill",
    optional ? "is-optional" : "",
    !exists ? "is-missing" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <Link href={`/atlas/prompts/${encodeURIComponent(part.name)}`} className={cls}>
      <span className="atlas-comp-pill-role">{role}</span>
      <span className="atlas-comp-pill-name">
        {role === "preamble" || role === "grounding" || role === "citations"
          ? role
          : shortenName(part.name)}
      </span>
      <span className="atlas-comp-pill-meta">{fmtChars(part.char_count)}c</span>
      {part.condition && (
        <span className="atlas-comp-pill-cond" title={part.condition}>
          if · {part.condition.length > 32 ? part.condition.slice(0, 30) + "…" : part.condition}
        </span>
      )}
    </Link>
  );
}

function ExpandedView({
  systemParts,
  userParts,
}: {
  systemParts: PromptPart[];
  userParts: PromptPart[];
}) {
  return (
    <>
      {systemParts.length > 0 && (
        <>
          <div className="atlas-comp-row-label" style={{ marginBottom: "0.4rem" }}>
            system message
          </div>
          {systemParts.map((p, i) => <PartCard key={`${p.name}-${i}`} part={p} />)}
        </>
      )}
      {userParts.length > 0 && (
        <>
          <div className="atlas-comp-row-label" style={{ margin: "1rem 0 0.4rem" }}>
            user message
          </div>
          {userParts.map((p, i) => <PartCard key={`${p.name}-${i}`} part={p} />)}
        </>
      )}
    </>
  );
}

function PartCard({ part }: { part: PromptPart }) {
  const sections = part.sections ?? [];
  const exists = part.exists !== false;
  return (
    <div className="atlas-part-card">
      <div className="atlas-part-card-head">
        <span className="atlas-part-card-role">{part.role}</span>
        <Link
          href={`/atlas/prompts/${encodeURIComponent(part.name)}`}
          className="atlas-part-card-name"
        >
          {part.name}
        </Link>
        {part.optional && (
          <span className="atlas-chip is-flag" style={{ fontSize: "0.62rem" }}>
            optional
          </span>
        )}
        {!exists && (
          <span className="atlas-chip is-warm" style={{ fontSize: "0.62rem" }}>
            missing
          </span>
        )}
        <span className="atlas-part-card-meta">
          {part.condition && (
            <span style={{ color: "var(--a-flag)" }}>if · {part.condition}</span>
          )}
          <span>{fmtChars(part.char_count)} chars</span>
          <span>{sections.length} §</span>
        </span>
      </div>
      <div className="atlas-part-card-body">
        {sections.length === 0 ? (
          <div style={{ fontFamily: "var(--a-mono)", fontSize: "0.74rem", color: "var(--a-muted)" }}>
            no parsed sections — see{" "}
            <Link href={`/atlas/prompts/${encodeURIComponent(part.name)}`}>raw file</Link>
          </div>
        ) : (
          sections.map((s, i) => <SectionDisclosure key={i} section={s} />)
        )}
      </div>
    </div>
  );
}

function SectionDisclosure({ section }: { section: PromptSection }) {
  const title = section.title || "(intro)";
  return (
    <details className="atlas-section-card">
      <summary>
        <span className="atlas-section-card-title">{title}</span>
        <span className="atlas-section-card-meta">
          h{section.level} · {fmtChars(section.char_count)}c
        </span>
      </summary>
      <div className="atlas-section-card-body atlas-markdown">
        <Markdown remarkPlugins={[remarkGfm]}>{section.body || ""}</Markdown>
      </div>
    </details>
  );
}

function RawView({ parts }: { parts: PromptPart[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div className="atlas-comp-row-label" style={{ marginBottom: "0.2rem" }}>
        raw files (open each on its own page)
      </div>
      {parts.map((p, i) => (
        <Link
          key={`${p.name}-${i}`}
          href={`/atlas/prompts/${encodeURIComponent(p.name)}`}
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr auto",
            gap: "0.75rem",
            padding: "0.45rem 0.6rem",
            border: "1px solid var(--a-line)",
            background: "var(--a-bg-paper)",
            fontFamily: "var(--a-mono)",
            fontSize: "0.78rem",
            color: "var(--a-fg)",
            textDecoration: "none",
            alignItems: "baseline",
          }}
        >
          <span style={{ color: "var(--a-muted)", fontSize: "0.66rem", letterSpacing: "0.1em", textTransform: "uppercase" }}>
            {p.role}
          </span>
          <span style={{ color: "var(--a-rule)" }}>{p.name}</span>
          <span style={{ color: "var(--a-muted)", fontSize: "0.7rem" }}>
            {fmtChars(p.char_count)} chars · {(p.sections ?? []).length} sections →
          </span>
        </Link>
      ))}
    </div>
  );
}
