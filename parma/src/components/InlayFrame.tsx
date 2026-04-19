"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { Page, QuestionView } from "@/lib/types";
import {
  INLAY_PROTOCOL_PREFIX,
  validateInlayMessage,
} from "@/lib/inlayProtocol";
import type { InlayInboundMessage } from "@/lib/inlayProtocol";
import { useInspectPanel } from "./InspectPanelContext";

// Re-export the protocol primitives so callers that already imported
// them from here keep working. New code should import directly from
// @/lib/inlayProtocol.
export { INLAY_PROTOCOL_PREFIX, validateInlayMessage };
export type { InlayInboundMessage };

// The `init` payload the host sends to the iframe after it signals
// ready. Kept deliberately thin — the iframe gets the stuff it needs
// to render a custom view of the question without having to make
// authenticated requests.
export interface InlayInitMessage {
  type: "rumil.inlay.v1:init";
  target: {
    id: string;
    short_id: string;
    headline: string;
    abstract: string;
    content: string;
  };
  view: QuestionView | null;
  child_questions: Array<{ id: string; short_id: string; headline: string }>;
  project: { id: string; name: string };
  user: { id: string | null; email: string | null };
  theme: Record<string, string>;
  capabilities: string[];
}

// Capture the live CSS custom properties the host advertises so the
// iframe can opt into host theming. Best-effort; in a headless test
// environment window may not be available.
function readHostTheme(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const root = document.documentElement;
  const styles = window.getComputedStyle(root);
  const vars = [
    "--bg",
    "--bg-pane",
    "--fg",
    "--fg-muted",
    "--fg-dim",
    "--border",
    "--border-strong",
    "--accent",
    "--node-claim",
    "--node-question",
  ];
  const out: Record<string, string> = {};
  for (const v of vars) {
    const val = styles.getPropertyValue(v).trim();
    if (val) out[v] = val;
  }
  return out;
}

// DEV-only warning — silenced in production so a noisy inlay doesn't
// bloat user consoles.
function devWarn(...args: unknown[]) {
  if (process.env.NODE_ENV !== "production") {
    console.warn("[InlayFrame]", ...args);
  }
}

interface InlayFrameProps {
  inlay: Page;
  view: QuestionView;
  project: { id: string; name: string };
  childQuestions?: Array<{ id: string; headline: string }>;
}

