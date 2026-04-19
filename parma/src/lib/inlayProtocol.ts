// Pure-TS schema for the Inlay postMessage protocol (v1).
//
// This lives in src/lib/ (not src/components/) because it's pure
// data — no React — and we want to test it with Node's built-in
// test runner, which can't load .tsx files (jsx is not in the
// strip-types whitelist). InlayFrame.tsx re-exports these symbols
// so existing components can keep importing from one place.
//
// See planning/inlay-ui.md for the protocol definition.

export const INLAY_PROTOCOL_PREFIX = "rumil.inlay.v1:";

export type InlayInboundMessage =
  | { type: "rumil.inlay.v1:ready" }
  | { type: "rumil.inlay.v1:resize"; height: number }
  | { type: "rumil.inlay.v1:open_inspect"; page_id: string }
  | { type: "rumil.inlay.v1:navigate"; page_id: string }
  | { type: "rumil.inlay.v1:open_chat"; question_id?: string; prompt?: string }
  | { type: "rumil.inlay.v1:flag_page"; page_id: string; reason: string };

// The schema validator. Strict: if any required field is missing or
// has the wrong type, the message is rejected. We do NOT try to
// coerce — an Inlay that sends `height: "42"` is buggy and the host
// should refuse to resize it rather than silently re-parse.
//
// Returning `null` from here means "ignore this message"; the caller
// logs and moves on.
export function validateInlayMessage(
  raw: unknown,
): InlayInboundMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const data = raw as Record<string, unknown>;
  const type = data.type;
  if (typeof type !== "string") return null;
  if (!type.startsWith(INLAY_PROTOCOL_PREFIX)) return null;
  const kind = type.slice(INLAY_PROTOCOL_PREFIX.length);

  switch (kind) {
    case "ready":
      return { type: "rumil.inlay.v1:ready" };

    case "resize": {
      const height = data.height;
      if (typeof height !== "number" || !Number.isFinite(height)) return null;
      if (height < 0 || height > 20_000) return null;
      return { type: "rumil.inlay.v1:resize", height };
    }

    case "open_inspect": {
      const page_id = data.page_id;
      if (typeof page_id !== "string" || !page_id) return null;
      if (page_id.length > 128) return null;
      return { type: "rumil.inlay.v1:open_inspect", page_id };
    }

    case "navigate": {
      const page_id = data.page_id;
      if (typeof page_id !== "string" || !page_id) return null;
      if (page_id.length > 128) return null;
      return { type: "rumil.inlay.v1:navigate", page_id };
    }

    case "open_chat": {
      const question_id = data.question_id;
      const prompt = data.prompt;
      if (question_id !== undefined && typeof question_id !== "string") return null;
      if (prompt !== undefined && typeof prompt !== "string") return null;
      if (typeof prompt === "string" && prompt.length > 4000) return null;
      return {
        type: "rumil.inlay.v1:open_chat",
        question_id: typeof question_id === "string" ? question_id : undefined,
        prompt: typeof prompt === "string" ? prompt : undefined,
      };
    }

    case "flag_page": {
      const page_id = data.page_id;
      const reason = data.reason;
      if (typeof page_id !== "string" || !page_id) return null;
      if (typeof reason !== "string") return null;
      if (reason.length > 2000) return null;
      return { type: "rumil.inlay.v1:flag_page", page_id, reason };
    }

    default:
      return null;
  }
}
