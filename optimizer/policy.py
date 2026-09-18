from __future__ import annotations

import copy
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .domain import (
    Account,
    HealthDecision,
    HealthState,
    ModelMetric,
    ProbeOutcome,
    RankingRow,
    Sample,
    SourceMetric,
    UpdateDecision,
    UpdateState,
)


TTFT_CAP_MS = 60_000
MIN_VALID_SAMPLES = 3
TIER_LOAD_FACTORS = {1: 15, 2: 12, 3: 8, 4: 5}


def partition_accounts(
    accounts: list[Account], target_group_ids: tuple[int, ...]
) -> tuple[dict[int, list[Account]], list[str]]:
    targets = set(target_group_ids)
    grouped: dict[int, list[Account]] = {group_id: [] for group_id in target_group_ids}
    warnings: list[str] = []

    for item in accounts:
        if item.deleted or item.platform.lower() != "openai":
            continue
        memberships = sorted(targets.intersection(item.group_ids))
        for group_id in memberships:
            grouped[group_id].append(item)
        if len(memberships) > 1:
            warnings.append(
                f"account {item.id} belongs to multiple target groups {memberships}; "
                "account-level scheduling fields must have one shared decision"
            )

    for items in grouped.values():
        items.sort(key=lambda item: item.id)
    return grouped, warnings


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(min(TTFT_CAP_MS, max(0, value)) for value in values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _source_metric(source: str, samples: list[Sample]) -> SourceMetric | None:
    if not samples:
        return None
    successes = [
        sample for sample in samples if sample.success and sample.ttft_ms is not None
    ]
    failures = [sample for sample in samples if not sample.success]
    total = len(successes) + len(failures)
    if total == 0:
        return None
    timeouts = sum(1 for sample in failures if sample.error_category == "timeout")
    p50 = _percentile([sample.ttft_ms or 0 for sample in successes], 0.50)
    p90 = _percentile([sample.ttft_ms or 0 for sample in successes], 0.90)
    failure_rate = len(failures) / total
    timeout_rate = timeouts / total
    score = None
    if p50 is not None and p90 is not None:
        score = p50 + 0.25 * p90 + failure_rate * 15_000 + timeout_rate * 20_000
    elif failures:
        score = TTFT_CAP_MS + failure_rate * 15_000 + timeout_rate * 20_000
    return SourceMetric(
        source=source,
        samples=total,
        successes=len(successes),
        failures=len(failures),
        timeouts=timeouts,
        p50_ttft_ms=p50,
        p90_ttft_ms=p90,
        failure_rate=failure_rate,
        timeout_rate=timeout_rate,
        score_ms=score,
    )


def _combine_sources(
    real: SourceMetric | None, probe: SourceMetric | None
) -> float | None:
    weighted: list[tuple[float, float]] = []
    if real is not None and real.score_ms is not None:
        weighted.append((0.70, real.score_ms))
    if probe is not None and probe.score_ms is not None:
        weighted.append((0.30, probe.score_ms))
    if not weighted:
        return None
    total_weight = sum(weight for weight, _ in weighted)
    return sum(weight * score for weight, score in weighted) / total_weight


def _model_metrics(samples: list[Sample]) -> dict[tuple[int, int, str], ModelMetric]:
    buckets: dict[tuple[int, int, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        model = sample.model.strip()
        if not model:
            continue
        buckets[(sample.group_id, sample.account_id, model)].append(sample)

    result: dict[tuple[int, int, str], ModelMetric] = {}
    for key, bucket in buckets.items():
        real = _source_metric(
            "real", [sample for sample in bucket if sample.source == "real"]
        )
        probe = _source_metric(
            "probe", [sample for sample in bucket if sample.source == "probe"]
        )
        sources = [metric for metric in (real, probe) if metric is not None]
        result[key] = ModelMetric(
            model=key[2],
            real=real,
            probe=probe,
            sample_count=sum(metric.samples for metric in sources),
            success_count=sum(metric.successes for metric in sources),
            combined_score_ms=_combine_sources(real, probe),
        )
    return result


def _normalize_models(
    metrics: dict[tuple[int, int, str], ModelMetric],
    group_id: int,
    eligible_ids: set[int],
) -> dict[tuple[int, int, str], ModelMetric]:
    by_model: dict[str, list[float]] = defaultdict(list)
    all_scores: list[float] = []
    for (metric_group, account_id, model), metric in metrics.items():
        if metric_group != group_id or metric.combined_score_ms is None:
            continue
        all_scores.append(metric.combined_score_ms)
        if account_id in eligible_ids:
            by_model[model].append(metric.combined_score_ms)
    baselines = {
        model: statistics.median(values) for model, values in by_model.items() if values
    }
    global_baseline = (
        statistics.median(baselines.values())
        if baselines
        else statistics.median(all_scores)
        if all_scores
        else 1.0
    )

    normalized: dict[tuple[int, int, str], ModelMetric] = {}
    for key, metric in metrics.items():
        if key[0] != group_id or metric.combined_score_ms is None:
            continue
        baseline = max(1.0, baselines.get(metric.model, metric.combined_score_ms))
        normalized[key] = ModelMetric(
            model=metric.model,
            real=metric.real,
            probe=metric.probe,
            sample_count=metric.sample_count,
            success_count=metric.success_count,
            combined_score_ms=metric.combined_score_ms,
            normalized_score_ms=metric.combined_score_ms / baseline * global_baseline,
        )
    return normalized


def _quartile_tier(rank: int, total: int) -> int:
    if total <= 0 or rank <= 0:
        return 3
    if total == 1:
        return 1
    percentile = (rank - 1) / (total - 1)
    if percentile < 0.25:
        return 1
    if percentile < 0.50:
        return 2
    if percentile < 0.75:
        return 3
    return 4


def _target_load_factor(tier: int, concurrency: int) -> int:
    ceiling = max(1, concurrency)
    return max(1, min(TIER_LOAD_FACTORS[tier], ceiling))


def build_rankings(
    accounts: list[Account], samples: list[Sample], target_group_ids: tuple[int, ...]
) -> dict[int, list[RankingRow]]:
    grouped, _ = partition_accounts(accounts, target_group_ids)
    raw_metrics = _model_metrics(samples)
    output: dict[int, list[RankingRow]] = {}

    for group_id in target_group_ids:
        active_ids = {
            item.id
            for item in grouped[group_id]
            if item.schedulable and item.status.lower() == "active"
        }
        normalized = _normalize_models(raw_metrics, group_id, active_ids)
        provisional: list[dict[str, object]] = []
        for item in grouped[group_id]:
            item_metrics = {
                model: metric
                for (metric_group, account_id, model), metric in normalized.items()
                if metric_group == group_id and account_id == item.id
            }
            sample_count = sum(metric.sample_count for metric in item_metrics.values())
            success_count = sum(
                metric.success_count for metric in item_metrics.values()
            )
            weighted_scores: list[tuple[float, float]] = []
            for metric in item_metrics.values():
                if metric.normalized_score_ms is None:
                    continue
                confidence = min(10.0, math.sqrt(max(1, metric.sample_count)))
                weighted_scores.append((confidence, metric.normalized_score_ms))
            score = None
            if weighted_scores:
                score = sum(weight * value for weight, value in weighted_scores) / sum(
                    weight for weight, _ in weighted_scores
                )
            provisional.append(
                {
                    "account": item,
                    "metrics": item_metrics,
                    "sample_count": sample_count,
                    "success_count": success_count,
                    "score": score,
                    "insufficient": success_count < MIN_VALID_SAMPLES,
                    "active": item.id in active_ids,
                }
            )

        provisional.sort(
            key=lambda row: (
                not bool(row["active"]),
                bool(row["insufficient"]),
                row["score"] is None,
                float(row["score"]) if row["score"] is not None else math.inf,
                int(row["account"].id),  # type: ignore[union-attr]
            )
        )
        eligible_rows = [
            row
            for row in provisional
            if bool(row["active"]) and not bool(row["insufficient"])
        ]
        eligible_rank_by_id = {
            row["account"].id: rank  # type: ignore[union-attr]
            for rank, row in enumerate(eligible_rows, start=1)
        }
        rows: list[RankingRow] = []
        for rank, row in enumerate(provisional, start=1):
            item = row["account"]
            assert isinstance(item, Account)
            insufficient = bool(row["insufficient"])
            ranking_eligible = bool(row["active"]) and not insufficient
            basis_rank = eligible_rank_by_id.get(item.id)
            tier = (
                _quartile_tier(basis_rank, len(eligible_rows))
                if ranking_eligible and basis_rank is not None
                else 3
            )
            rows.append(
                RankingRow(
                    group_id=group_id,
                    account_id=item.id,
                    account_name=item.name,
                    rank=rank,
                    sample_count=int(row["sample_count"]),
                    success_count=int(row["success_count"]),
                    score_ms=float(row["score"]) if row["score"] is not None else None,
                    insufficient_samples=insufficient,
                    ranking_eligible=ranking_eligible,
                    ranking_basis_rank=basis_rank,
                    target_tier=tier,
                    target_priority=tier,
                    target_load_factor=_target_load_factor(tier, item.concurrency),
                    model_metrics=row["metrics"],  # type: ignore[arg-type]
                    schedulable=item.schedulable,
                )
            )
        output[group_id] = rows
    return output


def evaluate_update(
    account: Account,
    target_tier: int,
    score_ms: float | None,
    state: UpdateState,
    now: datetime,
) -> UpdateDecision:
    if state.candidate_tier != target_tier or state.candidate_count < 2:
        return UpdateDecision(
            False, None, None, "ranking has not held for two consecutive rounds"
        )
    if state.last_updated_at is not None and now - state.last_updated_at < timedelta(
        hours=6
    ):
        return UpdateDecision(False, None, None, "six-hour update cooldown is active")
    if (
        state.last_score_ms is not None
        and score_ms is not None
        and state.last_score_ms > 0
    ):
        relative_change = abs(score_ms - state.last_score_ms) / state.last_score_ms
        if relative_change < 0.15:
            return UpdateDecision(
                False, None, None, "score change is below the 15% threshold"
            )

    current_tier = min(4, max(1, account.priority))
    if target_tier < current_tier:
        next_tier = current_tier - 1
    elif target_tier > current_tier:
        next_tier = current_tier + 1
    else:
        next_tier = current_tier
    load_factor = _target_load_factor(next_tier, account.concurrency)
    if account.priority == next_tier and account.effective_load_factor == load_factor:
        return UpdateDecision(
            False, None, None, "account is already in the requested scheduling band"
        )
    return UpdateDecision(
        True, next_tier, load_factor, "stable ranking permits one-band update"
    )


def apply_probe_outcome(
    account: Account,
    state: HealthState,
    outcome: ProbeOutcome,
    occurred_at: datetime,
) -> tuple[HealthState, HealthDecision]:
    updated = copy.deepcopy(state)
    updated.last_probe_at = occurred_at
    updated.last_probe_success = outcome.success
    updated.last_error_category = outcome.error_category

    if outcome.success:
        updated.consecutive_failures = 0
        updated.first_failure_at = None
        updated.last_failure_at = None
        updated.credential_failures = 0
        updated.last_credential_failure_at = None
        updated.rate_limited_until = None
        updated.stable_since = updated.stable_since or occurred_at
        if not account.schedulable and updated.auto_disabled:
            updated.recovery_successes += 1
            if updated.recovery_successes >= 2:
                return updated, HealthDecision(
                    "enable", "two recovery probes succeeded"
                )
        elif updated.probation_active:
            updated.recovery_successes = 0
            updated.probation_successes += 1
            if updated.probation_successes >= 2:
                updated.probation_active = False
        else:
            updated.recovery_successes = 0
            if account.schedulable and updated.auto_disabled:
                updated.auto_disabled = False
        return updated, HealthDecision()

    updated.stable_since = None
    updated.recovery_successes = 0
    category = (outcome.error_category or "unknown").lower()
    if category in {"probe_configuration", "routing_mismatch"}:
        updated.last_probe_success = None
        updated.consecutive_failures = 0
        updated.first_failure_at = None
        updated.last_failure_at = None
        updated.credential_failures = 0
        updated.last_credential_failure_at = None
        return updated, HealthDecision(
            reason="probe configuration error does not affect account health"
        )
    if category == "rate_limit" or outcome.status_code == 429:
        updated.consecutive_failures = 0
        updated.first_failure_at = None
        updated.last_failure_at = None
        updated.credential_failures = 0
        updated.last_credential_failure_at = None
        updated.rate_limited_until = outcome.reset_at
        return updated, HealthDecision(
            reason="429 is temporary and never permanently disables"
        )

    if category == "auth" or outcome.status_code in (401, 403):
        if (
            updated.last_credential_failure_at is None
            or occurred_at - updated.last_credential_failure_at >= timedelta(minutes=5)
        ):
            updated.credential_failures += 1
            updated.last_credential_failure_at = occurred_at
        if updated.credential_failures >= 2:
            return updated, HealthDecision(
                "disable",
                "two credential failures confirmed at least five minutes apart",
                needs_credential_check=True,
            )
        return updated, HealthDecision(reason="credential failure awaits confirmation")

    updated.credential_failures = 0
    updated.last_credential_failure_at = None
    if updated.consecutive_failures == 0:
        updated.first_failure_at = occurred_at
    updated.consecutive_failures += 1
    updated.last_failure_at = occurred_at
    if (
        updated.consecutive_failures >= 3
        and updated.first_failure_at is not None
        and occurred_at - updated.first_failure_at >= timedelta(minutes=30)
    ):
        return updated, HealthDecision(
            "disable", "three failures span at least thirty minutes"
        )
    return updated, HealthDecision(reason="failure threshold not reached")


def should_probe(
    account: Account, state: HealthState, now: datetime
) -> tuple[bool, str]:
    if state.rate_limited_until is not None and now < state.rate_limited_until:
        return False, "waiting for the recorded rate-limit reset time"
    if (
        state.valid_real_60m >= 5
        and not state.auto_disabled
        and not state.probation_active
        and state.last_probe_success is not False
    ):
        return False, "at least five valid real traffic samples exist in the last hour"

    interval = timedelta(hours=1)
    reason = "normal account has insufficient recent real traffic"
    if state.auto_disabled:
        interval = timedelta(minutes=30)
        reason = "optimizer-disabled account recovery probe"
    elif not account.schedulable:
        interval = timedelta(hours=2)
        reason = "manually disabled account low-frequency health probe"
    elif state.last_error_category in {"probe_configuration", "routing_mismatch"}:
        interval = timedelta(hours=2)
        reason = "probe configuration needs low-frequency revalidation"
    elif state.probation_active:
        interval = timedelta(minutes=30)
        reason = "recovered account probation probe"
    elif state.credential_failures > 0:
        interval = timedelta(minutes=5)
        reason = "credential failure confirmation probe"
    elif state.last_probe_success is False:
        interval = timedelta(minutes=15)
        reason = "failed probe retry"
    elif state.valid_real_24h < 3:
        interval = timedelta(minutes=30)
        reason = "new or low-sample account"
    elif (
        state.stable_since is not None
        and now - state.stable_since >= timedelta(hours=24)
        and state.valid_real_24h >= 5
    ):
        interval = timedelta(hours=2)
        reason = "account has been stable for twenty-four hours"

    if state.last_probe_at is None:
        return True, reason
    return now - state.last_probe_at >= interval, reason
