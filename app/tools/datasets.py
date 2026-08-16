"""Reference data — realistic stand-ins for the sources a financial-crime unit uses.

All lists are FICTIONAL sample data, but shaped like the real thing (OFAC/EU
sanctions entries, a PEP list, FATF/Basel-style country-risk tiers) so the
screening and scoring logic that runs against them is genuine. In production you
would swap these tables for the live OFAC SDN / EU consolidated / FATF feeds
without changing the algorithms.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Country risk — tiered scores (0–100), aligned to how AML country risk works:
#   BLOCKED (call-for-action / comprehensively sanctioned) → 100
#   HIGH    (FATF grey-list / high secrecy / conflict)      → 70
#   MEDIUM  (elevated corruption / tax-haven traits)        → 40
#   default (low)                                            → 10
# --------------------------------------------------------------------------- #
BLOCKED_JURISDICTIONS: set[str] = {
    "iran", "north korea", "syria", "cuba", "crimea", "myanmar",
}
HIGH_RISK_COUNTRIES: set[str] = {
    "russia", "belarus", "venezuela", "afghanistan", "yemen", "south sudan",
    "panama", "cayman islands", "british virgin islands", "seychelles",
    "lebanon", "zimbabwe", "nicaragua", "mali", "haiti",
}
MEDIUM_RISK_COUNTRIES: set[str] = {
    "united arab emirates", "uae", "turkey", "nigeria", "pakistan",
    "philippines", "cambodia", "jordan", "malta", "cyprus", "kazakhstan",
}


def country_risk_score(country: str) -> int:
    """Return a 0–100 country-risk score for a residence/counterparty country."""
    c = (country or "").strip().lower()
    if not c:
        return 20  # unknown country is mildly risky
    if any(b in c for b in BLOCKED_JURISDICTIONS):
        return 100
    if any(h in c for h in HIGH_RISK_COUNTRIES):
        return 70
    if any(m in c for m in MEDIUM_RISK_COUNTRIES):
        return 40
    return 10


def country_risk_tier(country: str) -> str:
    s = country_risk_score(country)
    return ("BLOCKED" if s >= 100 else "HIGH" if s >= 70
            else "MEDIUM" if s >= 40 else "LOW")


# --------------------------------------------------------------------------- #
# Politically Exposed Persons (name → role). Fictional.
# --------------------------------------------------------------------------- #
PEP_LIST: dict[str, str] = {
    "maria gonzalez": "former deputy finance minister",
    "james okoro": "state-owned enterprise director",
    "viktor petrov": "regional governor",
    "olena kravets": "central bank deputy chair",
    "samuel adeyemi": "minister of energy",
    "dmitry sokolov": "senior defence official",
    "fatima al-mansoori": "sovereign wealth fund director",
}

# --------------------------------------------------------------------------- #
# Sanctions entries — fictional but OFAC/EU-shaped. `name` is fuzzy-matched.
# --------------------------------------------------------------------------- #
SANCTIONS_ENTRIES: list[dict[str, str]] = [
    {"name": "Viktor Petrov", "list_name": "OFAC SDN", "program": "RUSSIA-EO14024",
     "type": "individual", "country": "Russia"},
    {"name": "Viktor Petroff", "list_name": "EU Consolidated", "program": "RUSSIA",
     "type": "individual", "country": "Russia"},
    {"name": "Zahir Al-Rashid", "list_name": "OFAC SDN", "program": "SDGT",
     "type": "individual", "country": "Syria"},
    {"name": "Ivan Kozlov", "list_name": "OFAC SDN", "program": "CYBER2",
     "type": "individual", "country": "Russia"},
    {"name": "Dmitry Sokolov", "list_name": "OFAC SDN", "program": "RUSSIA-EO14024",
     "type": "individual", "country": "Russia"},
    {"name": "Ali Hosseini", "list_name": "OFAC SDN", "program": "IRAN-EO13224",
     "type": "individual", "country": "Iran"},
    {"name": "Kim Yong Nam", "list_name": "UN Consolidated", "program": "DPRK",
     "type": "individual", "country": "North Korea"},
    {"name": "Chen Wei Global Trading Co", "list_name": "EU Consolidated",
     "program": "DPRK", "type": "entity", "country": "North Korea"},
    {"name": "Global Horizon Shipping LLC", "list_name": "OFAC SDN",
     "program": "IRAN-EO13846", "type": "entity", "country": "Iran"},
    {"name": "Aurora Trade Finance Ltd", "list_name": "OFAC SDN", "program": "RUSSIA",
     "type": "entity", "country": "Cyprus"},
]

# Counterparties that are themselves red flags (sanctioned/shell/high-risk).
HIGH_RISK_COUNTERPARTIES: set[str] = {
    "quicksilver exchange", "offshore holdings ltd", "anonymous wallet",
    "cash intensive co", "shell corp bvi", "aurora trade finance ltd",
    "global horizon shipping llc", "unknown wallet", "mixer service",
}

# The cash/transaction reporting threshold structuring tries to stay under (USD CTR).
STRUCTURING_THRESHOLD: float = 10_000.0


# --------------------------------------------------------------------------- #
# Industry / occupation risk (customer-risk driver). Scores 0–100. These are the
# sectors AML programs treat as higher risk (cash-intensive, opaque ownership,
# sanctions/fraud exposure).
# --------------------------------------------------------------------------- #
INDUSTRY_RISK: dict[str, int] = {
    "arms & defense": 85, "weapons trading": 85,
    "cryptocurrency / digital assets": 70, "gambling & casinos": 70,
    "money services (msb)": 70, "adult entertainment": 65,
    "shell / holding company": 65, "precious metals & jewellery": 60,
    "art & antiques": 60, "cash-intensive retail": 55,
    "real estate": 50, "import / export & trade": 50, "construction": 50,
    "charities / non-profit": 50, "legal services": 40, "accounting & audit": 40,
    "hospitality": 40, "automotive": 40, "public sector": 20, "standard retail": 15,
    "manufacturing": 10, "technology": 10, "education": 10, "healthcare": 10,
    "agriculture": 10, "salaried employment": 10,
}


def industry_risk_score(industry: str) -> int:
    """0–100 risk for a customer's industry/occupation ('' = not provided → 0)."""
    if not industry:
        return 0
    key = industry.strip().lower()
    if key in INDUSTRY_RISK:
        return INDUSTRY_RISK[key]
    for name, score in INDUSTRY_RISK.items():
        if name in key or key in name:
            return score
    return 20  # unknown-but-provided industry is mildly risky
