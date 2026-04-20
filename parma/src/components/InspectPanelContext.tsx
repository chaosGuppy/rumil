"use client";

import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";

interface InspectPanelContextValue {
  openInspect: (shortId: string) => void;
  closeInspect: () => void;
  openShortId: string | null;
  // Promote a short id onto the pane stack (if a promote handler is
  // registered — typically by the active view's page component). Closes the
  // drawer as a side effect. Falls back to openInspect when no promote
  // handler is registered, so callers don't need to null-check.
  promoteToPane: (shortId: string) => void;
  // Register a handler that maps a short id → pane-stack push. The view
  // that owns the URL (e.g. StackedPanes) calls this on mount and passes
  // null on unmount.
  registerPromoteHandler: (
    handler: ((shortId: string) => void) | null,
  ) => void;
  // Jump into TRACE view mode for the given run, optionally preselecting a
  // call. Routed through a handler the QuestionViewPage registers, so the
  // provider stays URL-agnostic. Falls back to a no-op if no handler is
  // registered (e.g. operator views).
  openTrace: (runId: string, callId?: string) => void;
  registerTraceHandler: (
    handler: ((runId: string, callId?: string) => void) | null,
  ) => void;
}

const InspectPanelCtx = createContext<InspectPanelContextValue | null>(null);

// Wrap the app shell so any component can call useInspectPanel().openInspect(id).
// Mounts the single global inspect drawer at the tree's edge.
export function InspectPanelProvider({ children }: { children: ReactNode }) {
  const [openShortId, setOpenShortId] = useState<string | null>(null);
  const promoteHandlerRef = useRef<((id: string) => void) | null>(null);
  const traceHandlerRef = useRef<
    ((runId: string, callId?: string) => void) | null
  >(null);

  const openInspect = useCallback((shortId: string) => {
    // Accept the 8-char prefix form (most common in-body usage) but also
    // tolerate a full uuid — slice to 8 regardless.
    setOpenShortId(shortId.slice(0, 8));
  }, []);

  const closeInspect = useCallback(() => setOpenShortId(null), []);

  const registerPromoteHandler = useCallback(
    (handler: ((shortId: string) => void) | null) => {
      promoteHandlerRef.current = handler;
    },
    [],
  );

  const promoteToPane = useCallback((shortId: string) => {
    const id = shortId.slice(0, 8);
    const handler = promoteHandlerRef.current;
    if (handler) {
      handler(id);
      // Close the drawer — the pane is the richer surface now.
      setOpenShortId(null);
    } else {
      // No pane-owning view mounted → fall back to the drawer.
      setOpenShortId(id);
    }
  }, []);

  const openTrace = useCallback((runId: string, callId?: string) => {
    const handler = traceHandlerRef.current;
    if (handler) {
      handler(runId, callId);
      // Close the drawer; the trace view is taking over.
      setOpenShortId(null);
    }
  }, []);

  const registerTraceHandler = useCallback(
    (handler: ((runId: string, callId?: string) => void) | null) => {
      traceHandlerRef.current = handler;
    },
    [],
  );

  return (
    <InspectPanelCtx.Provider
      value={{
        openInspect,
        closeInspect,
        openShortId,
        promoteToPane,
        registerPromoteHandler,
        openTrace,
        registerTraceHandler,
      }}
    >
      {children}
    </InspectPanelCtx.Provider>
  );
}

// Safe in components that may render outside the provider (e.g. operator
// views) — returns a no-op openInspect so callers don't need to null-check.
export function useInspectPanel(): InspectPanelContextValue {
  const ctx = useContext(InspectPanelCtx);
  if (ctx) return ctx;
  return {
    openInspect: () => {},
    closeInspect: () => {},
    openShortId: null,
    promoteToPane: () => {},
    registerPromoteHandler: () => {},
    openTrace: () => {},
    registerTraceHandler: () => {},
  };
}
