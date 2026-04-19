"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS = [
  { href: "/traces", label: "traces" },
  { href: "/evaluations", label: "evaluations" },
  { href: "/ab-evals", label: "ab-evals" },
] as const;

export default function OperatorLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="op-shell">
      <nav className="op-nav">
        <Link href="/" className="op-nav-wordmark">
          rumil
        </Link>
        <div className="op-nav-links">
          {NAV_ITEMS.map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className={`op-nav-link ${pathname.startsWith(href) ? "active" : ""}`}
            >
              {label}
            </Link>
          ))}
        </div>
        <div className="op-nav-badge">operator</div>
      </nav>
      <main className="op-content">{children}</main>
    </div>
  );
}
