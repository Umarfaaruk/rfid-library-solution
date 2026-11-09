"""Domain errors carry a stable machine code so gates, kiosks and protocol adapters can react."""

from __future__ import annotations


class DomainError(Exception):
    status = 400

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if status is not None:
            self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class NotFound(DomainError):
    status = 404


class Forbidden(DomainError):
    status = 403


class DeviceFault(DomainError):
    status = 502
