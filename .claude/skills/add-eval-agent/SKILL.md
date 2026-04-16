---
name: add-eval-agent
description: Add a new evaluation agent that assesses a specific quality dimension of research runs. Use this skill when the user describes a type of issue, failure mode, or quality dimension they want an evaluation agent to check for — e.g. "I want to evaluate whether runs are citing sources properly" or "add an eval agent for calibration quality". Also trigger when the user wants to modify the set of evaluation dimensions or asks how to add a new eval agent.
allowed-tools: Read,Write,Edit,Bash,Glob,Grep
argument-hint: "<description of the quality dimension to evaluate>"
---

# Adding a New Evaluation Agent

The evaluation system runs multiple independent agents against staged research runs, each assessing a different quality dimension. Adding a new agent requires two files: a prompt and a spec entry.

## Step 1: Understand the dimension

Read the user's description of what they want evaluated. Clarify if needed:
- What specific issues or qualities should the agent look for?
- Are there concrete examples of good vs. bad output?
- Does the agent need any special tools (e.g. `WebSearch` for fact-checking)?

## Step 2: Choose a name

Pick a short, snake_case name for the agent (e.g. `calibration`, `source_quality`, `structural_coherence`). This name is used in the `--eval-agents` CLI filter, so it should be intuitive.

## Step 3: Read existing agents for reference

Read these two files to understand the conventions:
- `src/rumil/run_eval/agents.py` — the `EVAL_AGENTS` list and `EvalAgentSpec` dataclass
- One existing prompt for reference — `prompts/run-eval-grounding.md` is a good example since it uses an extra tool (WebSearch)

## Step 4: Write the prompt file

Create `prompts/run-eval-<name>.md`. Follow this structure exactly:

```
# Run Evaluation: <Display Name>

You are evaluating a research run for **<brief description of the dimension>**.

## What you are evaluating

You are looking at a research workspace where a run has added new pages and links. Items marked `[ADDED BY THIS RUN]` were created by the run being evaluated. Focus your evaluation on these items -- the rest of the workspace is pre-existing context.

## Your task

<Describe what specifically to assess. Break into numbered sub-dimensions
if there are distinct aspects. Be concrete about what "good" and "bad"
look like for each.>

## How to work

1. Use `explore_subgraph` to navigate the workspace graph, starting from the root question. Use `load_page` to read the full content of individual pages
2. <Any dimension-specific workflow steps, e.g. "Use WebSearch to verify claims">
3. Be specific -- cite page IDs and give concrete examples

## Output format

Produce a structured evaluation report with:

- **Summary**: 2-3 sentence overview
- **Strengths**: What this run did well on this dimension
- **Weaknesses**: Specific examples of problems, each with a page ID
- <Any dimension-specific sections>
- **Overall assessment**: A paragraph synthesizing your evaluation
```

Key points for the prompt:
- The "What you are evaluating" section is boilerplate — keep it as-is so agents behave consistently.
- The "Your task" section is where the dimension's specificity lives. Be concrete about what to look for.
- The "How to work" section should mention `explore_subgraph` and `load_page` — these are the tools every eval agent gets. Add tool-specific steps if the agent has `extra_tools`.
- The "Output format" section should request a structured report. The format can vary by dimension but should always include Summary, Strengths, Weaknesses, and Overall assessment.

## Step 5: Register the agent

Edit `src/rumil/run_eval/agents.py`. Add a new `EvalAgentSpec` entry to the `EVAL_AGENTS` list:

```python
EvalAgentSpec(
    name="<snake_case_name>",
    display_name="<Human Readable Name>",
    prompt_file="run-eval-<name>.md",
    extra_tools=[...],  # omit if no extra tools needed
),
```

The `extra_tools` field lists Claude Code tool names (e.g. `["WebSearch"]`) that the agent needs beyond the default `explore_subgraph` and `load_page`. Most agents don't need any.

## Step 6: Verify

1. Run `uv run pyright` — should pass with 0 errors
2. Check that the prompt file exists at the path specified in `prompt_file`
3. Optionally, do a quick smoke test:
   ```bash
   uv run python main.py "test question" --workspace eval-agent-test --smoke-test --staged --run-id-file /tmp/test-run-id
   uv run python main.py --run-eval $(cat /tmp/test-run-id) --eval-agents <name> --workspace eval-agent-test
   ```

## Things you don't need to do

The infrastructure handles everything else automatically:
- The general_quality agent's exclusion list updates via `{other_dimensions}` — no manual edit needed
- Report formatting, DB storage, and the `--eval-agents` CLI filter all work over the `EVAL_AGENTS` list
- Both `--run-eval` and `--ab-eval` pick up the new agent
