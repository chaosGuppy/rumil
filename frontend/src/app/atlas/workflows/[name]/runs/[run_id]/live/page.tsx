import Link from "next/link";
import { notFound } from "next/navigation";
import type { LiveRunSnapshot } from "@/api";
import { atlasFetch } from "../../../../../_lib/fetch";
import { Crumbs } from "../../../../../_components/Crumbs";
import { CrossLink } from "../../../../../_components/CrossLink";
import { LiveRunner } from "./LiveRunner";

export const metadata = { title: "live run" };

export default async function LiveRunPage({
  params,
}: {
  params: Promise<{ name: string; run_id: string }>;
}) {
  const { name, run_id } = await params;
  const path = `/api/atlas/runs/${encodeURIComponent(run_id)}/live`;
  const initial = await atlasFetch<LiveRunSnapshot | null>(path, null);
  if (!initial) notFound();

  // Client polls directly against the public API URL when set; falls back
  // to the same-origin path (works when API and FE are colocated, e.g.
  // behind a reverse proxy).
  const clientApiBase = process.env.NEXT_PUBLIC_API_URL || "";
  const pollUrl = `${clientApiBase}${path}`;

  return (
    <div>
      <div className="atlas-page-head">
        <div className="atlas-page-head-main">
          <Crumbs
            items={[
              { label: "atlas", href: "/atlas" },
              { label: "workflows", href: "/atlas/workflows" },
              { label: name, href: `/atlas/workflows/${name}` },
              { label: "runs", href: `/atlas/workflows/${name}/runs` },
              { label: run_id.slice(0, 8) },
              { label: "live" },
            ]}
          />
          <h1>
            {name}{" "}
            <span style={{ color: "var(--a-muted)", fontWeight: 400 }}>
              · {run_id.slice(0, 8)} · live
            </span>
          </h1>
          <div className="atlas-chip-row" style={{ marginBottom: "0.85rem" }}>
            <Link
              href={`/atlas/workflows/${encodeURIComponent(name)}/runs/${encodeURIComponent(run_id)}`}
              className="atlas-chip"
            >
              post-hoc overlay →
            </Link>
            <Link
              href={`/atlas/runs/${encodeURIComponent(run_id)}/flow`}
              className="atlas-chip is-accent"
            >
              run flow →
            </Link>
            <CrossLink to={`/traces/${run_id}`} chip>
              full trace
            </CrossLink>
          </div>
          <p className="atlas-lede">
            Stages light up as they fire. Polls every 2s while the run is
            in-flight; settles into the final overlay once no calls are
            pending or running.
          </p>
        </div>
      </div>

      <LiveRunner initial={initial} apiUrl={pollUrl} />
    </div>
  );
}
