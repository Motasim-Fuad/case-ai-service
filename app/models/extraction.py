"""Structured-extraction schemas.

`LLM*` classes are what GPT must return (OpenAI strict structured outputs: every field is required,
nullable where optional, no free-form dicts). The public result classes below extend them with
server-side fields (`ref`, `grounded`) that are computed in code, never by the model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

DocumentType = Literal[
    "police_report",
    "insurance_letter",
    "claim_form",
    "policy_document",
    "medical_record",
    "medical_bill",
    "repair_estimate",
    "witness_statement",
    "legal_notice",
    "correspondence",
    "photo_or_image",
    "other",
]
PartyRole = Literal[
    "claimant", "insured", "other_driver", "insurer", "adjuster", "attorney",
    "witness", "medical_provider", "police_officer", "repair_shop", "other",
]
EventType = Literal[
    "accident", "deadline", "request", "notice", "payment", "treatment",
    "inspection", "filing", "correspondence", "other",
]
AmountType = Literal[
    "claimed", "paid", "deductible", "premium", "demand", "estimate",
    "medical_charge", "limit", "other",
]


# ------------------------------------------------------------------ LLM-facing
class Evidence(BaseModel):
    page: int | None = Field(description="Page number from the nearest [[PAGE n]] marker.")
    quote: str | None = Field(description="Short VERBATIM excerpt (max 200 chars) that proves the item.")


class KeyValue(BaseModel):
    key: str
    value: str


class LLMParty(BaseModel):
    name: str
    role: PartyRole
    organization: str | None
    contact: str | None = Field(description="Phone / email / address if stated, else null.")
    evidence: Evidence
    confidence: float = Field(description="0.0 - 1.0")


class LLMEvent(BaseModel):
    title: str = Field(description="Short label, max ~12 words.")
    description: str
    event_date: str | None = Field(description="ISO date YYYY-MM-DD only if the full date is explicit, else null.")
    date_text: str | None = Field(description="The date exactly as written in the document.")
    event_type: EventType
    is_deadline: bool = Field(description="True only if the document requires something BY this date.")
    evidence: Evidence
    confidence: float


class LLMAmount(BaseModel):
    label: str
    amount: float = Field(description="Plain number, no currency symbol or thousands separators.")
    currency: str | None = Field(description="ISO 4217 code, e.g. USD, if determinable.")
    amount_type: AmountType
    evidence: Evidence
    confidence: float


class LLMRequest(BaseModel):
    description: str = Field(description="What is being requested / must be done.")
    requested_by: str | None
    due_date: str | None = Field(description="ISO date YYYY-MM-DD if explicit, else null.")
    evidence: Evidence
    confidence: float


class LLMClaimDetails(BaseModel):
    claim_number: str | None
    policy_number: str | None
    insurer: str | None
    insured_name: str | None
    date_of_loss: str | None = Field(description="ISO date YYYY-MM-DD if explicit, else null.")
    loss_location: str | None
    vehicle_info: str | None
    other: list[KeyValue] = Field(description="Any other case-relevant fact not covered above.")


class WindowExtraction(BaseModel):
    document_type: DocumentType
    window_summary: str = Field(description="2-4 factual sentences summarising these pages.")
    parties: list[LLMParty]
    events: list[LLMEvent]
    amounts: list[LLMAmount]
    requests: list[LLMRequest]
    claim_details: LLMClaimDetails


class FinalSummary(BaseModel):
    document_type: DocumentType
    summary: str = Field(description="3-5 sentence factual summary of the whole document.")


class RagAnswer(BaseModel):
    answer: str
    answerable: bool = Field(description="False if the context does not contain the answer.")
    cited_sources: list[int] = Field(description="Numbers N of the [S<N>] context blocks actually used.")


# ---------------------------------------------------------------- public result
class Party(LLMParty):
    ref: str = ""
    grounded: bool = True


class CaseEvent(LLMEvent):
    ref: str = ""
    grounded: bool = True


class Amount(LLMAmount):
    ref: str = ""
    grounded: bool = True


class ActionRequest(LLMRequest):
    ref: str = ""
    grounded: bool = True


class ClaimDetails(LLMClaimDetails):
    pass


class PageInfo(BaseModel):
    page: int
    method: Literal["text", "ocr", "docx", "txt"]
    char_count: int
    ocr_confidence: float | None = None
    error: str | None = None


class ExtractionOutput(BaseModel):
    """Intermediate (cacheable) output of the extraction stage."""

    document_type: DocumentType = "other"
    summary: str = ""
    parties: list[Party] = Field(default_factory=list)
    events: list[CaseEvent] = Field(default_factory=list)
    amounts: list[Amount] = Field(default_factory=list)
    requests: list[ActionRequest] = Field(default_factory=list)
    claim_details: ClaimDetails = Field(
        default_factory=lambda: ClaimDetails(
            claim_number=None, policy_number=None, insurer=None, insured_name=None,
            date_of_loss=None, loss_location=None, vehicle_info=None, other=[],
        )
    )
    warnings: list[str] = Field(default_factory=list)
    windows_total: int = 0
    windows_failed: int = 0


class DocumentExtractionResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    case_id: str
    document_type: DocumentType = "other"
    summary: str = ""
    parties: list[Party] = Field(default_factory=list)
    events: list[CaseEvent] = Field(default_factory=list)
    amounts: list[Amount] = Field(default_factory=list)
    requests: list[ActionRequest] = Field(default_factory=list)
    claim_details: ClaimDetails = Field(default_factory=lambda: ExtractionOutput().claim_details)
    pages: list[PageInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    processing_status: Literal["completed", "partial", "failed"] = "completed"
    error_message: str | None = None
