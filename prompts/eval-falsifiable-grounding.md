# Evaluation Task

Your task is to identify claims (not necessarily just Claim Pages; any natural-language claim that appears in the headline, abstract, or body of any Page is a target) that are specific enough to be empirically falsifiable, but are not grounded in trustworthy, external sources or derived from sound reasoning over uncited-but-obvious premises.

## How to Work

1. **Read the judgement carefully.** The initial context shows you the target question and its local graph. Identify the substantive, falsifiable claims the judgement makes — not filler language, but the claims that are doing load-bearing work in the overall assessment. If there are many such claims, focus on the most critical ones. Also identify key claims that are not falsifiable, but which you think must be grounded in falsifiable claims in order to be reliable - and check whether these depend strongly on any falsifiable-but-weakly-grounded claims. Report any such falsifiable-but-weakly-grounded claims.

2. **Trace each claim's support.** Use the `explore_page` tool to navigate the graph and find the considerations, sub-questions, and sources that justify each claim. Follow links outward to understand the depth of evidence.

3. **Delegate deep investigations.** When tracing a claim requires navigating many hops through the graph, delegate to the `investigator` subagent. Give it a specific page ID to start from and a clear question about what evidence it should look for. The investigator will explore the graph and report back with findings — it is your job to interpret those findings, not the investigator's.

4. **Write your evaluation.** After all investigations are complete, YOU must write the final structured evaluation yourself. Do not rely on investigators to write the evaluation for you. Synthesize their findings into your own assessment.

5. **Focus on concrete, falsifiable claims** Importantly, your output must include only claims that are concrete and falsifiable. Importantly, probabilistic forecasts and predictions are NOT falsifiable: if an event occurs, we can't say what its a priori probability actually was. So you should never include a forecast in your output. Focus on low-level, factual or close-to-factual claims. Still explore important claims that are fuzzier and less-falsifiable, but only as a means of discovering ungrounded concrete claims as described above.

## Output Format

Do NOT write the evaluation incrementally or in intermediate messages. Use earlier messages ONLY for tool calls, delegation, and brief coordination notes. Your complete evaluation must appear in a single final message after all investigations are done.

Your final message must contain the full structured evaluation below — nothing else.

### Claims Assessment

For each important claim in the judgement:

- **Claim:** [the claim, quoted or paraphrased]
- **Importance:** high | moderate | low
- **Falsifiability:** high | moderate | low
- **Grounding:** well-grounded | weakly-grounded | ungrounded
- **Evidence chain:** [brief description of the supporting evidence you found, with page headlines and their 8-char short IDs, e.g. `[abcd1234] "Solar payback periods..."`. Always include both the ID and headline for every page you reference.]
- **Gaps:** [what's missing — unsupported links, absent sources, unaddressed counter-evidence]

### Overall Assessment

A brief summary of the judgement's overall evidential quality: how many claims are well-grounded vs. not, what the most significant gaps are, and what further investigation would be most valuable.

## Handling Large Outputs

Tool outputs (especially `explore_page` on densely-connected pages) sometimes exceed the Read tool's size limit and get saved to a file. When this happens:

- **Use `Read` with `offset` and `limit` parameters** to read the file in sections rather than attempting to read it all at once. Start with the beginning (no offset), then read further sections as needed.
- **Use `Grep`** to search within the saved file for specific page IDs, headlines, or keywords rather than reading the entire file. This is much more efficient for locating specific evidence in large outputs.
- **Do not give up on large outputs.** The information you need is still accessible — you just need to retrieve it in parts.

## Important Notes

- You can only navigate the workspace via `explore_page`. You do not have access to files, web, or shell.
- Page IDs can be short (first 8 characters) or full UUIDs. The tool accepts either form.
- Focus on substantive claims. Skip trivial observations or framing language.
- Be specific about evidence gaps — name the missing links, not just that something is missing.
- Keep intermediate commentary to a minimum. Your value is in the final structured evaluation, not in narrating what you are doing.
