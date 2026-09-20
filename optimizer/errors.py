from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .domain import RoundTimeoutError


class RedactedError(RuntimeError):
    def __init__(self, error_type: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.outcome_unknown = outcome_unknown


@contextmanager
def redacted_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        if isinstance(exc, (RedactedError, RoundTimeoutError)):
            raise
        raise RedactedError(
            type(exc).__name__,
            outcome_unknown=isinstance(exc, TimeoutError)
            or getattr(exc, "category", None) == "timeout",
        ) from None
