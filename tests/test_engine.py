from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from optimizer.api import TargetedProbeSafetyError
from optimizer.config import Config
from optimizer.database import DatabaseSnapshot
from optimizer.domain import Account, ProbeOutcome, RoundTimeoutError, Sample
from optimizer.engine import OptimizerEngine, scheduling_rollback_mutations
from optimizer.storage import JsonStore

NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)


class FakeDatabase:
    def __init__(self, snapshot: DatabaseSnapshot) -> None:
        self.value = snapshot
        self.read_only_arguments: list[bool] = []

    def snapshot(self, *, read_only: bool = True) -> DatabaseSnapshot:
        self.read_only_arguments.append(read_only)
        return self.value


class ForbiddenAPI:
    def __getattr__(self, name: str):
        raise AssertionError(f"dry-run must not access admin API method {name}")


class FailedDisableAPI:
    def available_models(self, account_id: int) -> set[str]:
        return {"gpt"}

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        return ProbeOutcome(False, "server", 500, duration_ms=10)

    def apply(self, mutation: object) -> None:
        raise TimeoutError("write outcome is unknown")


class SuccessfulRecoveryAPI:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def available_models(self, account_id: int) -> set[str]:
        return {"gpt"}

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        return ProbeOutcome(
            True,
            status_code=200,
            ttft_ms=10,
            duration_ms=10,
            model=model,
            capability_verified=True,
        )

    def apply(self, mutation: object) -> None:
        self.applied.append(mutation)


class SucceedThenRoundTimeoutAPI:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def apply(self, mutation: object) -> None:
        self.applied.append(mutation)
        if len(self.applied) == 2:
            raise RoundTimeoutError("round deadline")


class FailedCapabilityHandshakeAPI:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def available_models(self, account_id: int) -> set[str]:
        return {"gpt"}

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        raise TargetedProbeSafetyError("optimizer mode was not confirmed")

    def apply(self, mutation: object) -> None:
        self.applied.append(mutation)


class VerifiedMappedProbeAPI:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def available_models(self, account_id: int) -> set[str]:
        return {"gpt-alias"}

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        return ProbeOutcome(
            True,
            status_code=200,
            ttft_ms=25,
            duration_ms=25,
            model="gpt-upstream",
            capability_verified=True,
        )

    def apply(self, mutation: object) -> None:
        self.applied.append(mutation)


class NoProbeAPI:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def available_models(self, account_id: int) -> set[str]:
        return {"gpt"}

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        raise AssertionError("real traffic should skip the active probe")

    def apply(self, mutation: object) -> None:
        self.applied.append(mutation)


