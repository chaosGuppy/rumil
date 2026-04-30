## the task

you're searching the workspace for existing questions that should
be linked as sub-questions of a given **scope question**. the
workspace is a graph of questions, claims, and judgements. you'll
be given the scope question, the sub-questions it already has, and
three-hop subgraphs of several promising top-level questions to
seed your search.

## relevance bar (read carefully)

the bar is high. a candidate question only passes if **all** of the
following hold:

1. its answer would have a **strong and fairly straightforward
   influence** on the answer to the scope question — not a tenuous,
   speculative, or many-step-removed connection.
2. you can articulate a concrete, direct path by which the answer
   would influence the scope's answer.
3. the influence persists **even after conditioning on good answers
   to the sub-questions already linked to the scope** AND **good
   answers to the other sub-questions you're proposing in this same
   run**. each candidate must add independent direct influence on
   top of the others.
4. the influence is **direct**: if the only way the candidate
   matters is via its effect on another sub-question (already linked
   or newly proposed), it does NOT pass the bar.

**cap: return at most 5 candidates.** include only the most
relevant questions — those whose answers would most strongly and
directly influence the scope's answer. each must independently
clear the bar above. if fewer than 5 meet the bar, return fewer.
returning 0 is perfectly acceptable if no candidates are strong
enough.

## a few moves

before deciding on candidates, name the cached take. when you scan
the seed subgraphs, what's the obvious "this looks relevant" pattern
you'd reach for? write it down. now ask: would the candidate's
answer *actually* influence the scope's answer, or am i pattern-
matching on topical similarity? topical relevance is not the same
as load-bearing influence.

attack each candidate by asking: if i hold the existing
sub-questions' answers fixed, does this candidate still add
independent influence? if its impact would be fully mediated by
something already on the list, cut it.

## prefer questions with fleshed-out answers

every question in the rendered subgraphs is annotated with either
`(Answered at robustness X/5)` or `(Unanswered)`. the robustness
score (1-5) reflects how resilient the current answer is — higher
means the answer has survived more scrutiny and is less likely to
change. a question's visible sub-question count in the tree is
another signal of how fleshed-out it is.

**all else being equal, prefer questions that are well-fleshed-out
over ones that aren't.** concretely:

- a question with a robust answer (e.g. 4/5 or 5/5) **and** many
  sub-questions is highly valuable to link — its influence on the
  scope is concrete and already supported by evidence.
- a question with a weak answer (1/5 or 2/5) or no answer at all,
  **and** few or no sub-questions, is much less valuable — linking
  it mostly just adds a placeholder.
- when you're near the 5-candidate cap and must choose between
  otherwise-comparable candidates, break ties by preferring the
  more fleshed-out one.

this is a tiebreaker, not a hard filter: a sharply relevant
unanswered question can still beat a weakly-relevant
robustly-answered one. but between two candidates of similar
relevance, the fleshed-out one wins.

## picking the right level of the hierarchy

when you link a question as a sub-question of the scope, **all of
its descendents come along for the ride** — they're implicitly part
of the scope's investigation too. so for each promising area of the
graph, ask yourself: "of this question's direct children, what
fraction would i want linked to the scope?" use this rule of thumb:

- if **>= 50%** of the children would pass the bar, link the
  **parent** instead — it's a cleaner unit and brings the rest
  along.
- if **< 50%** of the children would pass the bar, link those
  specific children **individually** rather than dragging in the
  parent and all its other irrelevant children.

apply the same logic recursively when deciding between a child and
its grandchildren. note: it's fine to link a parent and one of its
descendents together if the descendent has **independent direct
influence** on the scope that isn't mediated by the parent — the
"all influence must be direct" rule above is what governs this.

## how to explore

you have up to **{max_rounds}** rounds of tool use. in each round
you may call `explore_question_subgraph` with any question short ID
(8-char prefix) to see a 3-hop subgraph rooted at that question
(children, grandchildren, great-grandchildren, headlines only). use
this to drill into promising branches of the seed subgraphs you
were given, or into questions you've already discovered.

**explore broadly and use your full budget.** default to issuing
**around 5 `explore_question_subgraph` calls in parallel per
round**, and keep exploring until your rounds are exhausted — don't
stop early just because you've found a few plausible candidates.
only deviate if you have a concrete reason (you've genuinely run
out of unexplored promising branches, or the remaining branches
are clearly irrelevant). finding candidates you ultimately reject
is a normal and expected part of a thorough search; an under-used
exploration budget is a worse outcome than finding nothing.

when you've finished exploring, submit your final answer by
calling the `submit_linked_subquestions` tool **exactly once**, as
your very last action. pass a list of `question_ids` (8-char short
ids or full UUIDs). if you find no candidates that pass the bar,
call the tool with an empty list. don't call any other tool after
submitting.
