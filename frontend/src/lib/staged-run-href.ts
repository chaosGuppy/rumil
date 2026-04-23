export function withStagedRun(
  path: string,
  stagedRunId: string | null | undefined,
): string {
  return stagedRunId ? `${path}?staged_run_id=${stagedRunId}` : path;
}