class EngineTests(unittest.TestCase):
    def test_verified_probe_uses_actual_model_and_persists_capability(self) -> None:
        item = Account(1, "one", (7,), "openai", 10, 2, None, True, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )
        api = VerifiedMappedProbeAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pricing_file = root / "model_pricing.json"
            pricing_file.write_text(
                '{"gpt-alias":{"input_cost_per_token":0.000001,'
                '"output_cost_per_token":0.000001},'
                '"gpt-upstream":{"input_cost_per_token":0.000002,'
                '"output_cost_per_token":0.000003}}',
                encoding="utf-8",
            )
            config = Config(
                root=root,
                group_ids=(7,),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt-alias",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
                model_pricing_file=pricing_file,
            )
            store = JsonStore(root)
            report = OptimizerEngine(
                config, database, api, store, now=lambda: NOW
            ).run(dry_run=False)
            state = store.load_state()

        self.assertTrue(
            report["safeguards"]["targeted_probe_capability_verified"]
        )
        self.assertEqual(
            "gpt-upstream",
            report["safeguards"]["targeted_probe_verification"]["actual_model"],
        )
        self.assertEqual("gpt-alias", report["probe_results"][0]["requested_model"])
        self.assertEqual("gpt-upstream", report["probe_results"][0]["model"])
        self.assertEqual(
            0.000017,
            report["probe_results"][0][
                "requested_model_estimated_catalog_cost_usd"
            ],
        )
        self.assertEqual(
            0.000035,
            report["probe_results"][0]["estimated_catalog_cost_usd"],
        )
        self.assertTrue(report["probe_results"][0]["cost_estimate_exact"])
        self.assertIn("gpt-upstream", report["rankings"]["7"][0]["model_metrics"])
        self.assertTrue(state["targeted_probe_capability"]["verified"])

    def test_no_probe_does_not_claim_capability_but_uses_real_traffic_ranking(
        self,
    ) -> None:
        item = Account(1, "one", (7,), "openai", 10, 4, None, True, "active")
        samples = [
            Sample(1, 7, "gpt", "real", NOW - timedelta(minutes=5), True, ttft)
            for ttft in (100, 110, 120, 130, 140)
        ]
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                samples,
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )
        api = NoProbeAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pricing_file = root / "model_pricing.json"
            pricing_file.write_text(
                '{"gpt":{"input_cost_per_token":0.000001,'
                '"output_cost_per_token":0.000001}}',
                encoding="utf-8",
            )
            config = Config(
                root=root,
                group_ids=(7,),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
                model_pricing_file=pricing_file,
            )
            store = JsonStore(root)
            report = OptimizerEngine(
                config, database, api, store, now=lambda: NOW
            ).run(dry_run=False)
            state = store.load_state()

        self.assertFalse(
            report["safeguards"]["targeted_probe_capability_verified"]
        )
        self.assertTrue(report["safeguards"]["zero_api_mutations"])
        self.assertEqual([], api.applied)
        self.assertEqual(NOW.isoformat(), state["last_ranking_at"])

    def test_apply_is_blocked_when_advanced_scheduler_is_disabled(self) -> None:
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [],
                [],
                {"openai_advanced_scheduler_enabled": "false"},
                {},
                0,
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                root=Path(temp_dir),
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
            )
            with self.assertRaisesRegex(RuntimeError, "advanced scheduler"):
                OptimizerEngine(
                    config,
                    database,
                    ForbiddenAPI(),
                    JsonStore(Path(temp_dir)),
                    now=lambda: NOW,
                ).run(dry_run=False)

    def test_capability_handshake_failure_blocks_all_account_mutations(self) -> None:
        item = Account(1, "one", (7,), "openai", 10, 2, None, True, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )
        api = FailedCapabilityHandshakeAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pricing_file = root / "model_pricing.json"
            pricing_file.write_text(
                '{"gpt":{"input_cost_per_token":0.000001,'
                '"output_cost_per_token":0.000001}}',
                encoding="utf-8",
            )
            config = Config(
                root=root,
                group_ids=(7,),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
                model_pricing_file=pricing_file,
            )
            store = JsonStore(root)
            report = OptimizerEngine(
                config,
                database,
                api,
                store,
                now=lambda: NOW,
            ).run(dry_run=False)

            state = store.load_state()

        self.assertEqual([], api.applied)
        self.assertEqual([], report["mutations"])
        self.assertFalse(
            report["safeguards"]["targeted_probe_capability_verified"]
        )
        self.assertNotIn("last_ranking_at", state)
        self.assertTrue(
            any("capability handshake failed" in item for item in report["warnings"])
        )

    def test_daily_probe_estimate_is_capped_by_main_loop_frequency(self) -> None:
        reason = "credential failure needs confirmation"

        self.assertEqual(144, OptimizerEngine._daily_probe_rate(reason, 600))
        self.assertEqual(288, OptimizerEngine._daily_probe_rate(reason, 300))
        self.assertEqual(124, OptimizerEngine._daily_probe_rate(reason, 700))

    def test_manually_disabled_probe_estimate_is_low_frequency(self) -> None:
        self.assertEqual(
            12,
            OptimizerEngine._daily_probe_rate(
                "manually disabled account low-frequency health probe", 600
            ),
        )

    def test_rollback_encodes_null_load_factor_with_official_clear_value(self) -> None:
        mutations = scheduling_rollback_mutations(
            {
                "accounts": [
                    {
                        "id": 1,
                        "priority": 2,
                        "load_factor": None,
                        "schedulable": True,
                    }
                ]
            }
        )

        self.assertEqual(0, mutations[0].values["load_factor"])

    def test_dry_run_uses_read_only_database_and_does_not_advance_state(self) -> None:
        item = Account(1, "one", (7,), "openai", 10, 2, None, True, "active")
        samples = [
            Sample(1, 7, "gpt", "real", NOW, True, value)
            for value in (1_000, 1_100, 1_200)
        ]
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                samples,
                {"openai_advanced_scheduler_enabled": "false"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=False,
                active_probes_enabled=False,
                targeted_test_safe=False,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
            )
            store = JsonStore(root)
            before = store.load_state()
            report = OptimizerEngine(
                config,
                database,
                ForbiddenAPI(),
                store,
                now=lambda: NOW + timedelta(hours=2),
            ).run(dry_run=True)
            after = store.load_state()
            self.assertTrue((root / "backups" / "deployment-baseline.json").exists())
            self.assertFalse((root / "backups" / "activation-baseline.json").exists())

        self.assertEqual([True], database.read_only_arguments)
        self.assertEqual(before, after)
        self.assertEqual("dry-run", report["mode"])
        self.assertTrue(report["safeguards"]["zero_api_mutations"])
        self.assertTrue(report["safeguards"]["zero_scheduling_mutations"])
        self.assertEqual(
            0, report["safeguards"]["temporary_rate_limit_observations"]
        )
        self.assertIn(
            "formal apply is blocked",
            " ".join(report["warnings"]),
        )
        self.assertEqual(
            24,
            report["estimated_daily_probe_usage"][
                "adaptive_upper_bound_requests_if_enabled"
            ],
        )

    def test_failed_disable_write_does_not_claim_disable_ownership(self) -> None:
        item = Account(1, "one", (7,), "openai", 10, 2, None, True, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = JsonStore(root)
            pricing_file = root / "model_pricing.json"
            pricing_file.write_text(
                '{"gpt":{"input_cost_per_token":0.000001,'
                '"output_cost_per_token":0.000001}}',
                encoding="utf-8",
            )
            store.write_state(
                {
                    "version": 1,
                    "targeted_probe_capability": {
                        "verified": True,
                        "verified_at": NOW.isoformat(),
                        "account_id": 1,
                        "group_id": 7,
                        "requested_model": "gpt",
                        "actual_model": "gpt",
                    },
                    "accounts": {
                        "1": {
                            "health": {
                                "consecutive_failures": 2,
                                "first_failure_at": (NOW - timedelta(minutes=31)).isoformat(),
                                "last_failure_at": (NOW - timedelta(minutes=16)).isoformat(),
                                "last_probe_at": (NOW - timedelta(minutes=16)).isoformat(),
                                "last_probe_success": False,
                            }
                        }
                    },
                    "probe_history": [],
                }
            )
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
                model_pricing_file=pricing_file,
            )

            OptimizerEngine(
                config,
                database,
                FailedDisableAPI(),
                store,
                now=lambda: NOW,
            ).run(dry_run=False)
            health = store.load_state()["accounts"]["1"]["health"]
            self.assertFalse(health.get("auto_disabled", False))
            self.assertIs(health.get("pending_schedulable"), False)
            self.assertTrue((root / "backups" / "activation-baseline.json").exists())

            database.value = replace(
                database.value,
                accounts=[replace(item, schedulable=False)],
            )
            OptimizerEngine(
                replace(config, active_probes_enabled=False),
                database,
                ForbiddenAPI(),
                store,
                now=lambda: NOW + timedelta(minutes=1),
            ).run(dry_run=False)
            health = store.load_state()["accounts"]["1"]["health"]

        self.assertFalse(health.get("auto_disabled", False))
        self.assertTrue(health["ownership_uncertain"])
        self.assertNotIn("pending_schedulable", health)

    def test_multi_group_account_is_never_actively_probed(self) -> None:
        item = Account(1, "shared", (7, 59), "openai", 10, 2, None, True, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
            )
            report = OptimizerEngine(
                config,
                database,
                ForbiddenAPI(),
                JsonStore(root),
                now=lambda: NOW,
            ).run(dry_run=False)

        self.assertFalse(report["safeguards"]["active_probes_executed"])
        self.assertTrue(
            any("multiple target groups" in warning for warning in report["warnings"])
        )

    def test_recovery_downshift_starts_six_hour_schedule_cooldown(self) -> None:
        item = Account(1, "one", (7,), "openai", 10, 1, None, False, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [item],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pricing_file = root / "model_pricing.json"
            pricing_file.write_text(
                '{"gpt":{"input_cost_per_token":0.000001,'
                '"output_cost_per_token":0.000001}}',
                encoding="utf-8",
            )
            store = JsonStore(root)
            store.write_state(
                {
                    "version": 1,
                    "accounts": {
                        "1": {
                            "health": {
                                "auto_disabled": True,
                                "recovery_successes": 1,
                                "last_probe_at": (NOW - timedelta(minutes=31)).isoformat(),
                                "last_probe_success": True,
                            },
                            "groups": {
                                "7": {
                                    "candidate_tier": 1,
                                    "candidate_count": 2,
                                }
                            },
                        }
                    },
                    "probe_history": [],
                }
            )
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=True,
                targeted_test_safe=True,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
                model_pricing_file=pricing_file,
            )
            api = SuccessfulRecoveryAPI()

            OptimizerEngine(
                config,
                database,
                api,
                store,
                now=lambda: NOW,
            ).run(dry_run=False)
            group_state = store.load_state()["accounts"]["1"]["groups"]["7"]

        self.assertEqual(NOW.isoformat(), group_state["last_updated_at"])
        self.assertIsNone(group_state["candidate_tier"])
        self.assertEqual(0, group_state["candidate_count"])

    def test_activation_baseline_adds_new_accounts_without_rewriting_old_ones(self) -> None:
        first = Account(1, "one", (7,), "openai", 10, 2, None, True, "active")
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [first],
                [],
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=False,
                targeted_test_safe=False,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
            )
            store = JsonStore(root)
            engine = OptimizerEngine(
                config,
                database,
                ForbiddenAPI(),
                store,
                now=lambda: NOW,
            )
            engine.run(dry_run=False)

            second = Account(2, "two", (59,), "openai", 10, 1, None, True, "active")
            database.value = replace(
                database.value,
                accounts=[replace(first, priority=4), second],
            )
            OptimizerEngine(
                config,
                database,
                ForbiddenAPI(),
                store,
                now=lambda: NOW + timedelta(minutes=30),
            ).run(dry_run=False)
            baseline = json.loads(
                (root / "backups" / "activation-baseline.json").read_text("utf-8")
            )

        by_id = {item["id"]: item for item in baseline["accounts"]}
        self.assertEqual(2, by_id[1]["priority"])
        self.assertEqual(1, by_id[2]["priority"])

    def test_successful_ranking_write_keeps_cooldown_when_later_write_times_out(
        self,
    ) -> None:
        first = Account(1, "fast", (7,), "openai", 10, 4, None, True, "active")
        second = Account(2, "slow", (7,), "openai", 10, 1, None, True, "active")
        samples = [
            Sample(account_id, 7, "gpt", "real", NOW, True, ttft)
            for account_id, ttft in ((1, 100), (1, 110), (1, 120), (2, 900), (2, 950), (2, 1_000))
        ]
        database = FakeDatabase(
            DatabaseSnapshot(
                NOW,
                [first, second],
                samples,
                {"openai_advanced_scheduler_enabled": "true"},
                {},
                0,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = JsonStore(root)
            store.write_state(
                {
                    "version": 1,
                    "targeted_probe_capability": {
                        "verified": True,
                        "verified_at": NOW.isoformat(),
                        "account_id": 1,
                        "group_id": 7,
                        "requested_model": "gpt",
                        "actual_model": "gpt",
                    },
                    "accounts": {
                        "1": {"groups": {"7": {"candidate_tier": 1, "candidate_count": 1}}},
                        "2": {"groups": {"7": {"candidate_tier": 4, "candidate_count": 1}}},
                    },
                    "probe_history": [],
                }
            )
            config = Config(
                root=root,
                group_ids=(7, 59),
                database_host="postgres",
                database_port=5432,
                database_name="sub2api",
                database_user="sub2api",
                database_password="secret",
                api_base_url="http://sub2api:8080",
                apply_enabled=True,
                active_probes_enabled=False,
                targeted_test_safe=False,
                loop_interval_seconds=600,
                round_timeout_seconds=480,
                probe_model_preference=("gpt",),
                admin_email="",
                admin_password="",
                admin_api_key_file=None,
            )
            api = SucceedThenRoundTimeoutAPI()

            with self.assertRaises(RoundTimeoutError):
                OptimizerEngine(
                    config,
                    database,
                    api,
                    store,
                    now=lambda: NOW,
                ).run(dry_run=False)
            state = store.load_state()

            pending_values = api.applied[1].values
            database.value = replace(
                database.value,
                accounts=[
                    first,
                    replace(
                        second,
                        priority=int(pending_values["priority"]),
                        load_factor=int(pending_values["load_factor"]),
                    ),
                ],
            )
            OptimizerEngine(
                config,
                database,
                ForbiddenAPI(),
                store,
                now=lambda: NOW + timedelta(minutes=1),
            ).run(dry_run=False)
            reconciled_state = store.load_state()

        self.assertEqual(2, len(api.applied))
        self.assertEqual(
            NOW.isoformat(), state["accounts"]["1"]["groups"]["7"]["last_updated_at"]
        )
        second_group = state["accounts"]["2"]["groups"]["7"]
        self.assertEqual(NOW.isoformat(), second_group["last_updated_at"])
        self.assertEqual(
            api.applied[1].values, second_group["pending_schedule"]["values"]
        )
        self.assertEqual(NOW.isoformat(), state["last_ranking_at"])
        reconciled_group = reconciled_state["accounts"]["2"]["groups"]["7"]
        self.assertNotIn("pending_schedule", reconciled_group)
        self.assertEqual("confirmed_applied", reconciled_group["last_schedule_outcome"])
        self.assertEqual(NOW.isoformat(), reconciled_group["last_updated_at"])


if __name__ == "__main__":
    unittest.main()
