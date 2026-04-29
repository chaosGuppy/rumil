import type { ReactNode } from "react";
import styles from "./invite-shell.module.css";

type InviteShellProps = {
  label?: string;
  title?: string;
  children: ReactNode;
};

export function InviteShell({ label, title, children }: InviteShellProps) {
  return (
    <main className={styles.root}>
      <div className={styles.frame}>
        <header className={styles.wordmark}>
          <span className={styles.wordmarkName}>rumil</span>
        </header>

        <section className={styles.stage}>
          <div className={styles.card}>
            {label ? <span className={styles.cardLabel}>{label}</span> : null}
            {title ? <h1 className={styles.cardTitle}>{title}</h1> : null}
            {children}
          </div>
        </section>
      </div>
    </main>
  );
}