// Sandboxed iframe wrapper that renders an Inlay.
//
// Important: the iframe uses `sandbox="allow-scripts"` but NOT
// `allow-same-origin`, so it gets an opaque origin. That means:
//   - Every inbound postMessage event has `event.origin === "null"`.
//   - Our host→iframe postMessage targets "*" because there is no
//     concrete origin to compare against. This is safe because the
//     iframe can only exfiltrate data to its parent and has no
//     cookies / fetch credentials / cross-origin reach. It's the
//     load-bearing isolation for the Inlay feature.
export function InlayFrame({
  inlay,
  view,
  project,
  childQuestions = [],
}: InlayFrameProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const router = useRouter();
  const { openInspect } = useInspectPanel();
  const [height, setHeight] = useState<number>(480);

  // Stable init payload. Recomputed only when the underlying inputs
  // change so we don't spam the iframe with redundant init messages
  // on unrelated re-renders.
  const initPayload = useMemo<InlayInitMessage>(() => {
    return {
      type: "rumil.inlay.v1:init",
      target: {
        id: view.question.id,
        short_id: view.question.id.slice(0, 8),
        headline: view.question.headline,
        abstract: view.question.abstract ?? "",
        content: view.question.content ?? "",
      },
      view,
      child_questions: childQuestions.map((c) => ({
        id: c.id,
        short_id: c.id.slice(0, 8),
        headline: c.headline,
      })),
      project,
      user: { id: null, email: null },
      theme: readHostTheme(),
      capabilities: [
        "open_inspect",
        "navigate",
        "open_chat",
        "flag_page",
        "resize",
      ],
    };
  }, [view, childQuestions, project]);

  const sendInit = useCallback(() => {
    const frame = iframeRef.current;
    if (!frame || !frame.contentWindow) return;
    try {
      frame.contentWindow.postMessage(initPayload, "*");
    } catch (e) {
      devWarn("init postMessage failed", e);
    }
  }, [initPayload]);

  // Handle messages coming back from the iframe. We bind one listener
  // to window and dispatch by kind. Messages from iframes we don't
  // own will be rejected by the source-window check below.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const frame = iframeRef.current;
      if (!frame) return;
      // Only accept messages from *our* iframe. Other iframes on the
      // page (e.g. devtools, embeds) would otherwise be able to
      // spoof the protocol.
      if (event.source !== frame.contentWindow) return;
      const message = validateInlayMessage(event.data);
      if (!message) {
        devWarn("dropped malformed message", event.data);
        return;
      }
      switch (message.type) {
        case "rumil.inlay.v1:ready":
          sendInit();
          break;
        case "rumil.inlay.v1:resize":
          setHeight(Math.max(120, Math.min(20_000, message.height)));
          break;
        case "rumil.inlay.v1:open_inspect":
          openInspect(message.page_id);
          break;
        case "rumil.inlay.v1:navigate": {
          // The inlay passes short or full page ids. We forward to
          // the app router via the standard `?q=<id>` convention the
          // app uses for question navigation; non-question targets
          // fall back to inspect.
          const params = new URLSearchParams(window.location.search);
          params.set("q", message.page_id);
          router.push(`${window.location.pathname}?${params.toString()}`);
          break;
        }
        case "rumil.inlay.v1:open_chat":
          // MVP: no chat state yet — stash the requested prompt in
          // the URL so the chat panel (when open) can pick it up on
          // its next open. Concrete chat wiring is Phase 2.
          try {
            const detail = {
              question_id: message.question_id,
              prompt: message.prompt,
            };
            window.dispatchEvent(
              new CustomEvent("rumil:inlay:open_chat", { detail }),
            );
          } catch (e) {
            devWarn("open_chat dispatch failed", e);
          }
          break;
        case "rumil.inlay.v1:flag_page":
          try {
            const detail = {
              page_id: message.page_id,
              reason: message.reason,
            };
            window.dispatchEvent(
              new CustomEvent("rumil:inlay:flag_page", { detail }),
            );
          } catch (e) {
            devWarn("flag_page dispatch failed", e);
          }
          break;
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [sendInit, router, openInspect]);

  // If the payload changes post-init, refresh the iframe's view. The
  // iframe is expected to handle repeated init messages idempotently
  // (the example inlay does — see examples/inlays/forecast-card.html).
  useEffect(() => {
    sendInit();
  }, [sendInit]);

  return (
    <div
      style={{
        position: "relative",
        margin: "0 auto",
        maxWidth: "var(--article-max-width, 720px)",
        padding: "8px 0",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "10px",
          padding: "4px 2px 10px",
          fontFamily: "var(--font-mono-stack)",
          fontSize: "10px",
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "var(--fg-dim)",
        }}
      >
        <span>inlay</span>
        <span
          style={{
            fontFamily: "inherit",
            textTransform: "none",
            letterSpacing: 0,
          }}
        >
          {inlay.headline}
        </span>
        <span style={{ opacity: 0.6 }}>· {inlay.id.slice(0, 8)}</span>
      </div>
      <iframe
        ref={iframeRef}
        title={`Inlay: ${inlay.headline}`}
        srcDoc={inlay.content}
        sandbox="allow-scripts"
        style={{
          display: "block",
          width: "100%",
          height: `${height}px`,
          border: "1px solid var(--border)",
          borderRadius: "10px",
          background: "var(--bg-pane)",
          boxShadow: "var(--pane-shadow)",
          transition: "height 200ms ease-out",
        }}
        onLoad={() => {
          // Belt-and-braces: if the Inlay was cached by the browser
          // and window.onload never refires, posting init on iframe
          // load is a reasonable fallback. If the iframe also sends
          // `ready` we'll init twice, which is idempotent.
          sendInit();
        }}
      />
    </div>
  );
}
