"""Structured output models for the evaluation agent."""

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel, Field


class TriLevel(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"


class GroundingLevel(str, Enum):
    WELL_GROUNDED = "well-grounded"
    WEAKLY_GROUNDED = "weakly-grounded"
    UNGROUNDED = "ungrounded"


class OverlookedConsideration(BaseModel):
    missing_element: str = Field(
        description="What is absent — a line of reasoning, a subquestion, a perspective."
    )
    why_it_matters: str = Field(
        description="How this gap could affect the overall conclusion."
    )
    suggested_action: str = Field(
        description="What kind of investigation or analysis would fill this gap."
    )


class UnderdevelopedLine(BaseModel):
    area: str = Field(
        description="The subquestion, claim, or line of reasoning that is underdeveloped."
    )
    current_state: str = Field(
        description=(
            "What exists in the workspace — cite page headlines with their "
            '8-char short IDs, e.g. [abcd1234] "Solar payback periods..."'
        )
    )
    whats_lacking: str = Field(
        description="What specific analysis, evidence, or depth is missing."
    )
    suggested_action: str = Field(
        description="What further work would strengthen this area."
    )


class Inconsistency(BaseModel):
    conflict: str = Field(description="Describe the contradiction.")
    pages_involved: str = Field(
        description=(
            "Cite the specific pages on both sides, with headlines and 8-char short IDs."
        )
    )
    impact: str = Field(
        description="How this inconsistency affects the reliability of the overall analysis."
    )
    suggested_resolution: str = Field(
        description="How to resolve or investigate the conflict."
    )


class PriorityImprovement(BaseModel):
    description: str = Field(description="Short description of the improvement.")
    rationale: str = Field(
        description="Why this is high-priority and what action to take."
    )


class FeedbackEvaluation(BaseModel):
    """Evaluation structured around overlooked, underdeveloped, and inconsistent areas."""

    overlooked_considerations: Sequence[OverlookedConsideration] = Field(
        description=(
            "Lines of reasoning, arguments, or subquestions that are completely "
            "absent from the workspace but should be included."
        )
    )
    underdeveloped_lines: Sequence[UnderdevelopedLine] = Field(
        description=(
            "Lines of investigation that are key to the conclusion but lack "
            "sufficient analysis or grounding."
        )
    )
    inconsistencies: Sequence[Inconsistency] = Field(
        description=(
            "Places where the analysis contradicts itself — conflicting claims, "
            "judgements, or assumptions across different parts of the graph."
        )
    )
    priority_improvements: Sequence[PriorityImprovement] = Field(
        description=(
            "Ranked list of the most impactful improvements, drawing from all "
            "three dimensions. Focus on what would most change or strengthen "
            "the top-level judgement."
        )
    )

    def render_markdown(self) -> str:
        sections: list[str] = []

        sections.append("### Overlooked Considerations\n")
        if self.overlooked_considerations:
            for item in self.overlooked_considerations:
                sections.append(
                    f"- **Missing element:** {item.missing_element}\n"
                    f"- **Why it matters:** {item.why_it_matters}\n"
                    f"- **Suggested action:** {item.suggested_action}\n"
                )
        else:
            sections.append("No significant gaps identified.\n")

        sections.append("### Underdeveloped Key Lines\n")
        if self.underdeveloped_lines:
            for item in self.underdeveloped_lines:
                sections.append(
                    f"- **Area:** {item.area}\n"
                    f"- **Current state:** {item.current_state}\n"
                    f"- **What's lacking:** {item.whats_lacking}\n"
                    f"- **Suggested action:** {item.suggested_action}\n"
                )
        else:
            sections.append("No significant underdevelopment identified.\n")

        sections.append("### Inconsistencies\n")
        if self.inconsistencies:
            for item in self.inconsistencies:
                sections.append(
                    f"- **Conflict:** {item.conflict}\n"
                    f"- **Pages involved:** {item.pages_involved}\n"
                    f"- **Impact:** {item.impact}\n"
                    f"- **Suggested resolution:** {item.suggested_resolution}\n"
                )
        else:
            sections.append("No significant inconsistencies found.\n")

        sections.append("### Priority Improvements\n")
        for i, item in enumerate(self.priority_improvements, 1):
            sections.append(f"{i}. **{item.description}** — {item.rationale}\n")

        return "\n".join(sections)


class FalsifiableClaimAssessment(BaseModel):
    claim: str = Field(description="The claim, quoted or paraphrased.")
    importance: TriLevel = Field(
        description="How important this claim is to the overall judgement."
    )
    falsifiability: TriLevel = Field(
        description="How specific and empirically testable the claim is."
    )
    grounding: GroundingLevel = Field(
        description="How well the claim is supported by evidence in the workspace."
    )
    evidence_chain: str = Field(
        description=(
            "Brief description of the supporting evidence found, with page "
            "headlines and their 8-char short IDs."
        )
    )
    gaps: str = Field(
        description=(
            "What's missing — unsupported links, absent sources, "
            "unaddressed counter-evidence."
        )
    )


class FalsifiableGroundingEvaluation(BaseModel):
    """Evaluation of falsifiable claims and their evidential grounding."""

    claims: Sequence[FalsifiableClaimAssessment] = Field(
        description="Assessment of each important falsifiable claim in the judgement."
    )
    overall_assessment: str = Field(
        description=(
            "Summary of the judgement's overall evidential quality: how many "
            "claims are well-grounded vs. not, the most significant gaps, "
            "and what further investigation would be most valuable."
        )
    )

    def render_markdown(self) -> str:
        sections: list[str] = []

        sections.append("### Claims Assessment\n")
        for item in self.claims:
            sections.append(
                f"- **Claim:** {item.claim}\n"
                f"- **Importance:** {item.importance.value}\n"
                f"- **Falsifiability:** {item.falsifiability.value}\n"
                f"- **Grounding:** {item.grounding.value}\n"
                f"- **Evidence chain:** {item.evidence_chain}\n"
                f"- **Gaps:** {item.gaps}\n"
            )

        sections.append("### Overall Assessment\n")
        sections.append(self.overall_assessment + "\n")

        return "\n".join(sections)


class ClaimAssessment(BaseModel):
    claim: str = Field(description="The claim, quoted or paraphrased.")
    grounding: GroundingLevel = Field(
        description="How well the claim is supported by evidence in the workspace."
    )
    evidence_chain: str = Field(
        description=(
            "Brief description of the supporting evidence found, with page IDs."
        )
    )
    gaps: str = Field(
        description=(
            "What's missing — unsupported links, absent sources, "
            "unaddressed counter-evidence."
        )
    )


class GroundingEvaluation(BaseModel):
    """Evaluation of claim grounding quality."""

    claims: Sequence[ClaimAssessment] = Field(
        description="Assessment of each important claim in the judgement."
    )
    overall_assessment: str = Field(
        description=(
            "Summary of the judgement's overall evidential quality: how many "
            "claims are well-grounded vs. not, the most significant gaps, "
            "and what further investigation would be most valuable."
        )
    )

    def render_markdown(self) -> str:
        sections: list[str] = []

        sections.append("### Claims Assessment\n")
        for item in self.claims:
            sections.append(
                f"- **Claim:** {item.claim}\n"
                f"- **Grounding:** {item.grounding.value}\n"
                f"- **Evidence chain:** {item.evidence_chain}\n"
                f"- **Gaps:** {item.gaps}\n"
            )

        sections.append("### Overall Assessment\n")
        sections.append(self.overall_assessment + "\n")

        return "\n".join(sections)


EvalReport = FeedbackEvaluation | FalsifiableGroundingEvaluation | GroundingEvaluation

EVAL_TYPE_MODELS: dict[str, type[EvalReport]] = {
    "feedback": FeedbackEvaluation,
    "default": FalsifiableGroundingEvaluation,
    "grounding": GroundingEvaluation,
}
