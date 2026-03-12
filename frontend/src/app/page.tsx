import Link from "next/link";
import type { Project } from "@/api";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function getProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/api/projects`, {
    cache: "no-store",
  });
  if (!res.ok) return [];
  return res.json();
}

export default async function Home() {
  const projects = await getProjects();

  return (
    <main className="max-w-4xl mx-auto p-8">
      <h1 className="text-3xl font-bold mb-8">Differential</h1>
      <h2 className="text-xl font-semibold mb-4">Workspaces</h2>
      {projects.length === 0 ? (
        <p className="text-gray-500">
          No workspaces found. Make sure the API server is running and the database
          has data.
        </p>
      ) : (
        <ul className="space-y-3">
          {projects.map((project) => (
            <li key={project.id}>
              <Link
                href={`/projects/${project.id}`}
                className="block p-4 border rounded-lg hover:bg-gray-50 dark:hover:bg-gray-900 transition-colors"
              >
                <span className="font-medium">{project.name}</span>
                <span className="text-gray-500 text-sm ml-3">
                  {new Date(project.created_at).toLocaleDateString()}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
