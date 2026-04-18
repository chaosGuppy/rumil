"use client";

import { createContext, useCallback, useContext, useState } from "react";
import type { ReactNode } from "react";
import { InspectPanel } from "./InspectPanel";

interface InspectPanelContextValue {
  openInspect: (shortId: string) => void;
  closeInspect: () => void;
  openShortId: string | null;
}

const InspectPanelCtx = createContext<InspectPanelContextValue | null>(null);

// Wrap the app shell so any component can call useInspectPanel().openInspect(id).
// Mounts the single global inspect drawer at the tree's edge.
export function InspectPanelProvider({ children }: { children: ReactNode }) {
  const [openShortId, setOpenShortId] = useState<string | null>(null);

  const openInspect = useCallback((shortId: string) => {
    // Accept the 8-char prefix form (most common in-body usage) but also
    // tolerate a full uuid — slice to 8 regardless.
    setOpenShortId(shortId.slice(0, 8));
  }, []);

  const closeInspect = useCallback(() => setOpenShortId(null), []);

  return (
    <InspectPanelCtx.Provider
      value={{ openInspect, closeInspect, openShortId }}
    >
      {children}
      <InspectPanel
        shortId={openShortId}
        onClose={closeInspect}
        onOpen={openInspect}
      />
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
  };
}
