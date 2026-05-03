import Link from "next/link";

export function VersusHeader({
  breadcrumb,
  right,
}: {
  breadcrumb: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <header className="versus-header">
      <Link href="/versus" style={{ color: "inherit" }}>
        <strong>versus</strong>
      </Link>
      <span className="muted">/</span>
      <span>{breadcrumb}</span>
      <div className="grow"></div>
      {right}
      <Link href="/versus/recent">recent</Link>
      <Link href="/versus/inspect">inspect</Link>
      <Link href="/versus/results">results</Link>
      <Link href="/">rumil</Link>
    </header>
  );
}
