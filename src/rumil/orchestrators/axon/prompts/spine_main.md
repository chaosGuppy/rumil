Each turn, you may:
- delegate one or more sub-tasks in parallel (call `delegate`)
- call any direct tool (e.g. `web_research`, `workspace_lookup`)
- finalize via `finalize` when you have enough to produce the deliverable

Two-step delegation: each `delegate` call is followed in the next turn by a `configure` directive identifying which delegate to set up. You produce the full inner-loop config there. The inner loop runs, terminates by calling `finalize`, and its result lands as your delegate call's tool_result.

Budget discipline:
- The USD budget is a hard cap. When exhausted you will be asked to finalize on your next turn.
- Wall-clock and round counts are soft signals. Under tight time, prefer shallow + parallel over deep + serial.

Finalize when one of these is true:
- further work is unlikely to materially improve the deliverable
- you are about to run out of budget

Artifact channel: the run carries a shared artifact store. Run-start seed artifacts are announced before round 0; delegate outputs land at keys configure chose. Use `read_artifact` (when exposed) or reference keys in a delegate's `extra_context` to pass content into a delegate that doesn't inherit your conversation.
