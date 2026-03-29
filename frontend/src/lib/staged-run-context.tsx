"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

const STORAGE_KEY = "activeStagedRunId";

interface StagedRunContextValue {
  activeStagedRunId: string | null;
  setActiveStagedRunId: (id: string | null) => void;
}

const StagedRunContext = createContext<StagedRunContextValue>({
  activeStagedRunId: null,
  setActiveStagedRunId: () => {},
});

export function StagedRunProvider({ children }: { children: React.ReactNode }) {
  const [activeStagedRunId, setRaw] = useState<string | null>(null);

  useEffect(() => {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    if (stored) setRaw(stored);
  }, []);

  const setActiveStagedRunId = useCallback((id: string | null) => {
    setRaw(id);
    if (id) {
      sessionStorage.setItem(STORAGE_KEY, id);
    } else {
      sessionStorage.removeItem(STORAGE_KEY);
    }
  }, []);

  return (
    <StagedRunContext.Provider value={{ activeStagedRunId, setActiveStagedRunId }}>
      {children}
    </StagedRunContext.Provider>
  );
}

export function useStagedRun() {
  return useContext(StagedRunContext);
}
