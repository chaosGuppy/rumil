"use client";

import { createContext, useContext, useState, useEffect } from "react";
import { fetchConcepts } from "@/lib/api";
import type { ConceptInfo } from "@/lib/api";

const ConceptCtx = createContext<ConceptInfo[]>([]);

export function ConceptProvider({
  workspace,
  children,
}: {
  workspace: string;
  children: React.ReactNode;
}) {
  const [concepts, setConcepts] = useState<ConceptInfo[]>([]);

  useEffect(() => {
    fetchConcepts(workspace)
      .then(setConcepts)
      .catch(() => setConcepts([]));
  }, [workspace]);

  return (
    <ConceptCtx.Provider value={concepts}>{children}</ConceptCtx.Provider>
  );
}

export function useConcepts(): ConceptInfo[] {
  return useContext(ConceptCtx);
}
