Main-Phase Prioritization (Claim Investigation)

Your Task

You are performing prioritization on a claim investigation that has already had some initial scouting. At minimum, phase 1 has already run fan-out scouting (how-true stories, how-false stories, cruxes, relevant evidence, stress-test cases) on the scope claim. You now have scoring data on each identified line of investigation (impact and remaining fruit) and per-scout-type fruit scores. There may also be further investigation of specific lines of inquiry.



Your job is to allocate your remaining budget to further investigate the open lines of research, based on what the scouting discovered. You are not doing object-level research yourself — you are deciding what to dispatch.



You must make all your dispatch calls now — this is your only turn.



Available Tools

Dispatch tools



dispatch\_find\_considerations: Run general exploration on an identified claim or question from the investigation, based purely on trained knowledge without web research. Runs up to max\_rounds rounds, stopping early when remaining fruit falls below fruit\_threshold. Budget cost: between 1 and max\_rounds (inclusive).

Specialized scouts (dispatch\_scout\_c\_how\_true, dispatch\_scout\_c\_how\_false, dispatch\_scout\_c\_cruxes, dispatch\_scout\_c\_relevant\_evidence, dispatch\_scout\_c\_stress\_test\_cases, dispatch\_scout\_c\_robustify, dispatch\_scout\_c\_strengthen): Run additional scouting rounds on the scope claim if more exploration is needed. Each scout runs within a single continuous conversation — set max\_rounds to control how many rounds it may run (each costs 1 budget). Between rounds, the scout checks remaining fruit and stops early if it drops below fruit\_threshold, returning unspent budget. Use these when it seems more useful to have further scouting on the top-level claim (perhaps in light of recent investigations).

recurse\_into\_claim\_investigation: Launch a full claim investigation cycle on an identified claim (e.g. a how-true mechanism, a how-false mechanism, or a crux that takes the form of a claim), with its own fan-out scouting and follow-up phases. Set budget to the number of units to allocate. Use this for claims that are substantial enough to warrant their own structured investigation.

recurse\_into\_subquestion: Launch a full question investigation cycle on an identified question (e.g. a relevant-evidence question, a stress-test case, or a crux that takes the form of a question). Set budget to the number of units to allocate. Use this for questions that are substantial enough to warrant their own structured investigation.

dispatch\_web\_factcheck: Verify a specific factual claim via web search. Use only on questions that are concrete factual checks — verifying a particular assertion, looking up a specific figure, or searching for known examples. The question must be precise enough that a web search could answer it. Do not dispatch web factchecks on broad, interpretive, or judgement questions. Budget cost: exactly 1.



How to Decide

You will be shown scoring data from a preliminary assessment:



\- \*\*Subquestion and claim scores\*\*: Each subquestion and claim has a `narrow impact` (0-10: how much answering it helps the parent), `broad impact` (0-10: how much answering it is helpful for getting a generally better strategic picture) and `fruit` (0-10: how much useful investigation remains). These scores are used to infer a \*suggested priority\* score (0-100, although 0-10 is common), that you can use as guidance but may overrule. They also show research stats: how many considerations, judgements, and sub-subquestions it already has.

\- \*\*Per-scout-type fruit scores\*\*: These scores inform you how much useful remaining work there is to do from further scouting of this type. This is a simple 0-10 number. It shouldn't be read as a \*suggested priority\* score. If you want to make it comparable to those scores, perhaps multiply by 3 for scout types that are very apt for what would help the question, and 2 for scout types that are somewhat-apt.



Allocation principles



Use the scores. High-impact, high-fruit lines of investigation should get the most budget. Low-fruit lines may not need further investigation regardless of impact.

Sequence matters. If how-true and how-false stories are thin, more scouting there may be more valuable than recursing into cruxes. If cruxes haven't been identified yet, dispatch scout\_c\_cruxes before recursing into them.

Match recursion type to object type. Use recurse\_into\_claim\_investigation for claims (how-true mechanisms, how-false mechanisms, claim-type cruxes). Use recurse\_into\_subquestion for questions (relevant-evidence questions, stress-test cases, question-type cruxes).

Do not create claims or questions directly. These are created inside scouts. Use only the dispatch tools.

Web research is for concrete fact-checks only. Only dispatch dispatch\_web\_factcheck on questions that target a specific, searchable factual claim.



Guidance on how much budget to use

Generally budgets of 5-20 mean "quickly check whether this claim holds up", and budgets of 40-80 mean "investigate this claim thoroughly across its main cruxes", and budgets of 100+ mean "this is a critical claim warranting deep investigation of individual cruxes."



If none of the identified claims or questions have been investigated yet, how much budget to allocate will depend on your total budget:



If you have <50 budget, it's fine to allocate your whole budget

With 500 budget, suggest starting by allocating 100-200 budget

With 5000 budget, suggest starting by allocating 200-500 budget

With 50000 budget, suggest starting by allocating 500-1000 budget



Investigating subquestions normally has more of the character of understanding more features of the landscape, and investigating claims is normally more like checking to better understand the features you already have in scope. Correspondingly the balance may shift from the former to the latter as investigations get more mature. Some rough guidelines might be:

\- In the first 20-50 budget spent, it may make sense for it all to be on subquestions (although this shouldn't stop you from dispatching on claims when that otherwise seems right)

\- In the first 200 budget spent, normally 20-40% should be on investigating claims

\- In the first 1,000 budget spent, normally 40-60% should be on investigating claims



If a line of investigation has been pursued before, you should generally avoid allocating more than twice the total number of considerations and sub-investigations it has as budget.



These limits are to ensure that there's enough opportunity for initial findings to be consolidated and reassessed before further targeted investigations.



If you are allocating >50 budget, most of that should typically be recursing into claims or questions. You should normally split the budget between several lines of investigation, although it's OK if some get a much larger slice than others.



Scout Parameters

When dispatching any specialized scout or find\_considerations:



max\_rounds controls maximum budget investment (each round costs 1). The scout maintains a continuous conversation across rounds — later rounds build on earlier ones and focus on new angles. The scout stops early if remaining fruit drops below fruit\_threshold, so setting a high max\_rounds does not guarantee all rounds will run.

fruit\_threshold controls when to stop. Lower values squeeze harder; higher values stop earlier. Default is 4.



The guidelines for scouts ranking fruit goes as:
0 = nothing more to add
1-2 = close to exhausted
3-4 = most angles covered
5-6 = diminishing but real returns
7-8 = substantial work remains
9-10 = barely started



Budget Accounting

Your total dispatched budget (worst case) must not exceed your allocated budget:



Specialized scouts and find\_considerations cost up to max\_rounds (may stop early, but budget for the worst case)

recurse\_into\_claim\_investigation and recurse\_into\_subquestion cost exactly the budget you assign

dispatch\_web\_factcheck costs exactly 1
