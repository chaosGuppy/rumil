import Link from "next/link";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import type { Metadata } from "next";
import type { JudgmentSubmit, NextPairResponse } from "@/api/types.gen";
import { API_BASE, serverFetch } from "@/lib/api-base";
import { JudgeKeyboardShortcuts } from "@/components/versus/JudgeKeyboardShortcuts";
import { VersusHeader } from "@/components/versus/VersusHeader";
import "../versus.css";

export const metadata: Metadata = { title: "versus · judge" };

async function getNextPair(name: string, criterion: string): Promise<NextPairResponse | null> {
  const qs = new URLSearchParams({ name, criterion });
  const res = await serverFetch(`${API_BASE}/api/versus/next-pair?${qs}`, { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

async function submitVerdict(formData: FormData) {
  "use server";
  const body: JudgmentSubmit = {
    name: String(formData.get("name") ?? ""),
    criterion: String(formData.get("criterion") ?? ""),
    essay_id: String(formData.get("essay_id") ?? ""),
    prefix_hash: String(formData.get("prefix_hash") ?? ""),
    a: String(formData.get("a") ?? ""),
    b: String(formData.get("b") ?? ""),
    first_source: String(formData.get("first_source") ?? ""),
    second_source: String(formData.get("second_source") ?? ""),
    verdict: String(formData.get("verdict") ?? ""),
    note: String(formData.get("note") ?? ""),
  };
  const res = await serverFetch(`${API_BASE}/api/versus/judgments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to submit verdict (${res.status}): ${text}`);
  }
  revalidatePath("/versus/judge");
  redirect(
    `/versus/judge?name=${encodeURIComponent(body.name)}&criterion=${encodeURIComponent(body.criterion)}`,
  );
}

export default async function VersusJudgePage({
  searchParams,
}: {
  searchParams: Promise<{ name?: string; criterion?: string }>;
}) {
  const sp = await searchParams;
  const name = sp.name?.trim();
  if (!name) redirect("/versus");

  const data = await getNextPair(name, sp.criterion ?? "");
  if (!data) {
    return (
      <div className="versus-shell">
        <VersusHeader breadcrumb={`judge: ${name}`} />
        <main className="versus-main">
          <div className="versus-card">
            <em className="versus-muted">Failed to load. Make sure the API is running.</em>
          </div>
        </main>
      </div>
    );
  }

  const { pair, progress } = data;

  if (!pair) {
    return (
      <div className="versus-shell">
        <VersusHeader breadcrumb={`judge: ${name}`} />
        <main className="versus-main">
          <h1 style={{ fontWeight: 300, fontSize: 26, margin: "0 0 8px" }}>
            All done for <code>{progress.criterion}</code> 🎉
          </h1>
          <p className="versus-muted">Progress across all criteria:</p>
          <div className="versus-card" style={{ maxWidth: 500 }}>
            {progress.per_criterion.map((s) => (
              <div
                key={s.criterion}
                style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 6 }}
              >
                <span className="versus-pill">{s.criterion}</span>
                <span style={{ fontVariantNumeric: "tabular-nums" }} className="versus-muted">
                  {s.done} / {s.total}
                </span>
                {s.done < s.total && (
                  <Link
                    href={`/versus/judge?name=${encodeURIComponent(name)}&criterion=${encodeURIComponent(s.criterion)}`}
                    style={{ marginLeft: "auto" }}
                  >
                    continue
                  </Link>
                )}
              </div>
            ))}
          </div>
          <p style={{ marginTop: 20 }}>
            <Link href="/versus">back to start</Link>
          </p>
        </main>
      </div>
    );
  }

  const otherCriteria = progress.criteria.filter((c) => c !== pair.criterion);

  return (
    <div className="versus-shell">
      <VersusHeader
        breadcrumb={
          <span>
            judge: <strong>{name}</strong>
            {" · "}criterion: <span className="versus-pill">{pair.criterion}</span>
          </span>
        }
        right={
          <>
            <span style={{ fontVariantNumeric: "tabular-nums" }} className="versus-muted">
              {pair.done_count} / {pair.total}
            </span>
            <span className="versus-muted">switch:</span>
            {otherCriteria.map((c) => (
              <Link
                key={c}
                href={`/versus/judge?name=${encodeURIComponent(name)}&criterion=${encodeURIComponent(c)}`}
              >
                {c}
              </Link>
            ))}
          </>
        }
      />
      <main className="versus-main">
        <h1 style={{ fontWeight: 300, fontSize: 26, margin: "0 0 8px" }}>{pair.title}</h1>
        <p className="versus-muted">{pair.criterion_desc}</p>

        <details>
          <summary>Essay opening (click to reveal)</summary>
          <div className="versus-card judging-prose" style={{ marginTop: 8 }}>
            {pair.prefix_text}
          </div>
        </details>

        <h2 style={{ fontSize: 16, fontWeight: 500, margin: "24px 0 8px" }}>
          Two continuations — which is better?
        </h2>

        <form action={submitVerdict}>
          <input type="hidden" name="name" value={name} />
          <input type="hidden" name="criterion" value={pair.criterion} />
          <input type="hidden" name="essay_id" value={pair.essay_id} />
          <input type="hidden" name="prefix_hash" value={pair.prefix_hash} />
          <input type="hidden" name="a" value={pair.a} />
          <input type="hidden" name="b" value={pair.b} />
          <input type="hidden" name="first_source" value={pair.first_source} />
          <input type="hidden" name="second_source" value={pair.second_source} />

          <div className="two-col">
            <div className="versus-card">
              <h2 style={{ fontSize: 16, fontWeight: 500, marginTop: 0 }}>Continuation A</h2>
              <div className="judging-prose">{pair.first_text}</div>
            </div>
            <div className="versus-card">
              <h2 style={{ fontSize: 16, fontWeight: 500, marginTop: 0 }}>Continuation B</h2>
              <div className="judging-prose">{pair.second_text}</div>
            </div>
          </div>

          <div style={{ marginTop: 20 }}>
            <textarea
              name="note"
              rows={2}
              placeholder="Optional note on your reasoning"
              className="versus-textarea"
              style={{ width: "100%" }}
            />
          </div>

          <div style={{ display: "flex", gap: 12, marginTop: 16, alignItems: "center" }}>
            <button type="submit" name="verdict" value="A" className="versus-button primary">
              A is better <kbd style={{ opacity: 0.6, fontSize: 11 }}>1</kbd>
            </button>
            <button type="submit" name="verdict" value="B" className="versus-button primary">
              B is better <kbd style={{ opacity: 0.6, fontSize: 11 }}>2</kbd>
            </button>
            <button type="submit" name="verdict" value="tie" className="versus-button">
              Tie <kbd style={{ opacity: 0.6, fontSize: 11 }}>3</kbd>
            </button>
            <span className="versus-muted" style={{ fontSize: 11, marginLeft: 8 }}>
              shortcuts: 1 / 2 / 3 (or A / B / T, or ← / →)
            </span>
          </div>
        </form>
        <JudgeKeyboardShortcuts />
      </main>
    </div>
  );
}
