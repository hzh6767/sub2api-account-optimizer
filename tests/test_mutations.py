from __future__ import annotations

import unittest

from optimizer.domain import RoundTimeoutError
from optimizer.mutations import Mutation, MutationExecutor


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[Mutation] = []

    def apply(self, mutation: Mutation) -> None:
        self.calls.append(mutation)


class FailingAPI:
    def apply(self, mutation: Mutation) -> None:
        raise RuntimeError("not persisted in logs")


class TimeoutFirstAccountAPI:
    def __init__(self) -> None:
        self.calls: list[Mutation] = []

    def apply(self, mutation: Mutation) -> None:
        self.calls.append(mutation)
        if mutation.account_id == 1:
            raise TimeoutError("unknown outcome")


class RoundTimeoutAPI:
    def apply(self, mutation: Mutation) -> None:
        raise RoundTimeoutError("round deadline")


class MutationTests(unittest.TestCase):
    def test_dry_run_performs_zero_api_mutations(self) -> None:
        api = FakeAPI()
        executor = MutationExecutor(api, apply_enabled=True)

        result = executor.execute(
            [
                Mutation(
                    account_id=1,
                    kind="schedule",
                    values={"priority": 1, "load_factor": 10},
                )
            ],
            dry_run=True,
        )

        self.assertEqual([], api.calls)
        self.assertEqual("dry-run", result[0].status)

    def test_global_apply_gate_blocks_mutations(self) -> None:
        api = FakeAPI()
        executor = MutationExecutor(api, apply_enabled=False)

        result = executor.execute(
            [Mutation(account_id=1, kind="schedulable", values={"schedulable": False})],
            dry_run=False,
        )

        self.assertEqual([], api.calls)
        self.assertEqual("blocked", result[0].status)

    def test_api_failure_is_audited_without_aborting_the_round(self) -> None:
        executor = MutationExecutor(FailingAPI(), apply_enabled=True)

        result = executor.execute(
            [Mutation(account_id=1, kind="schedulable", values={"schedulable": False})],
            dry_run=False,
        )

        self.assertEqual("failed", result[0].status)
        self.assertIn("RuntimeError", result[0].detail)
        self.assertNotIn("not persisted", result[0].detail)

    def test_unknown_account_mutation_blocks_its_dependent_mutations_only(self) -> None:
        api = TimeoutFirstAccountAPI()
        executor = MutationExecutor(api, apply_enabled=True)

        result = executor.execute(
            [
                Mutation(1, "schedule", {"priority": 4, "load_factor": 5}),
                Mutation(1, "schedulable", {"schedulable": True}),
                Mutation(2, "schedulable", {"schedulable": False}),
            ],
            dry_run=False,
        )

        self.assertEqual(["unknown", "blocked", "applied"], [item.status for item in result])
        self.assertEqual(["schedule", "schedulable"], [item.kind for item in api.calls])
        self.assertEqual([1, 2], [item.account_id for item in api.calls])

    def test_round_timeout_is_not_downgraded_to_a_mutation_failure(self) -> None:
        executor = MutationExecutor(RoundTimeoutAPI(), apply_enabled=True)

        with self.assertRaises(RoundTimeoutError):
            executor.execute(
                [Mutation(1, "schedulable", {"schedulable": False})],
                dry_run=False,
            )


if __name__ == "__main__":
    unittest.main()
