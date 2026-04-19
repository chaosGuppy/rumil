import { defineConfig } from "@hey-api/openapi-ts";

// Types-only codegen: parma keeps its hand-written `lib/api.ts` fetch
// layer for domain-specific error ergonomics (liftFastApiError), but
// re-exports generated types from `lib/types.ts` so every backend model
// and enum stays in sync with rumil's OpenAPI spec.
export default defineConfig({
  input: "../frontend/openapi.json",
  output: {
    path: "src/api",
  },
  plugins: ["@hey-api/typescript"],
});
