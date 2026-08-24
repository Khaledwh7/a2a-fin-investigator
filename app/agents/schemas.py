"""Domain schemas — the structured payloads that ride inside A2A Parts.

A2A carries opaque content; we choose to put a validated JSON object in a
``data`` Part. Validating the incoming customer profile here is our first line
of input validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# --- Numeral normalization -------------------------------------------------
# Map Arabic-Indic (U+0660–0669) and Extended Arabic-Indic / Persian
# (U+06F0–06F9) digits — plus the Arabic decimal (٫) and thousands (٬)
# separators — to ASCII. A salary entered as ٥٠٬٠٠٠ is then stored and shown
# as 50,000 rather than in Eastern numerals.
_DIGIT_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬",
    "01234567890123456789.,",
)


def normalize_digits(value: Any) -> Any:
    """Convert Arabic-Indic / Persian numerals in a string to ASCII digits.

    Non-strings pass through unchanged.
    """
    return value.translate(_DIGIT_MAP) if isinstance(value, str) else value


def _deep_normalize(value: Any) -> Any:
    """Apply :func:`normalize_digits` to every string in a nested structure."""
    if isinstance(value, str):
        return normalize_digits(value)
    if isinstance(value, list):
        return [_deep_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _deep_normalize(v) for k, v in value.items()}
    return value


def _to_number(value: Any) -> Any:
    """Coerce a possibly Arabic / comma-grouped numeric string to a number."""
    if isinstance(value, str):
        s = normalize_digits(value).replace(",", "").replace(" ", "")
        return s.replace("$", "").strip() or 0
    return value


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

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        """Let an omitted-but-present field fall back to its default.

        Spreadsheet-shaped sources (a UI grid, a CSV import) send every column
        for every row, so a cell the analyst left blank arrives as null. Without
        this, one blank ``direction`` rejects the entire profile.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> Any:
        return _to_number(v)

    @field_validator("direction", "channel", mode="before")
    @classmethod
    def _normalize_enum(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


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
    # The analyst-provided ledger — the only transaction evidence there is. With
    # none, AML and Fraud report "not assessed"; nothing is ever synthesised.
    transactions: list[Transaction] = Field(default_factory=list)
    notes: str = ""  # free-text scenario / observed behaviour

    @model_validator(mode="before")
    @classmethod
    def _normalize_numerals(cls, data: Any) -> Any:
        """Normalize Arabic-Indic numerals across every inbound field."""
        return _deep_normalize(data) if isinstance(data, dict) else data

    @field_validator("annual_income", "expected_monthly_volume", mode="before")
    @classmethod
    def _coerce_amounts(cls, v: Any) -> Any:
        return _to_number(v)

    @classmethod
    def demo(cls) -> CustomerProfile:
        """A worked case, ledger included.

        The transactions are the point: the structuring and pass-through findings
        this profile produces are computed from these rows, so every number in the
        demo traces back to something an analyst can look at.
        """
        return cls(
            full_name="Viktor Petrov",
            date_of_birth="1975-03-11",
            country="Russia",
            occupation="import/export consultant",
            declared_source_of_funds="consulting fees",
            expected_monthly_volume=25_000,
            account_age_days=45,
            id_document=IdDocument(type="passport", number="RU8837261"),
            transactions=DEMO_LEDGER,
            notes="Multiple cash deposits just under threshold; rapid outward transfers.",
        )


# The demo case's ledger: ordinary consulting income, then four cash deposits
# parked just under the 10,000 reporting threshold, then funds routed straight
# back out to an offshore counterparty. Structuring + pass-through, in rows.
DEMO_LEDGER: list[dict[str, Any]] = [
    {"date": "2026-01-06", "amount": 6200.0, "direction": "in",
     "counterparty": "Meridian Consulting", "channel": "transfer"},
    {"date": "2026-01-09", "amount": 1450.0, "direction": "out",
     "counterparty": "Landlord Ltd", "channel": "transfer"},
    {"date": "2026-01-13", "amount": 9400.0, "direction": "in",
     "counterparty": "Cash Deposit", "channel": "cash"},
    {"date": "2026-01-14", "amount": 9750.0, "direction": "in",
     "counterparty": "Cash Deposit", "channel": "cash"},
    {"date": "2026-01-15", "amount": 9200.0, "direction": "in",
     "counterparty": "Cash Deposit", "channel": "cash"},
    {"date": "2026-01-16", "amount": 9600.0, "direction": "in",
     "counterparty": "Cash Deposit", "channel": "cash"},
    {"date": "2026-01-17", "amount": 14000.0, "direction": "in",
     "counterparty": "Offshore Holdings Ltd", "channel": "wire",
     "country": "Cayman Islands"},
    {"date": "2026-01-18", "amount": 13500.0, "direction": "out",
     "counterparty": "Offshore Holdings Ltd", "channel": "wire",
     "country": "Cayman Islands"},
    {"date": "2026-01-20", "amount": 8800.0, "direction": "out",
     "counterparty": "Shell Corp BVI", "channel": "wire",
     "country": "British Virgin Islands"},
]


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
