import Link from "next/link";
import type { ReactNode } from "react";

export type Crumb = { label: ReactNode; href?: string };

export function Crumbs({ items }: { items: Crumb[] }) {
  return (
    <div className="atlas-eyebrow">
      {items.map((c, i) => (
        <span key={i}>
          {i > 0 && <span className="atlas-eyebrow-sep">/</span>}{" "}
          {c.href ? <Link href={c.href}>{c.label}</Link> : <span>{c.label}</span>}{" "}
        </span>
      ))}
    </div>
  );
}
