"""Tool registry with least-privilege access control.

Agents don't call data sources directly — they go through this registry, which
enforces *which role may call which tool*. That's "secure tool calling" and
"least privilege" at the tool boundary: the Risk agent literally cannot invoke
the sanctions-screening tool, because its role isn't on the allow-list.

The registry lives in the tool layer, but it is also a security control: it is
where per-agent tool permissions are enforced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolAccessDenied(PermissionError):
    """Raised when a role invokes a tool it is not permitted to use."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    func: Callable[..., Any]
    allowed_roles: frozenset[str]
    description: str


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def call(self, name: str, caller_role: str, /, **kwargs: Any) -> Any:
        """Invoke a tool on behalf of ``caller_role``, enforcing the allow-list."""
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name}")
        if caller_role not in spec.allowed_roles:
            raise ToolAccessDenied(
                f"role '{caller_role}' may not call tool '{name}' "
                f"(allowed: {sorted(spec.allowed_roles)})"
            )
        return spec.func(**kwargs)

    def tools_for(self, role: str) -> list[str]:
        return sorted(n for n, s in self._tools.items() if role in s.allowed_roles)
