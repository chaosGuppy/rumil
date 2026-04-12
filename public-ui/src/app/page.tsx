"use client";

import { Suspense } from "react";
import { MOCK_WORLDVIEW } from "@/lib/mock-data";
import { StackedPanes } from "@/components/StackedPanes";

function WorldviewContent() {
  return <StackedPanes worldview={MOCK_WORLDVIEW} />;
}

export default function Page() {
  return (
    <Suspense
      fallback={
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "12px",
            color: "var(--fg-dim)",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}
        >
          Loading worldview...
        </div>
      }
    >
      <WorldviewContent />
    </Suspense>
  );
}
