from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from optimizer.domain import Account, HealthState, ProbeOutcome, Sample, UpdateState
from optimizer.policy import (
    apply_probe_outcome,
    build_rankings,
    evaluate_update,
    partition_accounts,
    should_probe,
)


NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)


def account(
    account_id: int,
    groups: tuple[int, ...],
    *,
    concurrency: int = 10,
    priority: int = 2,
    load_factor: int | None = None,
    schedulable: bool = True,
    platform: str = "openai",
    deleted: bool = False,
) -> Account:
    return Account(
        id=account_id,
        name=f"account-{account_id}",
        group_ids=groups,
        platform=platform,
        concurrency=concurrency,
        priority=priority,
        load_factor=load_factor,
        schedulable=schedulable,
        status="active",
        deleted=deleted,
    )


def successes(
    account_id: int, group_id: int, model: str, values: list[int]
) -> list[Sample]:
    return [
        Sample(
            account_id=account_id,
            group_id=group_id,
            model=model,
            source="real",
            occurred_at=NOW - timedelta(minutes=index),
            success=True,
            ttft_ms=value,
        )
        for index, value in enumerate(values)
    ]


class RankingPolicyTests(unittest.TestCase):
    def test_plus_pro_isolation_and_duplicate_warning(self) -> None:
        accounts = [
            account(1, (7,)),
            account(2, (59,)),
            account(3, (7, 59)),
            account(4, (7,), platform="grok"),
            account(5, (59,), deleted=True),
        ]

        grouped, warnings = partition_accounts(accounts, (7, 59))

        self.assertEqual([1, 3], [item.id for item in grouped[7]])
        self.assertEqual([2, 3], [item.id for item in grouped[59]])
        self.assertEqual(1, len(warnings))
        self.assertIn("account 3", warnings[0])

    def test_model_scores_are_normalized_before_composite(self) -> None:
        accounts = [account(1, (7,)), account(2, (7,))]
        samples = successes(1, 7, "fast-model", [100, 100, 100])
        samples += successes(2, 7, "slow-model", [10_000, 10_000, 10_000])

        ranking = build_rankings(accounts, samples, (7,))[7]
        rows = {row.account_id: row for row in ranking}

        self.assertAlmostEqual(rows[1].score_ms or 0, rows[2].score_ms or 0, places=6)
        self.assertEqual({"fast-model"}, set(rows[1].model_metrics))
        self.assertEqual({"slow-model"}, set(rows[2].model_metrics))

    def test_insufficient_samples_cannot_rank_first_and_use_tier_three(self) -> None:
        accounts = [account(1, (7,)), account(2, (7,))]
        samples = successes(1, 7, "gpt", [100])
        samples += successes(2, 7, "gpt", [1_000, 1_100, 1_200])

        ranking = build_rankings(accounts, samples, (7,))[7]

        self.assertEqual(2, ranking[0].account_id)
        row = next(item for item in ranking if item.account_id == 1)
        self.assertTrue(row.insufficient_samples)
        self.assertEqual(3, row.target_tier)
        self.assertEqual(8, row.target_load_factor)

    def test_quartiles_and_load_factor_are_capped_by_concurrency(self) -> None:
        accounts = [account(i, (7,), concurrency=10) for i in range(1, 5)]
        samples: list[Sample] = []
        for index, item in enumerate(accounts, start=1):
            samples += successes(item.id, 7, "gpt", [index * 1_000] * 3)

        ranking = build_rankings(accounts, samples, (7,))[7]

        self.assertEqual([1, 2, 3, 4], [row.target_tier for row in ranking])
        self.assertEqual([10, 10, 8, 5], [row.target_load_factor for row in ranking])

    def test_manually_disabled_account_does_not_shift_active_quartiles(self) -> None:
        accounts = [
            account(1, (59,), schedulable=False),
            account(2, (59,)),
            account(3, (59,)),
            account(4, (59,)),
        ]
        samples: list[Sample] = []
        for item in accounts:
            samples += successes(item.id, 59, "gpt", [item.id * 1_000] * 3)

        ranking = build_rankings(accounts, samples, (59,))[59]

        self.assertEqual([2, 3, 4, 1], [row.account_id for row in ranking])
        self.assertEqual([1, 3, 4], [row.target_tier for row in ranking[:3]])
        self.assertFalse(ranking[-1].ranking_eligible)
        self.assertEqual(3, ranking[-1].target_tier)


