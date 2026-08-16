"""Domain schemas — the structured payloads that ride inside A2A Parts.

A2A carries opaque content; we choose to put a validated JSON object in a
``data`` Part. Validating the incoming customer profile here is our first line
of input validation (hardened further in Phase 5).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IdDocument(BaseModel):
    type: str = "passport"
    number: str | None = None
    expired: bool = False


class Transaction(BaseModel):
    """One account movement. The AML agent analyses these directly, so every
    flag traces back to a row the analyst can see."""

    date: str = ""
    amount: float = Field(ge=0)
    direction: Literal["in", "out"] = "in"
    counterparty: str = ""
    channel: Literal["wire", "cash", "card", "crypto", "transfer", "cheque"] = "wire"
    country: str = ""  # counterparty jurisdiction (optional)


class CustomerProfile(BaseModel):
    """The subject of an investigation — the real KYC record a bank collects."""

    # --- identity ---
    full_name: str = Field(min_length=1, max_length=200)
    date_of_birth: str | None = None
    nationality: str = ""                 # citizenship (geographic risk)
    country: str = ""                     # country of residence
    city: str = ""
    # --- employment & wealth ---
    occupation: str = ""
    employer: str = ""
    industry: str = ""                    # drives customer/industry risk
    employment_status: str = ""           # employed | self-employed | ...
    annual_income: float = 0.0
    declared_source_of_funds: str = ""
    source_of_wealth: str = ""
    # --- account & onboarding ---
    account_purpose: str = ""
    expected_monthly_volume: float = 0.0
    account_age_days: int = 0
    onboarding_channel: str = "in_person"  # in_person | remote (non-face-to-face)
    pep_declared: bool = False             # self-declared politically exposed
    tax_residency: str = ""
    # --- documents ---
    id_document: IdDocument = Field(default_factory=IdDocument)
    # Real, analyst-provided ledger. When present, the AML agent analyses THIS
    # instead of synthesising one — so the numbers come from real input.
    transactions: list[Transaction] = Field(default_factory=list)
    notes: str = ""  # free-text scenario / observed behaviour

    @classmethod
    def demo(cls) -> CustomerProfile:
        return cls(
            full_name="Viktor Petrov",
            date_of_birth="1975-03-11",
            country="Russia",
            occupation="import/export consultant",
            declared_source_of_funds="consulting fees",
            expected_monthly_volume=25_000,
            account_age_days=45,
            id_document=IdDocument(type="passport", number="RU8837261"),
            notes="Multiple cash deposits just under threshold; rapid outward transfers.",
        )


def parse_profile(data: Any) -> CustomerProfile:
    """Validate an inbound data Part into a CustomerProfile (raises on bad input)."""
    if isinstance(data, CustomerProfile):
        return data
    if not isinstance(data, dict):
        raise ValueError("expected a customer profile object")
    return CustomerProfile.model_validate(data)


# Risk banding thresholds (our scoring choice, not A2A).
RISK_BANDS = [
    (75, "CRITICAL"),
    (50, "HIGH"),
    (25, "MEDIUM"),
    (0, "LOW"),
]


def band_for(score: int) -> str:
    for threshold, label in RISK_BANDS:
        if score >= threshold:
            return label
    return "LOW"
