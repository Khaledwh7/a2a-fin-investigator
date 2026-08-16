"""Signed Agent Cards — rogue-agent protection (an A2A v1.0 feature).

v1.0 lets an agent attach a cryptographic signature to its Agent Card so a peer
can verify the card was issued by a key it trusts, not a look-alike. We implement
a detached JWS-style signature (HMAC-SHA256) over the card:

    signing_input = base64url(protected_header) + "." + base64url(canonical_card)
    signature     = HMAC-SHA256(secret, signing_input)

The card's own ``signatures`` field is excluded from the signed payload. On
discovery, a client configured with ``require_signed_agent_cards`` verifies the
signature and refuses any card that is unsigned or fails — combined with the peer
allowlist (we only ever call configured URLs), that's our rogue-agent defense.

Note: we canonicalize with deterministic ``json.dumps(sort_keys=True)`` — a
pragmatic stand-in for RFC 8785 JSON Canonicalization, which the real spec uses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from app.a2a.types import AgentCard, AgentCardSignature


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _canonical_payload(card: AgentCard) -> bytes:
    body = card.to_wire()
    body.pop("signatures", None)  # never sign over the signatures themselves
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _signature(secret: str, protected: str, payload: bytes) -> str:
    signing_input = f"{protected}.".encode() + _b64url(payload).encode()
    mac = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return _b64url(mac)


def sign_card(card: AgentCard, *, secret: str, key_id: str) -> AgentCard:
    """Return a copy of ``card`` with a JWS signature attached."""
    protected = _b64url(json.dumps(
        {"alg": "HS256", "typ": "JWS", "kid": key_id}, sort_keys=True).encode())
    sig = _signature(secret, protected, _canonical_payload(card))
    signed = card.model_copy(deep=True)
    signed.signatures = [AgentCardSignature(protected=protected, signature=sig,
                                            header={"kid": key_id})]
    return signed


def verify_card(card: AgentCard, *, secret: str) -> bool:
    """True iff the card carries a valid signature under ``secret``."""
    if not card.signatures:
        return False
    payload = _canonical_payload(card)
    for sig in card.signatures:
        expected = _signature(secret, sig.protected, payload)
        if hmac.compare_digest(expected, sig.signature):
            return True
    return False
