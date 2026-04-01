import { API_BASE, serverFetch } from "@/lib/api-base";
import type { Project } from "@/api";

export async function fetchProjectName(
  projectId: string,
): Promise<string | undefined> {
  try {
    const res = await serverFetch(`${API_BASE}/api/projects/${projectId}`, {
      cache: "no-store",
    });
    if (!res.ok) return undefined;
    const project: Project = await res.json();
    return project.name;
  } catch {
    return undefined;
  }
}
