"""Versioned machine-readable protocol shared by the CLI and plugin launcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4


PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class ProtocolEnvelope:
    protocol_version: int
    operation_id: str
    status: str
    sync_state: str
    event_set_digest: str | None
    generation: str | None
    result: Any | None
    code: str | None = None
    message: str | None = None
    attempts: tuple[str, ...] = ()

    @classmethod
    def complete(
        cls, operation_id: str, result: object, *, sync_state: str = "unchanged",
        event_set_digest: str | None = None, generation: str | None = None,
    ) -> "ProtocolEnvelope":
        return cls(PROTOCOL_VERSION, operation_id, "complete", sync_state,
                   event_set_digest, generation, result)

    @classmethod
    def failure(
        cls, operation_id: str, status: str, code: str, message: str,
        attempts: tuple[str, ...] = (), *, sync_state: str = "unchanged",
        event_set_digest: str | None = None, generation: str | None = None,
    ) -> "ProtocolEnvelope":
        return cls(PROTOCOL_VERSION, operation_id, status, sync_state,
                   event_set_digest, generation, None, code, message, attempts)


def operation_id() -> str:
    return "op-" + uuid4().hex
