from __future__ import annotations

import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .api import AdminAPI, TargetedProbeSafetyError
from .config import Config
from .database import DatabaseSnapshot
from .domain import Account, HealthState, Sample, UpdateState, dataclass_dict
from .mutations import (
    Mutation,
    MutationExecutor,
    MutationOutcomeUnknownError,
    MutationResult,
)
from .pricing import ProbeModelSelection, load_model_pricing, select_probe_model
from .policy import (
    apply_probe_outcome,
    build_rankings,
    evaluate_update,
    partition_accounts,
    should_probe,
)
from .storage import JsonStore


class SnapshotDatabase(Protocol):
    def snapshot(self, *, read_only: bool = True) -> DatabaseSnapshot: ...


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _health_state(
    record: dict[str, Any], real_samples: list[Sample], now: datetime
) -> HealthState:
    valid = [
        sample
        for sample in real_samples
        if sample.success and sample.ttft_ms is not None
    ]
    return HealthState(
        auto_disabled=bool(record.get("auto_disabled", False)),
        consecutive_failures=int(record.get("consecutive_failures", 0)),
        first_failure_at=_parse_datetime(record.get("first_failure_at")),
        last_failure_at=_parse_datetime(record.get("last_failure_at")),
        credential_failures=int(record.get("credential_failures", 0)),
        last_credential_failure_at=_parse_datetime(
            record.get("last_credential_failure_at")
        ),
        recovery_successes=int(record.get("recovery_successes", 0)),
        probation_active=bool(record.get("probation_active", False)),
        probation_successes=int(record.get("probation_successes", 0)),
        last_probe_at=_parse_datetime(record.get("last_probe_at")),
        last_probe_success=record.get("last_probe_success"),
        last_error_category=record.get("last_error_category"),
        rate_limited_until=_parse_datetime(record.get("rate_limited_until")),
        stable_since=_parse_datetime(record.get("stable_since")),
        valid_real_60m=sum(
            1 for sample in valid if sample.occurred_at >= now - timedelta(hours=1)
        ),
        valid_real_24h=len(valid),
    )


def _health_record(state: HealthState) -> dict[str, Any]:
    return dataclass_dict(state)


def _update_state(record: dict[str, Any]) -> UpdateState:
    return UpdateState(
        candidate_tier=record.get("candidate_tier"),
        candidate_count=int(record.get("candidate_count", 0)),
        last_score_ms=record.get("last_score_ms"),
        last_updated_at=_parse_datetime(record.get("last_updated_at")),
    )


def _load_target(tier: int, concurrency: int) -> int:
    return min({1: 15, 2: 12, 3: 8, 4: 5}[tier], max(1, concurrency))


