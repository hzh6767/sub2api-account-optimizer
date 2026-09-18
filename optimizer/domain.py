from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class RoundTimeoutError(TimeoutError):
    """Raised by the process-wide round deadline and never treated as an API error."""


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    group_ids: tuple[int, ...]
    platform: str
    concurrency: int
    priority: int
    load_factor: int | None
    schedulable: bool
    status: str
    deleted: bool = False

    @property
    def effective_load_factor(self) -> int:
        if self.load_factor is not None and self.load_factor > 0:
            return self.load_factor
        return max(1, self.concurrency)


@dataclass(frozen=True)
class Sample:
    account_id: int
    group_id: int
    model: str
    source: str
    occurred_at: datetime
    success: bool
    ttft_ms: int | None = None
    error_category: str | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class SourceMetric:
    source: str
    samples: int
    successes: int
    failures: int
    timeouts: int
    p50_ttft_ms: float | None
    p90_ttft_ms: float | None
    failure_rate: float
    timeout_rate: float
    score_ms: float | None


@dataclass(frozen=True)
class ModelMetric:
    model: str
    real: SourceMetric | None
    probe: SourceMetric | None
    sample_count: int
    success_count: int
    combined_score_ms: float | None
    normalized_score_ms: float | None = None


@dataclass(frozen=True)
class RankingRow:
    group_id: int
    account_id: int
    account_name: str
    rank: int
    sample_count: int
    success_count: int
    score_ms: float | None
    insufficient_samples: bool
    ranking_eligible: bool
    ranking_basis_rank: int | None
    target_tier: int
    target_priority: int
    target_load_factor: int
    model_metrics: dict[str, ModelMetric]
    schedulable: bool


@dataclass
class UpdateState:
    candidate_tier: int | None = None
    candidate_count: int = 0
    last_score_ms: float | None = None
    last_updated_at: datetime | None = None


@dataclass(frozen=True)
class UpdateDecision:
    allowed: bool
    priority: int | None
    load_factor: int | None
    reason: str


@dataclass(frozen=True)
class ProbeOutcome:
    success: bool
    error_category: str | None = None
    status_code: int | None = None
    ttft_ms: int | None = None
    duration_ms: int | None = None
    reset_at: datetime | None = None
    model: str | None = None
    capability_verified: bool = False


@dataclass
class HealthState:
    auto_disabled: bool = False
    consecutive_failures: int = 0
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    credential_failures: int = 0
    last_credential_failure_at: datetime | None = None
    recovery_successes: int = 0
    probation_active: bool = False
    probation_successes: int = 0
    last_probe_at: datetime | None = None
    last_probe_success: bool | None = None
    last_error_category: str | None = None
    rate_limited_until: datetime | None = None
    stable_since: datetime | None = None
    valid_real_60m: int = 0
    valid_real_24h: int = 0


@dataclass(frozen=True)
class HealthDecision:
    action: str | None = None
    reason: str = ""
    needs_credential_check: bool = False


def dataclass_dict(value: Any) -> Any:
    """Convert nested dataclasses to JSON-compatible values without secrets."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): dataclass_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [dataclass_dict(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
