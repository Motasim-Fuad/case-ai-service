"""LLM access behind one tiny interface: `parse(system, user, schema) -> schema instance`.

* OpenAILLM  – production. Uses Structured Outputs (schema-enforced), never free-text JSON parsing.
* MockLLM    – offline regex heuristics so the full pipeline can be smoke-tested without any API key.
"""
from __future__ import annotations

import re
from typing import Protocol, TypeVar

import openai
import pydantic
from openai import OpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.core.config import Settings
from app.core.errors import LLMConfigError, LLMError, LLMOutputError, LLMRefusalError
from app.models.extraction import (
    Evidence, FinalSummary, KeyValue, LLMAmount, LLMClaimDetails, LLMEvent, LLMParty,
    LLMRequest, RagAnswer, WindowExtraction,
)

T = TypeVar("T", bound=BaseModel)
_TRANSIENT = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)


class LLMClient(Protocol):
    def parse(
        self, *, system: str, user: str, schema: type[T], model: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> T: ...


class OpenAILLM:
    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._s = settings
        self._client = client or OpenAI(
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retries are handled by tenacity below (single source of truth)
        )

    def parse(self, *, system, user, schema, model=None, history=None):  # type: ignore[no-untyped-def]
        messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
        model = model or self._s.openai_extraction_model

        @retry(
            retry=retry_if_exception_type(_TRANSIENT),
            wait=wait_random_exponential(multiplier=1, max=30),
            stop=stop_after_attempt(self._s.llm_max_attempts),
            reraise=True,
        )
        def _call():  # type: ignore[no-untyped-def]
            kwargs: dict = {"model": model, "messages": messages, "response_format": schema}
            if self._s.llm_temperature is not None:
                kwargs["temperature"] = self._s.llm_temperature
            parse_fn = getattr(self._client.chat.completions, "parse", None) or self._client.beta.chat.completions.parse
            return parse_fn(**kwargs)

        try:
            completion = _call()
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
            raise LLMConfigError(f"OpenAI configuration error (key/model?): {exc}") from exc
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError, pydantic.ValidationError) as exc:
            raise LLMOutputError(f"Model output could not be parsed: {type(exc).__name__}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMRefusalError(f"Model refused: {message.refusal}")
        if message.parsed is None:
            raise LLMOutputError("Model returned no parsed output")
        return message.parsed


# ---------------------------------------------------------------------------- mock
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTHS.split("|"))}
_DATE_RES = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I),
]
_AMOUNT_RE = re.compile(r"(?:USD\s*|\$\s*)([\d]{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_PAGE_RE = re.compile(r"\[\[PAGE (\d+)\]\]")
_ROLE_RE = re.compile(
    r"^\s*(Claimant|Insured|Adjuster|Insurer|Insurance Company|Witness|Attorney|Other Driver)\s*[:\-]\s*(.+?)\s*$",
    re.I | re.M,
)
_ROLE_MAP = {"insurance company": "insurer", "other driver": "other_driver"}


def _find_dates(line: str) -> list[tuple[str, str | None]]:
    """Return [(date_text, iso_or_None)]."""
    found: list[tuple[str, str | None]] = []
    for idx, rx in enumerate(_DATE_RES):
        for m in rx.finditer(line):
            try:
                if idx == 0:
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif idx == 1:
                    mo, d, y = int(m[1]), int(m[2]), int(m[3])
                else:
                    mo, d, y = _MONTH_NUM[m[1].capitalize()], int(m[2]), int(m[3])
                found.append((m.group(0), f"{y:04d}-{mo:02d}-{d:02d}"))
            except (ValueError, KeyError):
                found.append((m.group(0), None))
    return found


def _split_pages(text: str) -> list[tuple[int, str]]:
    parts = _PAGE_RE.split(text)
    return [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


class MockLLM:
    """Deterministic, regex-based stand-in. NOT intelligent – for offline smoke tests only."""

    def parse(self, *, system, user, schema, model=None, history=None):  # type: ignore[no-untyped-def]
        if schema is WindowExtraction:
            return self._window(user)  # type: ignore[return-value]
        if schema is FinalSummary:
            summaries = re.findall(r"^- (.+)$", user, re.M)
            return FinalSummary(document_type="other", summary=" ".join(summaries)[:600] or "Summary unavailable.")  # type: ignore[return-value]
        if schema is RagAnswer:
            blocks = re.findall(r"\[S(\d+)\][^\n]*\n(.+?)(?=\n\[S\d+\]|\Z)", user, re.S)
            if not blocks:
                return RagAnswer(answer="Not found in the case documents.", answerable=False, cited_sources=[])  # type: ignore[return-value]
            num, text = blocks[0]
            return RagAnswer(  # type: ignore[return-value]
                answer=f"[mock answer] Most relevant passage: {text.strip()[:300]}",
                answerable=True, cited_sources=[int(num)],
            )
        raise NotImplementedError(f"MockLLM cannot produce {schema.__name__}")

    # ---- heuristics -----------------------------------------------------
    def _window(self, user: str) -> WindowExtraction:
        parties: list[LLMParty] = []
        events: list[LLMEvent] = []
        amounts: list[LLMAmount] = []
        requests: list[LLMRequest] = []
        claim: dict[str, str | None] = {"claim_number": None, "policy_number": None, "insurer": None,
                                         "insured_name": None, "date_of_loss": None,
                                         "loss_location": None, "vehicle_info": None}
        full = user.lower()
        for page, body in _split_pages(user):
            for m in _ROLE_RE.finditer(body):
                role = _ROLE_MAP.get(m[1].lower(), m[1].lower())
                parties.append(LLMParty(name=m[2], role=role, organization=None, contact=None,  # type: ignore[arg-type]
                                        evidence=Evidence(page=page, quote=m.group(0).strip()[:200]), confidence=0.8))
                if role == "insurer":
                    claim["insurer"] = claim["insurer"] or m[2]
                if role == "insured":
                    claim["insured_name"] = claim["insured_name"] or m[2]
            for cm, key in ((re.search(r"Claim\s*(?:No\.?|Number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]+)", body, re.I), "claim_number"),
                            (re.search(r"Policy\s*(?:No\.?|Number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]+)", body, re.I), "policy_number")):
                if cm and not claim[key]:
                    claim[key] = cm.group(1)
            for line in (ln.strip() for ln in body.splitlines() if ln.strip()):
                low = line.lower()
                dates = _find_dates(line)
                is_deadline = any(k in low for k in ("deadline", "no later than", "due by", "must be submitted", "submit", "by "))
                for date_text, iso in dates:
                    etype = "accident" if ("date of loss" in low or "accident" in low or "collision" in low) else (
                        "deadline" if is_deadline else "other")
                    events.append(LLMEvent(
                        title=" ".join(line.split()[:10]), description=line[:300], event_date=iso, date_text=date_text,
                        event_type=etype, is_deadline=is_deadline and etype != "accident",  # type: ignore[arg-type]
                        evidence=Evidence(page=page, quote=line[:200]), confidence=0.7,
                    ))
                    if etype == "accident" and not claim["date_of_loss"]:
                        claim["date_of_loss"] = iso
                    if any(k in low for k in ("please submit", "please provide", "we request", "you must", "required to")):
                        requests.append(LLMRequest(description=line[:300], requested_by=claim["insurer"], due_date=iso,
                                                   evidence=Evidence(page=page, quote=line[:200]), confidence=0.7))
                if not dates and any(k in low for k in ("please submit", "please provide", "we request", "you must")):
                    requests.append(LLMRequest(description=line[:300], requested_by=claim["insurer"], due_date=None,
                                               evidence=Evidence(page=page, quote=line[:200]), confidence=0.6))
                for am in _AMOUNT_RE.finditer(line):
                    try:
                        value = float(am.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    label_words = line[: am.start()].split()[-4:]
                    atype = next((t for k, t in (("deductible", "deductible"), ("premium", "premium"), ("estimate", "estimate"),
                                                 ("repair", "estimate"), ("paid", "paid"), ("payment", "paid"), ("demand", "demand"),
                                                 ("medical", "medical_charge"), ("hospital", "medical_charge"), ("claim", "claimed"))
                                  if k in low), "other")
                    amounts.append(LLMAmount(label=" ".join(label_words).strip(": ") or "Amount", amount=value, currency="USD",
                                             amount_type=atype, evidence=Evidence(page=page, quote=line[:200]), confidence=0.8))  # type: ignore[arg-type]
        if "police report" in full:
            dtype = "police_report"
        elif "claim number" in full or "dear " in full:
            dtype = "insurance_letter"
        elif "estimate" in full and "repair" in full:
            dtype = "repair_estimate"
        elif "hospital" in full or "medical" in full:
            dtype = "medical_bill"
        else:
            dtype = "other"
        return WindowExtraction(
            document_type=dtype,  # type: ignore[arg-type]
            window_summary=f"[mock] Document of type {dtype} with {len(events)} dated events and {len(amounts)} amounts.",
            parties=parties, events=events, amounts=amounts, requests=requests,
            claim_details=LLMClaimDetails(**claim, other=[KeyValue(key="mock", value="heuristic extraction")]),
        )
