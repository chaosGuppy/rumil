"use client";

import { useEffect } from "react";

export function useDocumentTitle(title: string | null | undefined): void {
  useEffect(() => {
    if (!title) return;
    document.title = `${title} · Rumil`;
  }, [title]);
}
