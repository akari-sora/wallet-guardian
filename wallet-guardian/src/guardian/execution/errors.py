"""Errors for the execution registry. A failure never becomes an approval."""

from __future__ import annotations


class ExecutionRegistryError(ValueError):
    """The execution document is not usable. Nothing in it is user-approved."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if detail == "" else f"{code}: {detail}"
        super().__init__(message)
