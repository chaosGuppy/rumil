const ADMIN_PATH_PREFIXES = ["/traces", "/ab-evals", "/versus"];

const ADMIN_PATH_SUFFIXES = ["/stats"];

export function isAdminPath(path: string): boolean {
  if (ADMIN_PATH_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`))) {
    return true;
  }
  return ADMIN_PATH_SUFFIXES.some(
    (s) => path === s || path.endsWith(s) || path.endsWith(`${s}/`),
  );
}
