"""Shapes of the data core exchanges: API bodies, and the structured output it asks the LLM for.

The LLM output models forbid extra fields; their JSON schema is sent to the gateway for the provider's
structured-output mode, and the reply is validated against the same model (AGENTS.md section 8).
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobState(StrEnum):
    submitted = "submitted"
    extracting = "extracting"
    awaiting_confirmation = "awaiting_confirmation"
    searching = "searching"
    retrieving = "retrieving"
    assessing = "assessing"
    verifying = "verifying"
    scoring = "scoring"
    done = "done"
    failed = "failed"
    refused = "refused"
    # Stopped before searching: the text needs a specific effect or a supplement (P3.01, docs/POLICY.md).
    needs_clarification = "needs_clarification"


class Claim(BaseModel):
    """A claim as the user confirms it. Only supplement and effect are required."""

    supplement: str = Field(min_length=1, max_length=200)
    effect: str = Field(min_length=1, max_length=500)
    population: str | None = Field(default=None, max_length=300)
    dose: str | None = Field(default=None, max_length=200)
    duration: str | None = Field(default=None, max_length=200)


# The longest web address a check accepts (P3.12); the gateway checks the rest when it reads the page.
MAX_URL = 2048
_URL = re.compile(r"^https?://[^\s/?#@]+(?:[/?#]\S*)?$", re.IGNORECASE)


class JobCreate(BaseModel):
    user_id: int
    # "url": input_text is a web page's address, and the worker has the gateway read the page (P3.12).
    input_type: Literal["text", "url"] = "text"
    input_text: str = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _one_address(self):
        if self.input_type == "url" and (
            len(self.input_text) > MAX_URL or not _URL.match(self.input_text)
        ):
            raise ValueError("a link must be one http or https address")
        return self


class FeedbackAnswers(BaseModel):
    """A person's rating of their own result (P4.03). Every answer is optional, but not all at once."""

    verdict_right: Literal["yes", "no", "unsure"] | None = None
    understandable: Literal["yes", "partly", "no"] | None = None
    missing: str | None = Field(default=None, max_length=1000)

    @field_validator("missing")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _something(self):
        if (
            self.verdict_right is None
            and self.understandable is None
            and self.missing is None
        ):
            raise ValueError("answer at least one question")
        return self


class FeedbackIn(FeedbackAnswers):
    # The account sending it: core checks it is the check's own account (web checks too).
    user_id: int


class Feedback(FeedbackAnswers):
    updated_at: datetime


class Job(BaseModel):
    id: UUID
    user_id: int
    state: JobState
    input_type: str
    input_text: str
    # For a link (P3.12): the address after redirects, title, site name and how much text was read.
    source: dict | None = None
    extracted_claims: list[dict] | None
    confirmed_claim: Claim | None
    result: dict | None
    error: str | None
    config_version: str
    # Safety topics found by triage (P3.03) and whether the owner should review the job.
    sensitive_topics: list[str] = []
    needs_review: bool = False
    created_at: datetime
    updated_at: datetime
    # P4.04: set by the database on every state change (migration 0008).
    confirmed_at: datetime | None = None
    finished_at: datetime | None = None
    feedback: Feedback | None = None


class JobSummary(BaseModel):
    """One row of a user's history: enough to list it, not the whole result."""

    id: UUID
    state: JobState
    input_type: str = "text"
    input_text: str
    # A link's page title, once it has been read (P3.12).
    source_title: str | None = None
    confirmed_claim: Claim | None
    verdict: str | None
    score: int | None
    created_at: datetime


class JobEvent(BaseModel):
    state: JobState
    detail: str | None
    at: datetime


# Structured LLM outputs. Strict: no extra fields, every field present (nullable where optional).
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedClaim(_Strict):
    supplement: str
    effect: str
    population: str | None
    dose: str | None
    duration: str | None
    quote: str


class Extraction(_Strict):
    claims: list[ExtractedClaim]


# Triage (P3.01-P3.03, D-050), from pipeline-v5: the model labels, code decides (pipeline.triage).
RequestKind = Literal[
    "claim_check",
    "personal_advice",
    "marketing",
    "off_topic",
    "system_prompt",
    "abusive",
]
SensitiveTopic = Literal[
    "pregnancy", "children", "serious_illness", "high_dose", "drug_interaction"
]


class TriagedClaim(ExtractedClaim):
    specific: bool  # names an effect research can test, not "good for you"


class Triage(_Strict):
    request: RequestKind
    instructions_found: bool  # the text contains instructions addressed to the system
    sensitive_topics: list[SensitiveTopic]


class TriagedExtraction(_Strict):
    claims: list[TriagedClaim]
    triage: Triage


class PassageAssessment(_Strict):
    passage_id: str
    label: Literal["supports", "contradicts", "not_relevant"]
    quote: str
    reason: str


class Assessment(_Strict):
    assessments: list[PassageAssessment]


class GuardedAssessment(Assessment):
    """pipeline-v5 (P3.02): the model also reports instructions found in the article's passages."""

    instructions_found: bool
