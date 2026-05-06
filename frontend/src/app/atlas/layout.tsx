import type { Metadata } from "next";
import Link from "next/link";
import { SearchBar } from "./_components/SearchBar";
import "./atlas.css";

export const metadata: Metadata = {
  title: {
    template: "%s · atlas",
    default: "atlas",
  },
};

const NAV_LINKS: Array<{ href: string; label: string }> = [
  { href: "/atlas", label: "index" },
  { href: "/atlas/workflows", label: "workflows" },
  { href: "/atlas/calls", label: "calls" },
  { href: "/atlas/dispatches", label: "dispatches" },
  { href: "/atlas/moves", label: "moves" },
  { href: "/atlas/pages", label: "pages" },
  { href: "/atlas/prompts", label: "prompts" },
];

export default function AtlasLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="atlas-shell">
      <header className="atlas-topnav">
        <Link href="/atlas" className="atlas-brand">
          <span className="atlas-brand-mark">/</span>
          <span className="atlas-brand-name">atlas</span>
        </Link>
        <nav className="atlas-nav">
          {NAV_LINKS.slice(1).map((l) => (
            <Link key={l.href} href={l.href} className="atlas-nav-link">
              {l.label}
            </Link>
          ))}
          <Link href="/atlas/feed" className="atlas-nav-link">
            recent work
          </Link>
          <Link href="/atlas/gaps" className="atlas-nav-link">
            gaps
          </Link>
          <Link href="/atlas/novelty" className="atlas-nav-link">
            novelty
          </Link>
        </nav>
        <SearchBar />
        <Link href="/" className="atlas-back">
          <span aria-hidden>←</span> rumil
        </Link>
      </header>
      <main className="atlas-main">{children}</main>
    </div>
  );
}
