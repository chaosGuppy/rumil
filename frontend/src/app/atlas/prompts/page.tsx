import Link from "next/link";
import { atlasFetch } from "../_lib/fetch";
import { Crumbs } from "../_components/Crumbs";
import { Filter } from "../_components/Filter";

export const metadata = { title: "prompts" };

export default async function PromptsList() {
  const names = await atlasFetch<string[]>("/api/atlas/registry/prompts", []);
  const sorted = names.slice().sort();

  const items = sorted.map((n) => ({
    searchKey: n,
    node: (
      <Link
        key={n}
        href={`/atlas/prompts/${encodeURIComponent(n)}`}
        className="atlas-row"
      >
        <div className="atlas-row-name">{n}</div>
        <div className="atlas-row-desc">
          <span style={{ color: "var(--a-muted)", fontFamily: "var(--a-mono)", fontSize: "0.78rem" }}>
            prompts/{n}
          </span>
        </div>
        <div className="atlas-row-meta">
          <span className="atlas-chip is-muted">md</span>
        </div>
      </Link>
    ),
  }));

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs items={[{ label: "atlas", href: "/atlas" }, { label: "prompts" }]} />
          <h1 className="is-sans">prompt files</h1>
          <p className="atlas-lede">
            The markdown that runs the system. <span className="atlas-mono">preamble.md</span>{" "}
            defines the workspace model and is shared across all calls;
            everything else is call- or phase-specific.
          </p>
        </div>
      </div>
      <Filter items={items} placeholder="filter prompt files…" />
    </div>
  );
}
