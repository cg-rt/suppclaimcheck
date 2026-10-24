"""Shapes of the data core exchanges: API bodies, and the structured output it asks the LLM for.

The LLM output models forbid extra fields; their JSON schema is sent to the gateway for the provider's
structured-output mode, and the reply is validated against the same model (AGENTS.md section 8).
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class Claim(BaseModel):
    """A claim as the user confirms it. Only supplement and effect are required."""

    supplement: str = Field(min_length=1, max_length=200)
    effect: str = Field(min_length=1, max_length=500)
    population: str | None = Field(default=None, max_length=300)
    dose: str | None = Field(default=None, max_length=200)
    duration: str | None = Field(default=None, max_length=200)


class JobCreate(BaseModel):
    user_id: int
    input_type: Literal["text"] = "text"
    input_text: str = Field(min_length=1, max_length=20_000)


class Job(BaseModel):
    id: UUID
    user_id: int
    state: JobState
    input_type: str
    input_text: str
    extracted_claims: list[dict] | None
    confirmed_claim: Claim | None
    result: dict | None
    error: str | None
    config_version: str
    created_at: datetime
    updated_at: datetime


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


class PassageAssessment(_Strict):
    passage_id: str
    label: Literal["supports", "contradicts", "not_relevant"]
    quote: str
    reason: str


class Assessment(_Strict):
    assessments: list[PassageAssessment]
