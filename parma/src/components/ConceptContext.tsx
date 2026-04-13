"use client";

import { createContext, useContext, useState, useEffect } from "react";
import { fetchConcepts } from "@/lib/api";
import type { Page } from "@/lib/types";

const ConceptCtx = createContext<Page[]>([]);

export function ConceptProvider({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  const [concepts, setConcepts] = useState<Page[]>([]);

  useEffect(() => {
    fetchConcepts(projectId)
      .then(setConcepts)
      .catch(() => setConcepts([]));
  }, [projectId]);

  return (
    <ConceptCtx.Provider value={concepts}>{children}</ConceptCtx.Provider>
  );
}

export function useConcepts(): Page[] {
  return useContext(ConceptCtx);
}
