import Link from "next/link";
import type { Metadata } from "next";
import type { EssayMeta } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "./versus.css";

export const metadata: Metadata = { title: "versus" };

const DEFAULT_CRITERIA = ["standalone_quality", "informativeness", "substance_and_bite"];

async function listEssays(): Promise<EssayMeta[]> {
  const res = await serverFetch(`${API_BASE}/api/versus/essays`, { cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

export default async function VersusHome() {
  const essays = await listEssays();
  const apiOk = essays.length > 0;

  return (
    <div className="versus-shell">
      <VersusHeader breadcrumb="home" />
      <main className="versus-main">
        <h1 style={{ fontWeight: 300, fontSize: 26, margin: "0 0 8px" }}>
          versus — blind human judging
        </h1>
        <p className="versus-muted">
          Enter your name, pick a criterion, and start judging. Your judgments are stored alongside
          the model judges.
        </p>

        {!apiOk && (
          <div className="versus-card" style={{ marginBottom: 16, borderColor: "#c63d3d" }}>
            <strong>No essays found.</strong> Run <code>uv run scripts/fetch_essays.py</code> in the
            versus directory and ensure <code>VERSUS_CONFIG_PATH</code> points at a valid config.
          </div>
        )}

        <form
          method="get"
          action="/versus/judge"
          className="versus-card"
          style={{ maxWidth: 560 }}
        >
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
            <label htmlFor="name" style={{ minWidth: 90 }}>Judge name</label>
            <input
              id="name"
              name="name"
              type="text"
              required
              placeholder="e.g. alice"
              className="versus-input"
              style={{ flex: 1 }}
            />
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <label htmlFor="criterion" style={{ minWidth: 90 }}>Criterion</label>
            <select id="criterion" name="criterion" className="versus-select">
              {DEFAULT_CRITERIA.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div style={{ marginTop: 20 }}>
            <button type="submit" className="versus-button">Start</button>
          </div>
        </form>

        <p style={{ marginTop: 24 }}>
          <Link href="/versus/inspect">Inspect →</Link>{" "}
          <span className="versus-muted">(view originals + prompt templates side-by-side)</span>
          {" · "}
          <Link href="/versus/results">Results →</Link>{" "}
          <span className="versus-muted">(gen-model × judge-model matrix + raw judgments)</span>
        </p>
      </main>
    </div>
  );
}
