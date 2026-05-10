The transcript above is approaching the context limit and will be replaced by the summary you write here. Future turns of this same run will continue from your summary as if it were the live thread, so write for your future self mid-task — not for an outside reader.

Preserve, in roughly this priority order:

1. **The scope question and operating assumptions.** What are you trying to answer / produce? Any caller-supplied constraints (blind judging rules, output schema, no-attribution rules, etc.) — these are load-bearing and never derivable later.
2. **Durable findings from delegates.** The actual claims, sources, contradictions, and evidence the inner agents have surfaced. Quote verbatim where precision matters (specific numbers, names, phrasings, source URLs). Drop the delegate mechanics — which delegate ran, retry chatter, parameter overrides — unless they bear on what to do next.
3. **The plan and current state.** What you have already concluded vs. what is still unresolved. What you intended to do next before the compaction fired. Open questions you have not yet answered.
4. **Drafted deliverable content.** If you have started shaping the finalize answer (an outline, a draft passage, a verdict-with-reasoning), preserve it verbatim. The deliverable is the point of the run.

You can drop:

- Raw tool-result framing (cost lines, throttling notices, budget-status reminders).
- Speculative reasoning that did not pan out, unless you might revisit it.
- Routine planning prose ("I will now delegate X then Y") once X and Y have run.

Format your summary as readable prose with section headings. Wrap the whole thing in a single `<summary>...</summary>` block — the API expects that wrapper. The summary will be the *only* context (plus the system prompt) the next iteration sees, so make sure everything load-bearing is inside the block.
