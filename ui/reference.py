"""Reference data for the intake form.

The dropdown vocabularies an analyst picks from, kept apart from the widgets
that render them so the form file stays about layout and this file stays about
domain content. Several lists are ordered deliberately (higher-risk options
visible rather than buried) because what the analyst can choose is what the
engine can score.
"""

from __future__ import annotations

# Transaction ledger columns — must match the AML engine's Transaction schema.
LEDGER_COLS = ["date", "amount", "direction", "counterparty", "channel", "country"]

# Values the ledger grid accepts; anything else is normalised at the boundary.
DIRECTIONS = {"in", "out"}
CHANNELS_ALLOWED = {"wire", "cash", "card", "crypto", "transfer", "cheque"}

COUNTRIES = [
    "United Kingdom", "United States", "Germany", "France", "Spain", "Italy",
    "Ireland", "Netherlands", "Canada", "Australia", "United Arab Emirates",
    "Singapore", "India", "Brazil", "South Africa", "Nigeria", "China", "Turkey",
    # --- higher-risk / monitored jurisdictions (flagged by the engine) ---
    "Russia", "Panama", "Cayman Islands", "Belarus", "Venezuela", "Myanmar",
    "Afghanistan", "Yemen", "Iran", "North Korea", "Syria", "Cuba",
    "Other (not listed)",
]

SOURCE_OF_FUNDS = ["Salary / employment", "Business income", "Investments",
                   "Consulting fees", "Trading", "Inheritance",
                   "Sale of property", "Pension", "Other"]

SOURCE_OF_WEALTH = ["Employment income", "Business ownership", "Investments",
                    "Inheritance", "Property", "Family wealth", "Other",
                    "Not provided"]

DOC_TYPES = ["Passport", "National ID card", "Driver's licence", "Residence permit"]

EMPLOYMENT_STATUS = ["Employed", "Self-employed", "Business owner", "Retired",
                     "Student", "Unemployed"]

ACCOUNT_PURPOSE = ["Personal banking", "Salary & savings", "Business payments",
                   "Investments", "International transfers", "Trading", "Other"]

# Industries, ordered so the higher-risk ones are visible (they raise the score).
INDUSTRIES = ["Salaried employment", "Technology", "Education", "Healthcare",
              "Manufacturing", "Agriculture", "Standard retail", "Public sector",
              "Hospitality", "Automotive", "Legal services", "Accounting & audit",
              "Construction", "Real estate", "Import / export & trade",
              "Charities / non-profit", "Cash-intensive retail",
              "Precious metals & jewellery", "Art & antiques",
              "Shell / holding company", "Money services (MSB)",
              "Cryptocurrency / digital assets", "Gambling & casinos",
              "Adult entertainment", "Arms & defense", "Not provided"]

CHANNELS = {"In person (branch)": "in_person", "Remote / online": "remote"}

# Observed-behaviour options → the keywords the AML engine understands. This is
# how the analyst's REAL input drives detection — no hidden magic words to type.
BEHAVIOURS: dict[str, str] = {
    "Frequent cash deposits just under reporting thresholds":
        "cash deposits just under threshold structuring",
    "Rapid in-and-out transfers (pass-through / layering)":
        "rapid layering pass-through",
    "Dealings with offshore or shell companies":
        "offshore shell company",
    "Transfers to / from high-risk jurisdictions":
        "high-risk jurisdiction",
    "Cryptocurrency / anonymous-wallet activity":
        "crypto anonymous wallet",
    "Unusually large transactions vs. stated income":
        "large transactions",
}
