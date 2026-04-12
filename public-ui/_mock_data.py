"""Python-native mock data for seeding the SQLite DB.

Mirrors the TypeScript mock in src/lib/mock-data.ts but as plain dicts
so the server can import it without parsing TS.
"""

MOCK_HEADLINE = (
    "What are the most important bottlenecks to making AI development go well?"
)

MOCK_SUMMARY = (
    "The research identifies governance capacity, alignment technique maturity, "
    "and organizational incentive structures as the primary bottlenecks. There is "
    "moderate confidence that technical alignment progress is outpacing governance "
    "readiness, creating a growing gap. Key uncertainties remain around whether "
    "current interpretability advances will generalize to frontier systems and "
    "whether international coordination mechanisms can scale fast enough."
)

MOCK_NODES: list[dict] = [
    {
        "node_type": "claim",
        "headline": "Governance capacity is lagging behind technical capabilities",
        "content": "Multiple lines of evidence suggest that AI governance institutions are developing significantly slower than AI capabilities. Regulatory frameworks remain fragmented across jurisdictions, and technical standards bodies lack the expertise to evaluate frontier systems.",
        "credence": 7,
        "robustness": 4,
        "source_page_ids": ["f8a1b2c3", "d4e5f6a7"],
        "children": [
            {
                "node_type": "evidence",
                "headline": "Major jurisdictions have incompatible regulatory frameworks",
                "content": "The EU AI Act, US executive orders, and China's AI regulations take fundamentally different approaches to risk classification and enforcement.",
                "credence": 8,
                "robustness": 4,
                "source_page_ids": ["b1c2d3e4"],
                "children": [
                    {
                        "node_type": "uncertainty",
                        "headline": "Whether regulatory convergence will happen before critical capability thresholds",
                        "content": "Historical precedent from other technology sectors suggests eventual convergence, but timelines varied from 5 to 30 years.",
                        "credence": None,
                        "robustness": None,
                        "source_page_ids": ["c2d3e4f5"],
                        "children": [
                            {
                                "node_type": "evidence",
                                "headline": "Nuclear non-proliferation took ~25 years from first weapon to NPT",
                                "content": "The Treaty on the Non-Proliferation of Nuclear Weapons was opened for signature in 1968, 23 years after the first nuclear detonation.",
                                "credence": 8,
                                "robustness": 4,
                                "source_page_ids": ["11111111"],
                                "children": [
                                    {
                                        "node_type": "claim",
                                        "headline": "The nuclear analogy understates AI governance difficulty because AI is dual-use by default",
                                        "content": "Nuclear weapons require purpose-built infrastructure. AI capabilities are inherently dual-use.",
                                        "credence": 7,
                                        "robustness": 3,
                                        "source_page_ids": ["22222222"],
                                        "children": [
                                            {
                                                "node_type": "hypothesis",
                                                "headline": "Dual-use governance may require capability-gating rather than intent-based regulation",
                                                "content": "If you can't distinguish peaceful from dangerous uses at the model level, governance may need to shift to controlling capability thresholds.",
                                                "credence": 4,
                                                "robustness": 2,
                                                "source_page_ids": ["33333333"],
                                                "children": [
                                                    {
                                                        "node_type": "uncertainty",
                                                        "headline": "Whether capability thresholds can be meaningfully defined and measured",
                                                        "content": "Current benchmarks are poor proxies for dangerous capabilities.",
                                                        "credence": None,
                                                        "robustness": None,
                                                        "source_page_ids": ["44444444"],
                                                        "children": [],
                                                    },
                                                    {
                                                        "node_type": "evidence",
                                                        "headline": "METR and Apollo evals show dangerous capability measurement is nascent but progressing",
                                                        "content": "Structured evaluations for autonomous replication, cyber-offense, and CBRN uplift exist but have significant coverage gaps.",
                                                        "credence": 7,
                                                        "robustness": 3,
                                                        "source_page_ids": ["55555555"],
                                                        "children": [],
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                            {
                                "node_type": "claim",
                                "headline": "Biotech governance converged faster (~15 years from Asilomar to first binding frameworks)",
                                "content": "The Asilomar conference in 1975 led to NIH guidelines within a year and binding international frameworks by the early 1990s.",
                                "credence": 7,
                                "robustness": 3,
                                "source_page_ids": ["66666666"],
                                "children": [],
                            },
                        ],
                    },
                ],
            },
            {
                "node_type": "claim",
                "headline": "Technical standards bodies lack sufficient AI safety expertise",
                "content": "Organizations like ISO and IEEE have initiated AI safety working groups, but their membership is predominantly industry representatives.",
                "credence": 6,
                "robustness": 3,
                "source_page_ids": ["d3e4f5a6"],
                "children": [],
            },
            {
                "node_type": "hypothesis",
                "headline": "Compute governance may be the most tractable lever",
                "content": "The concentration of advanced chip manufacturing in a small number of facilities creates a natural chokepoint.",
                "credence": 5,
                "robustness": 3,
                "source_page_ids": ["e4f5a6b7"],
                "children": [
                    {
                        "node_type": "evidence",
                        "headline": "Export controls have measurably slowed some capability development",
                        "content": "Analysis suggests a 6-18 month delay in frontier capabilities following October 2022 export controls, though workarounds are emerging.",
                        "credence": 6,
                        "robustness": 3,
                        "source_page_ids": ["f5a6b7c8"],
                        "children": [
                            {
                                "node_type": "claim",
                                "headline": "The delay is eroding as alternative supply chains develop",
                                "content": "Chinese firms are investing heavily in domestic chip fabrication, and grey-market access to controlled chips continues.",
                                "credence": 5,
                                "robustness": 2,
                                "source_page_ids": ["77777777"],
                                "children": [],
                            },
                            {
                                "node_type": "evidence",
                                "headline": "Huawei's Ascend 910B achieves ~80% of H100 training performance",
                                "content": "Benchmarks from late 2025 suggest the domestically-produced Ascend 910B reaches roughly 80% of NVIDIA H100 throughput.",
                                "credence": 6,
                                "robustness": 3,
                                "source_page_ids": ["88888888"],
                                "children": [],
                            },
                        ],
                    },
                    {
                        "node_type": "uncertainty",
                        "headline": "Whether compute governance scales to open-weight models trained on commodity hardware",
                        "content": "As training efficiency improves and open-weight models proliferate, the compute bottleneck may weaken.",
                        "credence": None,
                        "robustness": None,
                        "source_page_ids": [],
                        "children": [],
                    },
                ],
            },
        ],
    },
    {
        "node_type": "uncertainty",
        "headline": "Whether current interpretability techniques will generalize to frontier systems",
        "content": "Sparse autoencoders and related techniques have shown promising results on smaller models, but it remains unclear whether these approaches will scale.",
        "credence": None,
        "robustness": None,
        "source_page_ids": ["a6b7c8d9", "b7c8d9e0"],
        "children": [
            {
                "node_type": "evidence",
                "headline": "Sparse autoencoders reveal interpretable features in mid-scale models",
                "content": "Recent work has demonstrated that sparse autoencoders can extract monosemantic features from models up to ~70B parameters.",
                "credence": 8,
                "robustness": 4,
                "source_page_ids": ["c8d9e0f1"],
                "children": [
                    {
                        "node_type": "claim",
                        "headline": "Feature universality across model families suggests interpretability may transfer",
                        "content": "Similar features appear across different model architectures and training runs.",
                        "credence": 5,
                        "robustness": 2,
                        "source_page_ids": ["99999999"],
                        "children": [
                            {
                                "node_type": "uncertainty",
                                "headline": "Whether universality holds for safety-relevant features specifically",
                                "content": "Most universality results are demonstrated on concrete features. Deception or power-seeking features may not exhibit the same universality.",
                                "credence": None,
                                "robustness": None,
                                "source_page_ids": ["aaaaaaaa"],
                                "children": [],
                            },
                        ],
                    },
                ],
            },
            {
                "node_type": "hypothesis",
                "headline": "Scaling interpretability may require fundamentally new approaches",
                "content": "Some researchers argue that the feature-level approach won't compose into system-level understanding at frontier scale.",
                "credence": 4,
                "robustness": 2,
                "source_page_ids": ["d9e0f1a2"],
                "children": [
                    {
                        "node_type": "evidence",
                        "headline": "Representation engineering can steer model behavior without feature-level understanding",
                        "content": "Activation addition and contrastive activation steering have shown that model behavior can be modified by manipulating representation-space directions.",
                        "credence": 7,
                        "robustness": 3,
                        "source_page_ids": ["cccccccc"],
                        "children": [],
                    },
                ],
            },
            {
                "node_type": "claim",
                "headline": "Interpretability progress is necessary but not sufficient for alignment",
                "content": "Even complete mechanistic understanding wouldn't automatically solve alignment.",
                "credence": 8,
                "robustness": 4,
                "source_page_ids": ["e0f1a2b3"],
                "children": [],
            },
        ],
    },
    {
        "node_type": "claim",
        "headline": "Organizational incentive structures systematically under-invest in safety",
        "content": "Market pressures, competitive dynamics, and short-term thinking create structural incentives to prioritize capability development over safety research.",
        "credence": 7,
        "robustness": 3,
        "source_page_ids": ["f1a2b3c4"],
        "children": [
            {
                "node_type": "evidence",
                "headline": "Safety team departures correlate with commercialization pressure",
                "content": "Multiple prominent safety researchers have left major labs citing concerns about safety being deprioritized.",
                "credence": 7,
                "robustness": 3,
                "source_page_ids": ["a2b3c4d5"],
                "children": [],
            },
            {
                "node_type": "hypothesis",
                "headline": "Third-party auditing could partially correct the incentive misalignment",
                "content": "Independent safety evaluations could make safety investment more legible to stakeholders and create accountability.",
                "credence": 5,
                "robustness": 2,
                "source_page_ids": ["b3c4d5e6"],
                "children": [
                    {
                        "node_type": "uncertainty",
                        "headline": "Whether auditors can keep pace with the rate of model releases",
                        "content": "Current audit capacity is orders of magnitude below what would be needed for comprehensive evaluation.",
                        "credence": None,
                        "robustness": None,
                        "source_page_ids": [],
                        "children": [],
                    },
                ],
            },
        ],
    },
    {
        "node_type": "hypothesis",
        "headline": "The window for meaningful governance intervention may be narrowing",
        "content": "If AI capabilities continue to advance rapidly while governance frameworks remain immature, there may be a closing window during which effective intervention is possible.",
        "credence": 6,
        "robustness": 2,
        "source_page_ids": ["c4d5e6f7"],
        "children": [
            {
                "node_type": "claim",
                "headline": "Analogies to other technologies suggest governance windows are real but hard to predict",
                "content": "Nuclear technology, social media, and genetic engineering all had identifiable periods where governance choices had outsized impact.",
                "credence": 7,
                "robustness": 3,
                "source_page_ids": ["d5e6f7a8"],
                "children": [],
            },
            {
                "node_type": "uncertainty",
                "headline": "Whether we are currently in such a window for AI",
                "content": "This depends heavily on timeline estimates for transformative AI, which remain deeply uncertain.",
                "credence": None,
                "robustness": None,
                "source_page_ids": ["e6f7a8b9"],
                "children": [],
            },
        ],
    },
    {
        "node_type": "context",
        "headline": "The research draws on both empirical analysis and theoretical frameworks",
        "content": "Findings are grounded in policy analysis, technical assessment, case studies, and expert interviews. The evidence base is stronger for current-state claims than for forward-looking predictions.",
        "credence": None,
        "robustness": None,
        "source_page_ids": ["f7a8b9c0"],
        "children": [
            {
                "node_type": "uncertainty",
                "headline": "Forward-looking claims about AI development trajectories are inherently speculative",
                "content": "Predictions about AI capabilities 3-10 years out have historically been unreliable in both directions.",
                "credence": None,
                "robustness": None,
                "source_page_ids": [],
                "children": [],
            },
        ],
    },
]
