"""Domain types for concept assessment — shared by assess_concept and closing_reviewers."""

from pydantic import BaseModel, Field

SCREENING_PHASE = 'screening'
VALIDATION_PHASE = 'validation'

SCREENING_MAX_ROUNDS = 2
VALIDATION_MAX_ROUNDS = 8
SCREENING_FRUIT_THRESHOLD = 5
VALIDATION_FRUIT_THRESHOLD = 3


class ConceptAssessmentReview(BaseModel):
    remaining_fruit: int = Field(
        description=(
            '0-10: how much more testing would meaningfully change your assessment. '
            '0 = verdict is settled; 10 = barely started.'
        )
    )
    confidence_in_output: float = Field(description='0-5 confidence in this assessment')
    score: int = Field(
        description=(
            '1-10: overall usefulness of this concept for the research. '
            '1-4 = not useful enough to warrant promotion; '
            '5-7 = moderately useful, borderline; '
            '8-10 = clearly useful, strong candidate for promotion.'
        )
    )
    what_worked: str = Field(description='Where the concept added clarity or revealed something')
    what_didnt: str = Field(description='Where the concept failed or added noise')
    could_existing_claims_be_restated: bool = Field(
        description='Whether existing claims could be stated more usefully with this concept'
    )
    did_it_reveal_new_considerations: bool = Field(
        description='Whether applying the concept surfaced considerations not already in the workspace'
    )
    did_it_resolve_existing_tensions: bool = Field(
        description='Whether the concept dissolved any apparent contradictions or tensions'
    )
    suggested_refinements: str = Field(
        '', description='How the concept could be sharpened or narrowed to be more useful'
    )
    screening_passed: bool = Field(
        description=(
            'Whether this concept warrants deeper validation. '
            'True if there is genuine promise, even if uncertain. '
            'False if the concept clearly does not add value.'
        )
    )


REVIEW_SYSTEM_PROMPT = (
    'You are a research assistant completing a closing review of a concept assessment '
    'you just performed. Be honest and specific. Most concept proposals should not be '
    "promoted \u2014 a clear 'no' is more useful than an uncertain 'maybe'."
)
