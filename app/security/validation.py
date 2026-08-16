"""Input validation at the A2A boundary.

Cheap, hard limits that stop a malformed or abusive message before it reaches an
executor: too many parts, oversized text. (Body-size and rate limits are
enforced one layer out, in HTTP middleware.) The customer profile itself is
validated by Pydantic in ``app/agents/schemas.py`` — this is the envelope check.

Raises ``A2AError(invalid_params)`` so the server returns a clean JSON-RPC error.
"""

from __future__ import annotations

from app.a2a.errors import invalid_params
from app.a2a.types import Message


def make_message_validator(*, max_parts: int, max_text_chars: int):
    def validate(message: Message) -> None:
        if len(message.parts) > max_parts:
            raise invalid_params(
                f"too many parts: {len(message.parts)} > {max_parts}")
        for part in message.parts:
            if part.text is not None and len(part.text) > max_text_chars:
                raise invalid_params(
                    f"text part too long: {len(part.text)} > {max_text_chars} chars")
    return validate