class UpdatePolicyTests(unittest.TestCase):
    def test_update_moves_at_most_one_band_and_obeys_cooldown(self) -> None:
        item = account(1, (7,), priority=4, load_factor=5)
        stable = UpdateState(candidate_tier=1, candidate_count=2)

        decision = evaluate_update(item, 1, 1_000, stable, NOW)
        self.assertTrue(decision.allowed)
        self.assertEqual(3, decision.priority)
        self.assertEqual(8, decision.load_factor)

        cooling = UpdateState(
            candidate_tier=1,
            candidate_count=2,
            last_updated_at=NOW - timedelta(hours=1),
        )
        decision = evaluate_update(item, 1, 1_000, cooling, NOW)
        self.assertFalse(decision.allowed)
        self.assertIn("cooldown", decision.reason)

    def test_two_rankings_and_fifteen_percent_threshold_prevent_churn(self) -> None:
        item = account(1, (7,), priority=3, load_factor=8)

        one_round = UpdateState(candidate_tier=2, candidate_count=1)
        self.assertFalse(evaluate_update(item, 2, 700, one_round, NOW).allowed)

        small_change = UpdateState(
            candidate_tier=2,
            candidate_count=2,
            last_score_ms=1_000,
        )
        decision = evaluate_update(item, 2, 1_100, small_change, NOW)
        self.assertFalse(decision.allowed)
        self.assertIn("15%", decision.reason)


