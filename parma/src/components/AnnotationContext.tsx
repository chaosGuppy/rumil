"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import type { AnnotationEvent } from "@/lib/annotations";
import { listPageAnnotationsBatch } from "@/lib/annotations";

// AnnotationContext — one source of truth for annotations attached to the
// pages currently rendered. Consumers:
//   - PageContent subscribes to a page's annotations to render spans +
//     page-level indicator
//   - Selection toolbar / endorse / dispute / page-level widgets mutate by
//     calling invalidate() after POSTing
//
// We intentionally batch at the tree level: when a view renders 40 items,
// we don't want 40 parallel fetches interleaving with concept loads. The
// provider collects page ids from its children via register() and fetches
// them in one pass (debounced one tick).

interface AnnotationContextValue {
  annotationsForPage: (pageId: string) => AnnotationEvent[];
  register: (pageId: string) => void;
  unregister: (pageId: string) => void;
  invalidate: (pageId?: string) => void;
}

const AnnotationCtx = createContext<AnnotationContextValue | null>(null);

export function AnnotationProvider({ children }: { children: ReactNode }) {
  const [byPage, setByPage] = useState<Map<string, AnnotationEvent[]>>(
    () => new Map(),
  );
  // A ref-mirror of byPage so register() can see fresh data without being
  // re-created when byPage updates — prevents churning child effects every
  // time a batch resolves.
  const byPageRef = useRef(byPage);
  byPageRef.current = byPage;
  const registered = useRef<Map<string, number>>(new Map());
  const pending = useRef<Set<string>>(new Set());
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(async () => {
    const ids = Array.from(pending.current);
    pending.current.clear();
    flushTimer.current = null;
    if (ids.length === 0) return;
    const fetched = await listPageAnnotationsBatch(ids);
    setByPage((prev) => {
      const next = new Map(prev);
      for (const [id, rows] of fetched) next.set(id, rows);
      return next;
    });
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushTimer.current) return;
    flushTimer.current = setTimeout(flush, 16);
  }, [flush]);

  const register = useCallback(
    (pageId: string) => {
      const count = registered.current.get(pageId) ?? 0;
      registered.current.set(pageId, count + 1);
      if (count === 0 && !byPageRef.current.has(pageId)) {
        pending.current.add(pageId);
        scheduleFlush();
      }
    },
    [scheduleFlush],
  );

  const unregister = useCallback((pageId: string) => {
    const count = registered.current.get(pageId) ?? 0;
    if (count <= 1) registered.current.delete(pageId);
    else registered.current.set(pageId, count - 1);
  }, []);

  const invalidate = useCallback(
    (pageId?: string) => {
      if (pageId) {
        pending.current.add(pageId);
      } else {
        for (const id of registered.current.keys()) pending.current.add(id);
      }
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const annotationsForPage = useCallback(
    (pageId: string) => byPage.get(pageId) ?? [],
    [byPage],
  );

  const value = useMemo<AnnotationContextValue>(
    () => ({ annotationsForPage, register, unregister, invalidate }),
    [annotationsForPage, register, unregister, invalidate],
  );

  useEffect(() => {
    return () => {
      if (flushTimer.current) clearTimeout(flushTimer.current);
    };
  }, []);

  return (
    <AnnotationCtx.Provider value={value}>{children}</AnnotationCtx.Provider>
  );
}

// Safe fallback: components that live outside a provider (operator views,
// trace viewer, etc.) silently get an empty annotation list. This matches
// the InspectPanelContext pattern.
export function useAnnotations(): AnnotationContextValue {
  const ctx = useContext(AnnotationCtx);
  if (ctx) return ctx;
  return {
    annotationsForPage: () => [],
    register: () => {},
    unregister: () => {},
    invalidate: () => {},
  };
}

// Register a page id with the provider for its lifetime. Use this in any
// component that's about to render annotation overlays for a page — it
// ensures the id gets into the batch fetcher exactly once.
export function useRegisterPage(pageId: string | null | undefined) {
  const { register, unregister } = useAnnotations();
  useEffect(() => {
    if (!pageId) return;
    register(pageId);
    return () => unregister(pageId);
  }, [pageId, register, unregister]);
}
