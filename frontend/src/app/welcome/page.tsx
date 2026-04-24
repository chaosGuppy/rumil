"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Fraunces, JetBrains_Mono } from "next/font/google";

import { InviteShell } from "@/components/invite/invite-shell";
import styles from "./welcome.module.css";

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

function WelcomeForm() {
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<"idle" | "pending" | "wrong" | "ok">(
    "idle",
  );
  const [shaking, setShaking] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!password || status === "pending") return;
    setStatus("pending");

    const res = await fetch("/welcome/verify", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    });

    if (res.ok) {
      setStatus("ok");
      const next = searchParams.get("next");
      const target = next && next.startsWith("/") ? next : "/sign-in";
      setTimeout(() => router.replace(target), 520);
      return;
    }

    setStatus("wrong");
    setShaking(true);
    setPassword("");
    setTimeout(() => {
      setShaking(false);
      inputRef.current?.focus();
    }, 450);
  }

  const cardClass = [
    shaking ? styles.shake : "",
    status === "ok" ? styles.dissolving : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={`${fraunces.variable} ${mono.variable}`}>
      <InviteShell label="Invitation" title="Enter your passphrase.">
        <form onSubmit={submit} className={`${styles.form} ${cardClass}`}>
          <div className={styles.fieldRow}>
            <input
              ref={inputRef}
              id="passphrase"
              name="passphrase"
              type="password"
              autoComplete="off"
              spellCheck={false}
              className={styles.input}
              placeholder="Token"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={status === "pending" || status === "ok"}
              aria-label="Passphrase"
            />
            <button
              type="submit"
              className={styles.submit}
              disabled={!password || status === "pending" || status === "ok"}
              aria-label="Submit"
            >
              <span className={styles.submitLabel}>
                {status === "pending"
                  ? "Checking"
                  : status === "ok"
                    ? "Admitted"
                    : "Enter"}
              </span>
              <svg
                className={styles.arrow}
                width="36"
                height="9"
                viewBox="0 0 36 9"
                fill="none"
                aria-hidden
              >
                <path
                  d="M0 4.5 H33.5 M30 1 L33.5 4.5 L30 8"
                  stroke="currentColor"
                  strokeWidth="1"
                  strokeLinecap="square"
                  strokeLinejoin="miter"
                />
              </svg>
            </button>
          </div>
          <p
            className={`${styles.error} ${
              status === "wrong" ? styles.errorVisible : ""
            }`}
            role="status"
            aria-live="polite"
          >
            Not recognised.
          </p>
        </form>
      </InviteShell>
    </div>
  );
}

export default function WelcomePage() {
  return (
    <Suspense fallback={null}>
      <WelcomeForm />
    </Suspense>
  );
}
