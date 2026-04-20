"use client";

import { useEffect, useState } from "react";
import { BoundaryList } from "@/components/operator/BoundaryList";
import { fetchProjects } from "@/lib/api";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import type { Project } from "@/lib/types";

export default function LlmBoundaryPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useDocumentTitle(["llm-boundary"], "Rumil operator");

  useEffect(() => {
    fetchProjects()
      .then((rows) => {
        setProjects(rows);
        if (rows.length > 0) {
          const stored =
            typeof window !== "undefined"
              ? window.localStorage.getItem("op-llm-boundary-project")
              : null;
          const initial =
            stored && rows.find((p) => p.id === stored) ? stored : rows[0].id;
          setProjectId(initial);
        }
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  function selectProject(id: string) {
    setProjectId(id);
    if (typeof window !== "undefined") {
      window.localStorage.setItem("op-llm-boundary-project", id);
    }
  }

  if (loading) return <div className="op-trace-list-empty">Loading workspaces...</div>;
  if (error) return <div className="op-trace-list-empty">Error: {error}</div>;
  if (projects.length === 0)
    return <div className="op-trace-list-empty">No workspaces.</div>;

  return (
    <div className="op-boundary">
      <div className="op-boundary-header">
        <h1 className="op-trace-list-title">LLM boundary</h1>
        <label className="op-boundary-ws-label">
          workspace
          <select
            className="op-boundary-ws-select"
            value={projectId ?? ""}
            onChange={(e) => selectProject(e.target.value)}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {projectId ? <BoundaryList projectId={projectId} /> : null}
    </div>
  );
}
