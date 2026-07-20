from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import RoundTimeoutError


@dataclass(frozen=True)
class Mutation:
    account_id: int
    kind: str
    values: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class MutationResult:
    mutation: Mutation
    status: str
    detail: str


class MutationOutcomeUnknownError(RoundTimeoutError):
    def __init__(self, mutation: Mutation) -> None:
        super().__init__("round timeout left the admin API mutation outcome unknown")
        self.mutation = mutation


class MutationAPI(Protocol):
    def apply(self, mutation: Mutation) -> None: ...


class MutationExecutor:
    """The single write choke point used by apply and rollback commands."""

    def __init__(self, api: MutationAPI, *, apply_enabled: bool) -> None:
        self.api = api
        self.apply_enabled = apply_enabled

    def execute(
        self, mutations: list[Mutation], *, dry_run: bool
    ) -> list[MutationResult]:
        return list(self.iter_execute(mutations, dry_run=dry_run))

    def iter_execute(
        self, mutations: list[Mutation], *, dry_run: bool
    ) -> Iterator[MutationResult]:
        failed_accounts: set[int] = set()
        for mutation in mutations:
            if dry_run:
                yield MutationResult(
                    mutation, "dry-run", "API mutation intentionally skipped"
                )
                continue
            if not self.apply_enabled:
                yield MutationResult(
                    mutation, "blocked", "OPTIMIZER_APPLY_ENABLED is false"
                )
                continue
            if mutation.account_id in failed_accounts:
                yield MutationResult(
                    mutation,
                    "blocked",
                    "a prior mutation for this account failed",
                )
                continue
            try:
                self.api.apply(mutation)
            except RoundTimeoutError as exc:
                raise MutationOutcomeUnknownError(mutation) from exc
            except Exception as exc:
                failed_accounts.add(mutation.account_id)
                outcome_unknown = isinstance(exc, TimeoutError) or getattr(
                    exc, "category", None
                ) == "timeout"
                yield MutationResult(
                    mutation,
                    "unknown" if outcome_unknown else "failed",
                    (
                        "admin API timeout left mutation outcome unknown"
                        if outcome_unknown
                        else f"admin API mutation failed: {type(exc).__name__}"
                    ),
                )
                continue
            yield MutationResult(
                mutation, "applied", "official admin API accepted mutation"
            )
