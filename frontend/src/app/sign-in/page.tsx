"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Fraunces, JetBrains_Mono } from "next/font/google";

import { InviteShell } from "@/components/invite/invite-shell";
import { createClient } from "@/lib/supabase/client";
import styles from "./sign-in.module.css";

const fraunces = Fraunces({
  subsets: ["latin"],
  weight: ["300", "400", "500"],
  style: ["normal", "italic"],
  variable: "--font-fraunces",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["300", "400", "500"],
  variable: "--font-mono-invite",
  display: "swap",
});

function SignInForm() {
  const [status, setStatus] = useState<"idle" | "pending">("idle");
  const [error, setError] = useState<string | null>(null);
  const searchParams = useSearchParams();

  async function signIn() {
    if (status === "pending") return;
    setStatus("pending");
    setError(null);

    const supabase = createClient();
    const next = searchParams.get("next") ?? "/";
    const redirectTo = `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`;

    const { error: err } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo },
    });

    if (err) {
      setError(err.message);
      setStatus("idle");
    }
  }

  return (
    <div className={`${fraunces.variable} ${mono.variable}`}>
      <InviteShell title="Sign in to continue.">
        <div
          className={`${styles.block} ${status === "pending" ? styles.dissolving : ""}`}
        >
          <button
            type="button"
            onClick={signIn}
            disabled={status === "pending"}
            className={styles.stamp}
          >
            <span className={styles.stampMark} aria-hidden>
              G
            </span>
            <span className={styles.stampLabel}>
              {status === "pending" ? "Signing in…" : "Continue with Google"}
            </span>
            <span className={styles.stampArrow} aria-hidden />
          </button>
          {error ? <p className={styles.error}>{error}</p> : null}
        </div>
      </InviteShell>
    </div>
  );
}

export default function SignInPage() {
  return (
    <Suspense fallback={null}>
      <SignInForm />
    </Suspense>
  );
}