class OptimizerEngine:
    def __init__(
        self,
        config: Config,
        database: SnapshotDatabase,
        api: AdminAPI,
        store: JsonStore,
        *,
        dependency_checker: Callable[[], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.api = api
        self.store = store
        self.dependency_checker = dependency_checker
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self, *, dry_run: bool) -> dict[str, Any]:
        started_monotonic = time.monotonic()
        now = self.now()
        snapshot = self.database.snapshot(read_only=True)
        advanced_scheduler_enabled = (
            str(
                snapshot.settings.get(
                    "openai_advanced_scheduler_enabled", "false"
                )
            ).lower()
            == "true"
        )
        if not dry_run and not advanced_scheduler_enabled:
            raise RuntimeError(
                "formal optimizer apply requires the advanced scheduler setting "
                "openai_advanced_scheduler_enabled=true"
            )
        state = self.store.load_state()
        state.setdefault("version", 1)
        state.setdefault("accounts", {})
        state.setdefault("probe_history", [])
        self._prune_probe_history(state, now)
        if not dry_run:
            self._reconcile_pending_health(snapshot.accounts, state, now)
            self._reconcile_pending_schedules(snapshot.accounts, state, now)

        baseline = self._baseline_payload(snapshot.accounts, now)
        if dry_run:
            self.store.write_backup_once("deployment-baseline.json", baseline)
        else:
            self._extend_activation_baseline(baseline, now)

        grouped, warnings = partition_accounts(snapshot.accounts, self.config.group_ids)
        duplicates = {
            item.id
            for items in grouped.values()
            for item in items
            if len(set(item.group_ids).intersection(self.config.group_ids)) > 1
        }
        samples = list(snapshot.samples)
        samples.extend(self._probe_samples(state, now))
        model_pricing = load_model_pricing(self.config.model_pricing_file)
        catalog_selection = select_probe_model(
            set(self.config.probe_model_preference),
            self.config.probe_model_preference,
            model_pricing,
            input_tokens=self.config.probe_input_tokens_estimate,
            output_tokens=self.config.probe_output_tokens_estimate,
        )
        if catalog_selection.model is None:
            warnings.append(
                "active probes are blocked because the allowed model price catalog is missing or incomplete"
            )
        mutation_results: list[MutationResult] = []
        probe_results: list[dict[str, Any]] = []
        probe_safety_blocked = False
        capability_record = state.get("targeted_probe_capability", {})
        capability_verified = bool(
            isinstance(capability_record, dict)
            and capability_record.get("verified") is True
        )

        probes_can_run = (
            not dry_run
            and self.config.apply_enabled
            and self.config.active_probes_enabled
            and self.config.targeted_test_safe
        )
        if not dry_run and self.config.active_probes_enabled and not probes_can_run:
            warnings.append(
                "active probes requested but blocked: apply, active-probe, and targeted-test-safe gates must all be true"
            )
        if probes_can_run:
            new_samples, probe_mutations, probe_results = self._run_due_probes(
                snapshot,
                grouped,
                duplicates,
                state,
                now,
                started_monotonic,
                model_pricing,
            )
            samples.extend(new_samples)
            probe_safety_blocked = any(
                bool(result.get("safety_blocked")) for result in probe_results
            )
            capability_record = state.get("targeted_probe_capability", {})
            capability_verified = bool(
                isinstance(capability_record, dict)
                and capability_record.get("verified") is True
                and not probe_safety_blocked
            )
            if probe_safety_blocked:
                warnings.append(
                    "targeted probe capability handshake failed; this apply round performed no account mutations"
                )
            else:
                try:
                    for result in MutationExecutor(
                        self.api, apply_enabled=self.config.apply_enabled
                    ).iter_execute(probe_mutations, dry_run=False):
                        mutation_results.append(result)
                        self._record_health_schedule_mutations(
                            state, [result], snapshot.accounts, now
                        )
                        self._record_health_mutations(state, [result], now)
                        self.store.write_state(state)
                except MutationOutcomeUnknownError as exc:
                    result = self._unknown_mutation_result(exc)
                    mutation_results.append(result)
                    self._record_health_schedule_mutations(
                        state, [result], snapshot.accounts, now
                    )
                    self._record_health_mutations(state, [result], now)
                    self.store.write_state(state)
                    raise

        rankings = build_rankings(snapshot.accounts, samples, self.config.group_ids)
        ranking_mutations: list[Mutation] = []
        ranking_due = self._ranking_due(state, now)
        if not dry_run and ranking_due and not probe_safety_blocked:
            ranking_mutations = self._ranking_mutations(
                snapshot.accounts, rankings, duplicates, state, now
            )
            state["last_ranking_at"] = now.isoformat()
            self.store.write_state(state)
            try:
                for result in MutationExecutor(
                    self.api, apply_enabled=self.config.apply_enabled
                ).iter_execute(ranking_mutations, dry_run=False):
                    mutation_results.append(result)
                    self._record_ranking_mutations(state, [result], rankings, now)
                    self.store.write_state(state)
            except MutationOutcomeUnknownError as exc:
                result = self._unknown_mutation_result(exc)
                mutation_results.append(result)
                self._record_ranking_mutations(state, [result], rankings, now)
                self.store.write_state(state)
                raise
        proposals = self._proposals(snapshot.accounts, rankings, duplicates, dry_run)
        probe_plan, probe_estimate = self._probe_plan(
            snapshot, grouped, duplicates, state, now, dry_run, model_pricing
        )
        warnings.extend(self._fixed_warnings(snapshot, dry_run))
        dependencies = (
            self.dependency_checker() if self.dependency_checker is not None else {}
        )
        report = {
            "schema_version": 1,
            "mode": "dry-run" if dry_run else "apply",
            "generated_at": now,
            "duration_ms": round((time.monotonic() - started_monotonic) * 1000),
            "target_groups": list(self.config.group_ids),
            "safeguards": {
                "database_transaction_read_only": True,
                "zero_api_mutations": not mutation_results
                and not any(
                    result.get("error_category") == "rate_limit"
                    or result.get("status_code") == 429
                    for result in probe_results
                ),
                "zero_scheduling_mutations": not mutation_results,
                "temporary_rate_limit_observations": sum(
                    1
                    for result in probe_results
                    if result.get("error_category") == "rate_limit"
                    or result.get("status_code") == 429
                ),
                "active_probes_executed": bool(probe_results),
                "apply_enabled": self.config.apply_enabled,
                "active_probes_enabled": self.config.active_probes_enabled,
                "targeted_test_safe": self.config.targeted_test_safe,
                "timer_enabled": False if dry_run else None,
                "advanced_scheduler_required": True,
                "targeted_probe_capability_verified": capability_verified,
                "targeted_probe_verification": capability_record
                if isinstance(capability_record, dict)
                else {},
            },
            "telemetry": {
                "real_success_samples_24h": sum(
                    1 for sample in snapshot.samples if sample.success
                ),
                "real_failure_samples_24h": sum(
                    1 for sample in snapshot.samples if not sample.success
                ),
                "probe_samples_loaded": len(samples) - len(snapshot.samples),
                "tool_call_filter_exact": False,
                "ttft_cap_ms": 60_000,
            },
            "scheduler": {
                "supported_by_running_version": True,
                "runtime_weight_update_api_supported": False,
                "openai_advanced_scheduler_enabled": advanced_scheduler_enabled,
                "runtime_weights": None,
                "runtime_weights_verified": False,
                "runtime_weight_verification": (
                    "Sub2API 0.2.5 does not expose scheduler weights through "
                    "the administrator API; verify the running container environment"
                ),
                "declared_deployment_weights": {
                    "priority": 0.6,
                    "load": 1.0,
                    "queue": 0.8,
                    "error_rate": 1.5,
                    "ttft": 1.2,
                    "reset": 0.0,
                    "quota_headroom": 0.0,
                },
                "recommended_weights": {
                    "priority": 0.6,
                    "load": 1.0,
                    "queue": 0.8,
                    "error_rate": 1.5,
                    "ttft": 1.2,
                    "reset": 0.0,
                    "quota_headroom": 0.0,
                },
                "declared_sticky_layers": {
                    "previous_response_id": True,
                    "session_hash": True,
                    "sticky_response_id_ttl_seconds": 3600,
                    "sticky_session_ttl_seconds": 3600,
                    "sticky_escape_enabled": True,
                    "sticky_escape_ttft_ms": 15000,
                    "sticky_escape_error_rate": 0.5,
                },
                "weights_source": (
                    "declared in sub2api-optimizer.override.yml; runtime values are "
                    "verified externally during deployment"
                ),
                "optimizer_action": (
                    "formal apply is blocked until the advanced scheduler is enabled"
                    if not advanced_scheduler_enabled
                    else "read-only preview; no probes or scheduling writes"
                    if dry_run
                    else "health probing and slow baseline scheduling adjustments; "
                    "Sub2API retains real-time TTFT/error/load scheduling"
                ),
            },
            "scheduled_test_plans_enabled": snapshot.enabled_scheduled_test_plans,
            "groups": snapshot.group_metadata,
            "rankings": {
                str(group_id): [dataclass_dict(row) for row in rows]
                for group_id, rows in rankings.items()
            },
            "proposals": proposals,
            "probe_plan": probe_plan,
            "probe_results": probe_results,
            "estimated_daily_probe_usage": probe_estimate,
            "mutations": [dataclass_dict(result) for result in mutation_results],
            "dependency_health": dependencies,
            "warnings": sorted(set(warnings)),
        }

        self.store.write_latest(report)
        self.store.append_history(report)
        if not dry_run:
            state["updated_at"] = now.isoformat()
            self.store.write_state(state)
        return dataclass_dict(report)

    def _probe_samples(self, state: dict[str, Any], now: datetime) -> list[Sample]:
        history = state.get("probe_history", [])
        buckets: dict[tuple[int, int], list[Sample]] = defaultdict(list)
        if not isinstance(history, list):
            return []
        for item in history:
            if not isinstance(item, dict):
                continue
            occurred_at = _parse_datetime(item.get("occurred_at"))
            if occurred_at is None or occurred_at < now - timedelta(hours=24):
                continue
            try:
                sample = Sample(
                    account_id=int(item["account_id"]),
                    group_id=int(item["group_id"]),
                    model=str(item["model"]),
                    source="probe",
                    occurred_at=occurred_at,
                    success=bool(item["success"]),
                    ttft_ms=int(item["ttft_ms"])
                    if item.get("ttft_ms") is not None
                    else None,
                    error_category=item.get("error_category"),
                    status_code=int(item["status_code"])
                    if item.get("status_code") is not None
                    else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
            buckets[(sample.account_id, sample.group_id)].append(sample)
        result: list[Sample] = []
        for bucket in buckets.values():
            result.extend(
                sorted(bucket, key=lambda sample: sample.occurred_at, reverse=True)[:6]
            )
        return result

    def _baseline_payload(
        self, accounts: list[Account], now: datetime
    ) -> dict[str, Any]:
        return {
            "created_at": now,
            "groups": list(self.config.group_ids),
            "accounts": [
                {
                    "id": item.id,
                    "name": item.name,
                    "group_ids": item.group_ids,
                    "concurrency": item.concurrency,
                    "priority": item.priority,
                    "load_factor": item.load_factor,
                    "schedulable": item.schedulable,
                    "status": item.status,
                }
                for item in accounts
            ],
        }

    def _extend_activation_baseline(
        self, current: dict[str, Any], now: datetime
    ) -> None:
        filename = "activation-baseline.json"
        existing = self.store.load_backup(filename)
        if existing is None:
            self.store.write_backup(filename, current)
            return

        accounts = existing.get("accounts")
        if not isinstance(accounts, list):
            raise ValueError("activation baseline accounts must be a JSON array")
        existing_ids = {
            int(item["id"])
            for item in accounts
            if isinstance(item, dict) and item.get("id") is not None
        }
        additions = [
            item
            for item in current.get("accounts", [])
            if isinstance(item, dict) and int(item["id"]) not in existing_ids
        ]
        if not additions:
            return
        updated = dict(existing)
        updated["accounts"] = sorted(
            [*accounts, *additions], key=lambda item: int(item["id"])
        )
        updated["last_extended_at"] = now
        updated["groups"] = sorted(
            set(existing.get("groups", [])).union(self.config.group_ids)
        )
        self.store.write_backup(filename, updated)

    @staticmethod
    def _prune_probe_history(state: dict[str, Any], now: datetime) -> None:
        history = state.get("probe_history", [])
        if not isinstance(history, list):
            state["probe_history"] = []
            return
        cutoff = now - timedelta(hours=24)
        state["probe_history"] = [
            item
            for item in history
            if isinstance(item, dict)
            and (occurred_at := _parse_datetime(item.get("occurred_at"))) is not None
            and occurred_at >= cutoff
        ][-10_000:]

    @staticmethod
    def _reconcile_pending_health(
        accounts: list[Account], state: dict[str, Any], now: datetime
    ) -> None:
        by_id = {item.id: item for item in accounts}
        for account_id, record in state.get("accounts", {}).items():
            if not isinstance(record, dict):
                continue
            health = record.get("health")
            if not isinstance(health, dict):
                continue
            pending = health.get("pending_schedulable")
            if not isinstance(pending, bool):
                continue
            try:
                account = by_id.get(int(account_id))
            except (TypeError, ValueError):
                account = None
            if account is not None and account.schedulable == pending:
                health["last_state_change_at"] = now.isoformat()
                health["recovery_successes"] = 0
                health["probation_successes"] = 0
                if pending:
                    health["auto_disabled"] = False
                    health["probation_active"] = True
                    health.pop("ownership_uncertain", None)
                else:
                    health["auto_disabled"] = False
                    health["probation_active"] = False
                    health["ownership_uncertain"] = True
            elif not pending:
                health["auto_disabled"] = False
                health.pop("ownership_uncertain", None)
            health.pop("pending_schedulable", None)

    @staticmethod
    def _reconcile_pending_schedules(
        accounts: list[Account], state: dict[str, Any], now: datetime
    ) -> None:
        by_id = {item.id: item for item in accounts}
        for account_id, record in state.get("accounts", {}).items():
            if not isinstance(record, dict):
                continue
            try:
                account = by_id.get(int(account_id))
            except (TypeError, ValueError):
                account = None
            if account is None:
                continue
            groups = record.get("groups")
            if not isinstance(groups, dict):
                continue
            for group in groups.values():
                if not isinstance(group, dict):
                    continue
                pending = group.get("pending_schedule")
                if not isinstance(pending, dict):
                    continue
                values = pending.get("values")
                if not isinstance(values, dict):
                    continue
                priority_matches = values.get("priority") is None or account.priority == int(
                    values["priority"]
                )
                requested_load = values.get("load_factor")
                if requested_load is None:
                    load_matches = True
                elif int(requested_load) == 0:
                    load_matches = account.load_factor is None
                else:
                    load_matches = account.load_factor == int(requested_load)
                group["last_schedule_outcome"] = (
                    "confirmed_applied"
                    if priority_matches and load_matches
                    else "not_observed_after_unknown_response"
                )
                group["last_schedule_reconciled_at"] = now.isoformat()
                group.pop("pending_schedule", None)

    def _run_due_probes(
        self,
        snapshot: DatabaseSnapshot,
        grouped: dict[int, list[Account]],
        duplicates: set[int],
        state: dict[str, Any],
        now: datetime,
        started_monotonic: float,
        model_pricing: dict[str, dict[str, Any]],
    ) -> tuple[list[Sample], list[Mutation], list[dict[str, Any]]]:
        probe_models: dict[tuple[int, int], ProbeModelSelection] = {}
        for group_id, accounts in grouped.items():
            accounts = [item for item in accounts if item.id not in duplicates]
            supported: set[str] | None = None
            supported_by_account: dict[int, set[str]] = {}
            for account in accounts:
                models = self.api.available_models(account.id)
                supported_by_account[account.id] = models
                supported = (
                    models if supported is None else supported.intersection(models)
                )
            common_selection = select_probe_model(
                supported or set(),
                self.config.probe_model_preference,
                model_pricing,
                input_tokens=self.config.probe_input_tokens_estimate,
                output_tokens=self.config.probe_output_tokens_estimate,
            )
            for account in accounts:
                probe_models[(group_id, account.id)] = (
                    common_selection
                    if common_selection.model is not None
                    else select_probe_model(
                        supported_by_account[account.id],
                        self.config.probe_model_preference,
                        model_pricing,
                        input_tokens=self.config.probe_input_tokens_estimate,
                        output_tokens=self.config.probe_output_tokens_estimate,
                    )
                )

        real_by_account: dict[int, list[Sample]] = defaultdict(list)
        for sample in snapshot.samples:
            if sample.source == "real":
                real_by_account[sample.account_id].append(sample)

        samples: list[Sample] = []
        mutations: list[Mutation] = []
        results: list[dict[str, Any]] = []
        probed = 0
        deadline = started_monotonic + self.config.round_timeout_seconds
        for group_id in self.config.group_ids:
            for account in grouped[group_id]:
                if account.id in duplicates:
                    continue
                selection = probe_models.get(
                    (group_id, account.id), ProbeModelSelection(None, None)
                )
                model = selection.model
                if model is None:
                    continue
                account_records = state["accounts"].setdefault(str(account.id), {})
                health = _health_state(
                    account_records.get("health", {}), real_by_account[account.id], now
                )
                due, due_reason = should_probe(account, health, now)
                if not due:
                    continue
                if time.monotonic() >= deadline:
                    return samples, mutations, results
                if probed:
                    time.sleep(random.uniform(3, 5))
                try:
                    outcome = self.api.probe_account(account.id, model)
                except TargetedProbeSafetyError as exc:
                    state["targeted_probe_capability"] = {
                        "verified": False,
                        "checked_at": self.now().isoformat(),
                        "account_id": account.id,
                        "requested_model": model,
                        "error_type": type(exc).__name__,
                    }
                    self.store.write_state(state)
                    results.append(
                        {
                            "account_id": account.id,
                            "account_name": account.name,
                            "group_id": group_id,
                            "model": model,
                            "success": False,
                            "error_category": "targeted_probe_safety",
                            "error_type": type(exc).__name__,
                            "safety_blocked": True,
                        }
                    )
                    return samples, [], results
                occurred_at = self.now()
                actual_model = outcome.model or model
                actual_cost_selection = select_probe_model(
                    {actual_model},
                    (actual_model,),
                    model_pricing,
                    input_tokens=self.config.probe_input_tokens_estimate,
                    output_tokens=self.config.probe_output_tokens_estimate,
                )
                if outcome.capability_verified:
                    state["targeted_probe_capability"] = {
                        "verified": True,
                        "verified_at": occurred_at.isoformat(),
                        "account_id": account.id,
                        "group_id": group_id,
                        "requested_model": model,
                        "actual_model": actual_model,
                    }
                sample = Sample(
                    account.id,
                    group_id,
                    actual_model,
                    "probe",
                    occurred_at,
                    outcome.success,
                    outcome.ttft_ms,
                    outcome.error_category,
                    outcome.status_code,
                )
                if outcome.error_category not in {
                    "probe_configuration",
                    "routing_mismatch",
                }:
                    samples.append(sample)
                    state["probe_history"].append(dataclass_dict(sample))
                updated_health, decision = apply_probe_outcome(
                    account, health, outcome, occurred_at
                )
                health_record = _health_record(updated_health)
                account_records["health"] = health_record
                if decision.action == "disable" and account.schedulable:
                    health_record["pending_schedulable"] = False
                    mutations.append(
                        Mutation(
                            account.id,
                            "schedulable",
                            {"schedulable": False},
                            decision.reason,
                        )
                    )
                elif (
                    decision.action == "enable"
                    and not account.schedulable
                    and health.auto_disabled
                ):
                    health_record["pending_schedulable"] = True
                    mutations.extend(
                        [
                            Mutation(
                                account.id,
                                "schedule",
                                {
                                    "priority": 4,
                                    "load_factor": _load_target(4, account.concurrency),
                                },
                                "recovered account enters the lowest traffic band",
                            ),
                            Mutation(
                                account.id,
                                "schedulable",
                                {"schedulable": True},
                                decision.reason,
                            ),
                        ]
                    )
                self.store.write_state(state)
                results.append(
                    {
                        "account_id": account.id,
                        "account_name": account.name,
                        "group_id": group_id,
                        "requested_model": model,
                        "model": actual_model,
                        "requested_model_estimated_catalog_cost_usd": selection.estimated_cost_usd,
                        "estimated_catalog_cost_usd": actual_cost_selection.estimated_cost_usd,
                        "cost_model": actual_model,
                        "cost_estimate_exact": actual_cost_selection.model is not None,
                        "due_reason": due_reason,
                        "success": outcome.success,
                        "ttft_ms": outcome.ttft_ms,
                        "duration_ms": outcome.duration_ms,
                        "status_code": outcome.status_code,
                        "error_category": outcome.error_category,
                        "rate_limit_reset_at": outcome.reset_at,
                        "occurred_at": occurred_at,
                    }
                )
                probed += 1
        return samples, mutations, results

    @staticmethod
    def _ranking_due(state: dict[str, Any], now: datetime) -> bool:
        last = _parse_datetime(state.get("last_ranking_at"))
        return last is None or now - last >= timedelta(hours=1)

    def _ranking_mutations(
        self,
        accounts: list[Account],
        rankings: dict[int, list[Any]],
        duplicates: set[int],
        state: dict[str, Any],
        now: datetime,
    ) -> list[Mutation]:
        by_id = {item.id: item for item in accounts}
        mutations: list[Mutation] = []
        for group_id, rows in rankings.items():
            for row in rows:
                account = by_id[row.account_id]
                if account.id in duplicates or not account.schedulable:
                    continue
                account_record = state["accounts"].setdefault(str(account.id), {})
                health_record = account_record.get("health", {})
                if bool(health_record.get("auto_disabled", False)) or bool(
                    health_record.get("probation_active", False)
                ) or isinstance(health_record.get("pending_schedulable"), bool):
                    continue
                groups = account_record.setdefault("groups", {})
                group_record = groups.setdefault(str(group_id), {})
                current = _update_state(group_record)
                if current.candidate_tier == row.target_tier:
                    current.candidate_count += 1
                else:
                    current.candidate_tier = row.target_tier
                    current.candidate_count = 1
                group_record.update(dataclass_dict(current))
                decision = evaluate_update(
                    account, row.target_tier, row.score_ms, current, now
                )
                if decision.allowed:
                    mutations.append(
                        Mutation(
                            account.id,
                            "schedule",
                            {
                                "priority": decision.priority,
                                "load_factor": decision.load_factor,
                            },
                            decision.reason,
                        )
                    )
        return mutations

    @staticmethod
    def _record_health_schedule_mutations(
        state: dict[str, Any],
        results: list[MutationResult],
        accounts: list[Account],
        now: datetime,
    ) -> None:
        by_id = {item.id: item for item in accounts}
        for result in results:
            if result.status not in {"applied", "unknown"} or result.mutation.kind != "schedule":
                continue
            account = by_id.get(result.mutation.account_id)
            if account is None:
                continue
            groups = (
                state["accounts"]
                .setdefault(str(account.id), {})
                .setdefault("groups", {})
            )
            for group_id in account.group_ids:
                group = groups.setdefault(str(group_id), {})
                group["candidate_tier"] = None
                group["candidate_count"] = 0
                group["last_updated_at"] = now.isoformat()
                if result.status == "unknown":
                    group["pending_schedule"] = {
                        "values": dict(result.mutation.values),
                        "attempted_at": now.isoformat(),
                    }
                else:
                    group.pop("pending_schedule", None)

    @staticmethod
    def _record_health_mutations(
        state: dict[str, Any], results: list[MutationResult], now: datetime
    ) -> None:
        for result in results:
            if result.mutation.kind != "schedulable":
                continue
            health = (
                state["accounts"]
                .setdefault(str(result.mutation.account_id), {})
                .setdefault("health", {})
            )
            if result.status != "applied":
                continue
            health.pop("pending_schedulable", None)
            health.pop("ownership_uncertain", None)
            schedulable = bool(result.mutation.values.get("schedulable"))
            health["auto_disabled"] = not schedulable
            health["last_state_change_at"] = now.isoformat()
            if schedulable:
                health["recovery_successes"] = 0
                health["probation_active"] = True
                health["probation_successes"] = 0
            else:
                health["probation_active"] = False
                health["probation_successes"] = 0

    @staticmethod
    def _record_ranking_mutations(
        state: dict[str, Any],
        results: list[MutationResult],
        rankings: dict[int, list[Any]],
        now: datetime,
    ) -> None:
        score_lookup = {
            row.account_id: row.score_ms for rows in rankings.values() for row in rows
        }
        for result in results:
            if result.status not in {"applied", "unknown"} or result.mutation.kind != "schedule":
                continue
            account_record = state["accounts"].setdefault(
                str(result.mutation.account_id), {}
            )
            for group_record in account_record.setdefault("groups", {}).values():
                group_record["last_updated_at"] = now.isoformat()
                group_record["last_score_ms"] = score_lookup.get(
                    result.mutation.account_id
                )
                if result.status == "unknown":
                    group_record["pending_schedule"] = {
                        "values": dict(result.mutation.values),
                        "attempted_at": now.isoformat(),
                    }
                else:
                    group_record.pop("pending_schedule", None)

    @staticmethod
    def _unknown_mutation_result(
        error: MutationOutcomeUnknownError,
    ) -> MutationResult:
        return MutationResult(
            error.mutation,
            "unknown",
            "round timeout left the admin API mutation outcome unknown",
        )

    def _proposals(
        self,
        accounts: list[Account],
        rankings: dict[int, list[Any]],
        duplicates: set[int],
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        by_id = {item.id: item for item in accounts}
        proposals: list[dict[str, Any]] = []
        for group_id, rows in rankings.items():
            for row in rows:
                item = by_id[row.account_id]
                current_tier = min(4, max(1, item.priority))
                next_tier = current_tier
                if row.target_tier < current_tier:
                    next_tier -= 1
                elif row.target_tier > current_tier:
                    next_tier += 1
                reason = "eventual target after two stable hourly rankings"
                eligible = True
                if item.id in duplicates:
                    eligible = False
                    reason = "skipped because account belongs to multiple target groups"
                elif not item.schedulable:
                    eligible = False
                    reason = "preserved as manually disabled; optimizer has no ownership marker"
                elif dry_run:
                    reason = "preview only; dry-run does not advance confirmation state"
                proposals.append(
                    {
                        "group_id": group_id,
                        "account_id": item.id,
                        "account_name": item.name,
                        "eligible_after_confirmation": eligible,
                        "current": {
                            "priority": item.priority,
                            "load_factor": item.load_factor,
                            "effective_load_factor": item.effective_load_factor,
                            "concurrency": item.concurrency,
                            "schedulable": item.schedulable,
                        },
                        "next_one_band_step": {
                            "priority": next_tier,
                            "load_factor": _load_target(next_tier, item.concurrency),
                        },
                        "eventual_target": {
                            "priority": row.target_priority,
                            "load_factor": row.target_load_factor,
                        },
                        "would_eventually_change": eligible
                        and (
                            item.priority != row.target_priority
                            or item.effective_load_factor != row.target_load_factor
                        ),
                        "reason": reason,
                    }
                )
        return proposals

    def _probe_plan(
        self,
        snapshot: DatabaseSnapshot,
        grouped: dict[int, list[Account]],
        duplicates: set[int],
        state: dict[str, Any],
        now: datetime,
        dry_run: bool,
        model_pricing: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        real_by_account: dict[int, list[Sample]] = defaultdict(list)
        for sample in snapshot.samples:
            if sample.source == "real":
                real_by_account[sample.account_id].append(sample)

        plan: list[dict[str, Any]] = []
        theoretical_requests = 0
        seen: set[tuple[int, int]] = set()
        for group_id, accounts in grouped.items():
            for account in accounts:
                if (group_id, account.id) in seen:
                    continue
                seen.add((group_id, account.id))
                if account.id in duplicates:
                    plan.append(
                        {
                            "group_id": group_id,
                            "account_id": account.id,
                            "account_name": account.name,
                            "due_now": False,
                            "reason": "blocked because account belongs to multiple target groups",
                            "valid_real_60m": 0,
                            "valid_real_24h": 0,
                            "would_run_in_this_dry_run": False,
                        }
                    )
                    continue
                record = state["accounts"].get(str(account.id), {})
                health = _health_state(
                    record.get("health", {}), real_by_account[account.id], now
                )
                due, reason = should_probe(account, health, now)
                daily = self._daily_probe_rate(
                    reason, self.config.loop_interval_seconds
                )
                if reason.startswith("at least five valid real traffic"):
                    daily = 0
                theoretical_requests += daily
                plan.append(
                    {
                        "group_id": group_id,
                        "account_id": account.id,
                        "account_name": account.name,
                        "due_now": due,
                        "reason": reason,
                        "valid_real_60m": health.valid_real_60m,
                        "valid_real_24h": health.valid_real_24h,
                        "would_run_in_this_dry_run": False,
                    }
                )
        catalog_selection = select_probe_model(
            set(self.config.probe_model_preference),
            self.config.probe_model_preference,
            model_pricing,
            input_tokens=self.config.probe_input_tokens_estimate,
            output_tokens=self.config.probe_output_tokens_estimate,
        )
        probes_enabled = (
            not dry_run
            and self.config.apply_enabled
            and self.config.active_probes_enabled
            and self.config.targeted_test_safe
        )
        current_requests = theoretical_requests if probes_enabled else 0
        catalog_daily_cost = (
            theoretical_requests * catalog_selection.estimated_cost_usd
            if catalog_selection.estimated_cost_usd is not None
            else None
        )
        current_cost = (
            current_requests * catalog_selection.estimated_cost_usd
            if catalog_selection.estimated_cost_usd is not None
            else None
        )
        return plan, {
            "current_phase_requests": current_requests,
            "current_phase_input_tokens": 0
            if current_requests == 0
            else current_requests * self.config.probe_input_tokens_estimate,
            "current_phase_output_tokens_upper_bound": current_requests
            * self.config.probe_output_tokens_estimate,
            "current_phase_estimated_cost_usd": 0
            if current_requests == 0
            else current_cost,
            "adaptive_upper_bound_requests_if_enabled": theoretical_requests,
            "adaptive_upper_bound_input_tokens_if_enabled": theoretical_requests
            * self.config.probe_input_tokens_estimate,
            "catalog_cheapest_allowed_model": catalog_selection.model,
            "catalog_estimated_cost_per_request_usd": catalog_selection.estimated_cost_usd,
            "adaptive_catalog_cost_estimate_usd_if_enabled": catalog_daily_cost,
            "assumption": "pre-request catalog estimates use the cheapest common requested model ID; account mappings can only be confirmed by the optimizer handshake, and completed probes report a corrected actual-model estimate",
        }

    @staticmethod
    def _daily_probe_rate(reason: str, loop_interval_seconds: int) -> int:
        if "credential" in reason:
            policy_rate = 288
        elif "failed" in reason:
            policy_rate = 96
        elif "recovery" in reason or "new or low" in reason:
            policy_rate = 48
        elif (
            "stable" in reason
            or "manually disabled" in reason
            or "configuration" in reason
        ):
            policy_rate = 12
        else:
            policy_rate = 24
        interval = max(1, loop_interval_seconds)
        loop_rate = max(1, (86_400 + interval - 1) // interval)
        return min(policy_rate, loop_rate)

    def _fixed_warnings(self, snapshot: DatabaseSnapshot, dry_run: bool) -> list[str]:
        warnings = [
            "usage_logs has no persisted tool-call marker; historical tool-call exclusion cannot be exact",
            "targeted optimizer probes use the patched side-effect-safe minimal-token account test mode",
            "scheduler weights are deployment configuration and require a Sub2API restart to change or roll back",
            "429 responses continue through Sub2API's internal temporary limiter and are also tracked locally",
            "load_factor targets 15 and 12 are capped at each account concurrency as required",
        ]
        if dry_run:
            warnings.append("dry-run executed no paid probes and no admin API mutations")
        if str(
            snapshot.settings.get("openai_advanced_scheduler_enabled", "false")
        ).lower() != "true":
            warnings.append(
                "formal apply is blocked until openai_advanced_scheduler_enabled=true"
            )
        if snapshot.enabled_scheduled_test_plans:
            warnings.append(
                "existing enabled scheduled test plans may overlap with optimizer probes"
            )
        return warnings


def scheduling_rollback_mutations(baseline: dict[str, Any]) -> list[Mutation]:
    mutations: list[Mutation] = []
    for item in baseline.get("accounts", []):
        load_factor = item.get("load_factor")
        mutations.append(
            Mutation(
                int(item["id"]),
                "schedule",
                {
                    "priority": int(item["priority"]),
                    # Sub2API's official update API defines 0 as "clear to NULL".
                    "load_factor": int(load_factor) if load_factor is not None else 0,
                },
                "restore deployment baseline",
            )
        )
        mutations.append(
            Mutation(
                int(item["id"]),
                "schedulable",
                {"schedulable": bool(item["schedulable"])},
                "restore deployment baseline",
            )
        )
    return mutations
