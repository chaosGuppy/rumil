import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Move Next.js's dev-only indicator (the unlabeled "N" glyph that sits
  // bottom-left by default) out of the main content area. Users were
  // mistaking it for an app affordance (ux-review-wave7 #2). We can't
  // attach a `title` to the built-in button, and the indicator is useful
  // for catching build errors, so relocating it is the least-intrusive
  // fix. bottom-right keeps it out of the pane area; the chat toggle
  // strip occupies its own 36px column so there's no overlap.
  devIndicators: {
    position: "bottom-right",
  },
};

export default nextConfig;
