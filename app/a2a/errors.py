"""A2A error codes — OFFICIAL to the A2A v1.0 spec (§ error handling).

A2A rides on JSON-RPC 2.0, so it reuses the standard JSON-RPC error range and
adds its own codes in the -32001 … -32009 block. These numbers are normative;
we did not invent them. Source: the A2A specification's error table.
"""

from __future__ import annotations


class JSONRPCErrorCode:
    """Standard JSON-RPC 2.0 codes (not A2A-specific)."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class A2AErrorCode:
    """A2A-specific codes. Exact numbers from the v1.0 spec."""

    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004
    CONTENT_TYPE_NOT_SUPPORTED = -32005
    INVALID_AGENT_RESPONSE = -32006
    EXTENDED_AGENT_CARD_NOT_CONFIGURED = -32007
    EXTENSION_SUPPORT_REQUIRED = -32008
    VERSION_NOT_SUPPORTED = -32009


class TransportAuthError(Exception):
    """A transport-level auth failure → maps to an HTTP status, not a JSON-RPC error.

    A2A treats authentication as an HTTP concern (the Agent Card declares
    ``securitySchemes``; a missing/invalid credential is a 401, an insufficient
    one a 403). This exception is defined in the protocol layer so the server
    can map it without importing the security layer — the security layer raises
    it, keeping the dependency arrow pointing the right way.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class A2AError(Exception):
    """An error that serializes to a JSON-RPC ``error`` object.

    Raise these anywhere in an agent; the server turns them into a proper
    JSON-RPC error response.
    """

    def __init__(self, code: int, message: str, data: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_jsonrpc(self) -> dict:
        err: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# --- convenience constructors (read like English at the call site) ----------

def task_not_found(task_id: str) -> A2AError:
    return A2AError(A2AErrorCode.TASK_NOT_FOUND, f"Task not found: {task_id}")


def task_not_cancelable(task_id: str) -> A2AError:
    return A2AError(
        A2AErrorCode.TASK_NOT_CANCELABLE,
        f"Task is in a terminal state and cannot be canceled: {task_id}",
    )


def unsupported_operation(method: str) -> A2AError:
    return A2AError(A2AErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {method}")


def content_type_not_supported(detail: str) -> A2AError:
    return A2AError(A2AErrorCode.CONTENT_TYPE_NOT_SUPPORTED, detail)


def invalid_agent_response(detail: str) -> A2AError:
    return A2AError(A2AErrorCode.INVALID_AGENT_RESPONSE, detail)


def version_not_supported(requested: str, supported: str) -> A2AError:
    return A2AError(
        A2AErrorCode.VERSION_NOT_SUPPORTED,
        f"A2A version {requested!r} not supported; this agent speaks {supported!r}",
        data={"requested": requested, "supported": supported},
    )


def method_not_found(method: str) -> A2AError:
    return A2AError(JSONRPCErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")


def invalid_params(detail: str) -> A2AError:
    return A2AError(JSONRPCErrorCode.INVALID_PARAMS, detail)


def internal_error(detail: str) -> A2AError:
    return A2AError(JSONRPCErrorCode.INTERNAL_ERROR, detail)