class HealthPolicyTests(unittest.TestCase):
    def test_one_failure_does_not_disable(self) -> None:
        state, decision = apply_probe_outcome(
            account(1, (7,)), HealthState(), ProbeOutcome(False, "timeout", 504), NOW
        )
        self.assertEqual(1, state.consecutive_failures)
        self.assertIsNone(decision.action)

    def test_three_failures_spanning_thirty_minutes_disable(self) -> None:
        state = HealthState()
        item = account(1, (7,))
        for offset in (0, 15, 31):
            state, decision = apply_probe_outcome(
                item,
                state,
                ProbeOutcome(False, "timeout", 504),
                NOW + timedelta(minutes=offset),
            )
        self.assertEqual("disable", decision.action)

    def test_auth_failure_requires_two_confirmations_five_minutes_apart(self) -> None:
        state, first = apply_probe_outcome(
            account(1, (7,)), HealthState(), ProbeOutcome(False, "auth", 401), NOW
        )
        self.assertIsNone(first.action)
        state, second = apply_probe_outcome(
            account(1, (7,)),
            state,
            ProbeOutcome(False, "auth", 403),
            NOW + timedelta(minutes=6),
        )
        self.assertEqual("disable", second.action)
        self.assertTrue(second.needs_credential_check)

    def test_429_never_permanently_disables(self) -> None:
        state = HealthState()
        for offset in (0, 20, 40, 60):
            state, decision = apply_probe_outcome(
                account(1, (7,)),
                state,
                ProbeOutcome(False, "rate_limit", 429),
                NOW + timedelta(minutes=offset),
            )
        self.assertIsNone(decision.action)
        self.assertEqual(0, state.consecutive_failures)

    def test_429_reset_suppresses_reprobe_until_the_reset_time(self) -> None:
        item = account(1, (7,))
        reset_at = NOW + timedelta(hours=1)
        state, decision = apply_probe_outcome(
            item,
            HealthState(),
            ProbeOutcome(False, "rate_limit", 429, reset_at=reset_at),
            NOW,
        )

        due, reason = should_probe(item, state, NOW + timedelta(minutes=30))
        self.assertIsNone(decision.action)
        self.assertFalse(due)
        self.assertIn("rate-limit reset", reason)
        self.assertTrue(should_probe(item, state, NOW + timedelta(minutes=61))[0])

    def test_429_breaks_a_sequence_of_credential_failures(self) -> None:
        item = account(1, (7,))
        state, _ = apply_probe_outcome(
            item, HealthState(), ProbeOutcome(False, "auth", 401), NOW
        )
        state, _ = apply_probe_outcome(
            item,
            state,
            ProbeOutcome(False, "rate_limit", 429),
            NOW + timedelta(minutes=6),
        )
        state, decision = apply_probe_outcome(
            item,
            state,
            ProbeOutcome(False, "auth", 403),
            NOW + timedelta(minutes=12),
        )

        self.assertEqual(1, state.credential_failures)
        self.assertIsNone(decision.action)

    def test_only_optimizer_disabled_account_recovers_after_two_successes(self) -> None:
        item = account(1, (7,), schedulable=False)
        state = HealthState(auto_disabled=True)
        state, first = apply_probe_outcome(item, state, ProbeOutcome(True), NOW)
        self.assertIsNone(first.action)
        state, second = apply_probe_outcome(
            item, state, ProbeOutcome(True), NOW + timedelta(minutes=30)
        )
        self.assertEqual("enable", second.action)

    def test_recovered_account_stays_in_probation_for_two_successes(self) -> None:
        item = account(1, (7,), schedulable=True)
        state = HealthState(probation_active=True)
        state, first = apply_probe_outcome(item, state, ProbeOutcome(True), NOW)
        self.assertTrue(state.probation_active)
        self.assertIsNone(first.action)
        state, second = apply_probe_outcome(
            item, state, ProbeOutcome(True), NOW + timedelta(minutes=30)
        )
        self.assertFalse(state.probation_active)
        self.assertEqual(2, state.probation_successes)
        self.assertIsNone(second.action)

    def test_success_clears_stale_disable_ownership_when_account_is_schedulable(
        self,
    ) -> None:
        item = account(1, (7,), schedulable=True)
        state = HealthState(auto_disabled=True)

        state, decision = apply_probe_outcome(item, state, ProbeOutcome(True), NOW)

        self.assertFalse(state.auto_disabled)
        self.assertIsNone(decision.action)

    def test_manually_disabled_account_never_recovers(self) -> None:
        item = account(1, (7,), schedulable=False)
        state = HealthState(auto_disabled=False)
        for offset in (0, 30, 60):
            state, decision = apply_probe_outcome(
                item, state, ProbeOutcome(True), NOW + timedelta(minutes=offset)
            )
        self.assertIsNone(decision.action)

    def test_manually_disabled_account_uses_low_frequency_probe_interval(self) -> None:
        item = account(1, (7,), schedulable=False)
        state = HealthState(
            credential_failures=2,
            last_probe_at=NOW,
            last_probe_success=False,
        )

        due, reason = should_probe(item, state, NOW + timedelta(minutes=30))
        self.assertFalse(due)
        self.assertIn("manually disabled", reason)
        self.assertTrue(should_probe(item, state, NOW + timedelta(hours=2))[0])

    def test_probe_configuration_error_never_disables_account(self) -> None:
        item = account(1, (7,))
        state = HealthState()
        for offset in (0, 30, 60, 90):
            state, decision = apply_probe_outcome(
                item,
                state,
                ProbeOutcome(False, "probe_configuration"),
                NOW + timedelta(minutes=offset),
            )

        self.assertIsNone(decision.action)
        self.assertEqual(0, state.consecutive_failures)
        self.assertIsNone(state.last_probe_success)
        due, reason = should_probe(item, state, NOW + timedelta(minutes=30))
        self.assertFalse(due)
        self.assertIn("configuration", reason)

    def test_real_traffic_skips_active_probe(self) -> None:
        due, reason = should_probe(
            account(1, (7,)),
            HealthState(valid_real_60m=5, valid_real_24h=20),
            NOW,
        )
        self.assertFalse(due)
        self.assertIn("real traffic", reason)


if __name__ == "__main__":
    unittest.main()
