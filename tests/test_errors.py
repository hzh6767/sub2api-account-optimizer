from __future__ import annotations

import io
import json
import signal
import tempfile
import traceback
import unittest
from collections.abc import Callable
from contextlib import nullcontext, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from optimizer.api import APIError
from optimizer.cli import _daemon, _load_json
from optimizer.domain import RoundTimeoutError
from optimizer.engine import OptimizerEngine
from optimizer.errors import RedactedError, redacted_errors
from optimizer.health import _http_health, _redis_health
from optimizer.mutations import Mutation, MutationExecutor
from optimizer.storage import JsonStore


class UnexpectedError(Exception):
    pass


class RedactedErrorTests(unittest.TestCase):
    def test_exception_details_are_redacted(self) -> None:
        original = UnexpectedError("sensitive-value")
        try:
            with redacted_errors():
                raise original
        except RedactedError as error:
            self.assertEqual("UnexpectedError", error.error_type)
            self.assertEqual("UnexpectedError", str(error))
            self.assertFalse(error.outcome_unknown)
            self.assertNotIn(
                "sensitive-value", "".join(traceback.format_exception(error))
            )
        else:
            self.fail("unexpected errors must be translated")

    def test_timeout_classification_preserves_transport_and_api_timeouts(self) -> None:
        errors = (
            TimeoutError("sensitive-value"),
            APIError(None, "timeout", "sensitive-value"),
        )
        for error in errors:
            with (
                self.subTest(error_type=type(error).__name__),
                self.assertRaises(RedactedError) as raised,
                redacted_errors(),
            ):
                raise error
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertEqual(type(error).__name__, raised.exception.error_type)

    def test_existing_redacted_errors_are_not_wrapped_again(self) -> None:
        original = RedactedError("TransportFailure", outcome_unknown=True)
        with self.assertRaises(RedactedError) as raised, redacted_errors():
            raise original
        self.assertIs(original, raised.exception)

    def test_control_flow_exceptions_propagate_unchanged(self) -> None:
        errors = (RoundTimeoutError("deadline"), KeyboardInterrupt(), SystemExit(1))
        for error in errors:
            with (
                self.subTest(error_type=type(error).__name__),
                self.assertRaises(type(error)) as raised,
                redacted_errors(),
            ):
                raise error
            self.assertIs(error, raised.exception)


class ErrorBoundaryTests(unittest.TestCase):
    def test_http_health_redacts_unexpected_errors(self) -> None:
        with patch(
            "optimizer.health.urllib.request.urlopen",
            side_effect=UnexpectedError("sensitive-value"),
        ):
            result = _http_health("http://service.invalid/health")
        self.assertEqual({"healthy": False, "error_type": "UnexpectedError"}, result)

    def test_redis_health_redacts_unexpected_errors(self) -> None:
        with (
            patch.dict("os.environ", {"REDIS_PORT": "6379"}),
            patch(
                "optimizer.health.socket.create_connection",
                side_effect=UnexpectedError("sensitive-value"),
            ),
        ):
            result = _redis_health()
        self.assertEqual({"healthy": False, "error_type": "UnexpectedError"}, result)

    def test_health_checks_preserve_round_deadlines(self) -> None:
        original = RoundTimeoutError("deadline")
        with (
            patch("optimizer.health.urllib.request.urlopen", side_effect=original),
            self.assertRaises(RoundTimeoutError) as raised,
        ):
            _http_health("http://service.invalid/health")
        self.assertIs(original, raised.exception)
        with (
            patch.dict("os.environ", {"REDIS_PORT": "6379"}),
            patch("optimizer.health.socket.create_connection", side_effect=original),
            self.assertRaises(RoundTimeoutError) as raised,
        ):
            _redis_health()
        self.assertIs(original, raised.exception)

    def test_mutations_preserve_timeout_and_unexpected_error_results(self) -> None:
        errors = (
            (UnexpectedError("sensitive-value"), "failed"),
            (APIError(None, "timeout", "sensitive-value"), "unknown"),
        )
        for error, status in errors:
            with self.subTest(error_type=type(error).__name__):
                api = Mock()
                api.apply.side_effect = [error, None]
                results = MutationExecutor(api, apply_enabled=True).execute(
                    [
                        Mutation(1, "schedule", {"priority": 4}),
                        Mutation(1, "schedulable", {"schedulable": True}),
                        Mutation(2, "schedulable", {"schedulable": False}),
                    ],
                    dry_run=False,
                )
                self.assertEqual(
                    [status, "blocked", "applied"], [item.status for item in results]
                )
                self.assertEqual(2, api.apply.call_count)
                self.assertNotIn("sensitive-value", repr(results))

    def test_daemon_reports_safe_errors_and_keeps_shutdown_behavior(self) -> None:
        errors = (UnexpectedError("sensitive-value"), RoundTimeoutError("deadline"))
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                callbacks: dict[int, Callable[[int, object], None]] = {}

                def register_signal(
                    signum: int,
                    handler: Callable[[int, object], None],
                    signal_handlers: dict[int, Callable[[int, object], None]] = callbacks,
                ) -> None:
                    signal_handlers[signum] = handler

                def fail_run(
                    *,
                    dry_run: bool,
                    failure: Exception = error,
                    signal_handlers: dict[int, Callable[[int, object], None]] = callbacks,
                ) -> None:
                    self.assertFalse(dry_run)
                    signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
                    raise failure

                config = Mock(
                    apply_enabled=True,
                    round_timeout_seconds=1,
                    loop_interval_seconds=1,
                )
                engine = Mock()
                engine.run.side_effect = fail_run
                stderr = io.StringIO()
                with (
                    patch("optimizer.cli.signal.signal", side_effect=register_signal),
                    patch("optimizer.cli.round_timeout", return_value=nullcontext()),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(0, _daemon(config, engine))
                self.assertEqual(
                    {"status": "error", "error_type": type(error).__name__},
                    json.loads(stderr.getvalue()),
                )


class JSONValidationTests(unittest.TestCase):
    def test_cli_rejects_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(TypeError):
                _load_json(path)

    def test_store_rejects_non_object_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JsonStore(Path(temp_dir))
            store.write_backup("invalid.json", [])
            with self.assertRaises(TypeError):
                store.load_backup("invalid.json")

    def test_activation_baseline_rejects_non_list_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JsonStore(Path(temp_dir))
            store.write_backup("activation-baseline.json", {"accounts": {}})
            engine = OptimizerEngine(Mock(), Mock(), Mock(), store)
            with self.assertRaises(TypeError):
                engine._extend_activation_baseline(
                    {"accounts": []}, datetime.now(timezone.utc)
                )
            self.assertEqual(
                {"accounts": {}}, store.load_backup("activation-baseline.json")
            )


if __name__ == "__main__":
    unittest.main()
