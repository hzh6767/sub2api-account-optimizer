from __future__ import annotations

import json
import http.client
import io
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from optimizer.api import (
    APIError,
    AdminAPI,
    TargetedProbeSafetyError,
    UrllibTransport,
    _event_failure,
)
from optimizer.config import Config
from optimizer.database import REAL_FAILURE_SQL, REAL_SUCCESS_SQL
from optimizer.domain import RoundTimeoutError
from optimizer.mutations import Mutation
from optimizer.storage import JsonStore, sanitize


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None, bool]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        if stream:
            return iter(
                [
                    b'data: {"type":"test_start","model":"gpt-5.6-terra","requested_model":"gpt-5.6-terra","mode":"optimizer"}\n',
                    b'data: {"type":"content","text":"1"}\n',
                ]
            )
        return {"code": 0, "data": {}}


class BusinessErrorTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return {"code": 409, "message": "rejected"}


class RateLimitEventTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return iter(
            [
                b'data: {"type":"test_start","model":"gpt-5.6-terra","requested_model":"gpt-5.6-terra","mode":"optimizer"}\n',
                b'data: {"type":"error","error":"429 rate limit","reset_at":"2026-07-19T19:00:00Z"}\n',
            ]
        )


class MappedModelTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return iter(
            [
                b'data: {"type":"test_start","model":"gpt-5.6-upstream","requested_model":"gpt-5.6-terra","mode":"optimizer"}\n',
                b'data: {"type":"content","text":"1"}\n',
            ]
        )


class ProbeConfigurationEventTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return iter(
            [
                b'data: {"type":"error","code":"probe_configuration","error":"optimizer mode only supports ordinary text models"}\n',
            ]
        )


class ContentWithoutHandshakeTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return iter([b'data: {"type":"content","text":"1"}\n'])


class ErrorWithoutHandshakeTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return iter([b'data: {"type":"error","error":"API returned 503"}\n'])


class StreamTimeoutAfterHandshake:
    def __iter__(self):
        yield b'data: {"type":"test_start","model":"gpt-upstream","requested_model":"gpt-alias","mode":"optimizer"}\n'
        raise TimeoutError("timed out")

    def close(self) -> None:
        return None


class StreamTimeoutTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return StreamTimeoutAfterHandshake()


class IncompleteStreamAfterHandshake:
    def __iter__(self):
        yield b'data: {"type":"test_start","model":"gpt-upstream","requested_model":"gpt-alias","mode":"optimizer"}\n'
        raise http.client.IncompleteRead(b"")

    def close(self) -> None:
        return None


class IncompleteStreamTransport(FakeTransport):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        self.calls.append((method, path, payload, stream))
        return IncompleteStreamAfterHandshake()


class AdapterTests(unittest.TestCase):
    def test_default_probe_budget_matches_minimal_optimizer_payload(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = Config.from_env()

        self.assertEqual(16, config.probe_input_tokens_estimate)
        self.assertEqual(1, config.probe_output_tokens_estimate)

    def test_sse_error_text_is_classified_without_logging_the_body(self) -> None:
        self.assertEqual(("auth", 401), _event_failure("API returned 401"))
        self.assertEqual(("rate_limit", 429), _event_failure("API returned 429"))
        self.assertEqual(("timeout", 504), _event_failure("upstream 504"))
        self.assertEqual(("upstream", 400), _event_failure("upstream 400"))
        self.assertEqual(("upstream", 404), _event_failure("upstream 404"))
        self.assertEqual(("upstream", 422), _event_failure("upstream 422"))

    def test_real_sample_queries_exclude_non_text_traffic(self) -> None:
        normalized = " ".join(REAL_SUCCESS_SQL.lower().split())
        self.assertIn("first_token_ms is not null", normalized)
        self.assertIn("request_type", normalized)
        self.assertIn("image_count", normalized)
        self.assertIn("video_count", normalized)
        self.assertIn("billing_mode", normalized)
        failure_normalized = " ".join(REAL_FAILURE_SQL.lower().split())
        self.assertIn("error_owner", failure_normalized)
        self.assertIn("coalesce(e.request_type, 0) = 0", failure_normalized)

    def test_targeted_probe_is_hard_blocked_when_running_endpoint_is_not_safe(
        self,
    ) -> None:
        transport = FakeTransport()
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=False,
            transport=transport,
        )

        with self.assertRaises(TargetedProbeSafetyError):
            api.probe_account(42, "gpt-5.6-terra")
        self.assertEqual([], transport.calls)

    def test_targeted_probe_path_contains_exact_account_and_stops_at_content(
        self,
    ) -> None:
        transport = FakeTransport()
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=transport,
        )

        result = api.probe_account(42, "gpt-5.6-terra")

        self.assertTrue(result.success)
        self.assertIsNotNone(result.ttft_ms)
        self.assertGreaterEqual(result.ttft_ms, 0)
        self.assertEqual(200, result.status_code)
        self.assertEqual("gpt-5.6-terra", result.model)
        self.assertTrue(result.capability_verified)
        self.assertEqual(result.ttft_ms, result.duration_ms)
        self.assertEqual("/api/v1/admin/accounts/42/test", transport.calls[0][1])
        self.assertEqual("1", transport.calls[0][2]["prompt"])
        self.assertEqual("optimizer", transport.calls[0][2]["mode"])
        self.assertTrue(transport.calls[0][3])

    def test_targeted_probe_accepts_verified_account_model_mapping(self) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=MappedModelTransport(),
        )

        result = api.probe_account(42, "gpt-5.6-terra")

        self.assertTrue(result.success)
        self.assertEqual(200, result.status_code)
        self.assertEqual("gpt-5.6-upstream", result.model)
        self.assertTrue(result.capability_verified)

    def test_targeted_probe_classifies_configuration_error_without_status(self) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=ProbeConfigurationEventTransport(),
        )

        result = api.probe_account(42, "gpt-5.6-terra")

        self.assertFalse(result.success)
        self.assertEqual("probe_configuration", result.error_category)
        self.assertIsNone(result.status_code)
        self.assertFalse(result.capability_verified)

    def test_targeted_probe_rejects_content_without_verified_handshake(self) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=ContentWithoutHandshakeTransport(),
        )

        with self.assertRaises(TargetedProbeSafetyError):
            api.probe_account(42, "gpt-5.6-terra")

    def test_targeted_probe_rejects_upstream_error_without_verified_handshake(
        self,
    ) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=ErrorWithoutHandshakeTransport(),
        )

        with self.assertRaises(TargetedProbeSafetyError):
            api.probe_account(42, "gpt-5.6-terra")

    def test_targeted_probe_classifies_stream_timeout_after_verified_handshake(
        self,
    ) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=StreamTimeoutTransport(),
        )

        result = api.probe_account(42, "gpt-alias")

        self.assertFalse(result.success)
        self.assertEqual("timeout", result.error_category)
        self.assertEqual("gpt-upstream", result.model)
        self.assertTrue(result.capability_verified)

    def test_targeted_probe_classifies_incomplete_stream_after_verified_handshake(
        self,
    ) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=IncompleteStreamTransport(),
        )

        result = api.probe_account(42, "gpt-alias")

        self.assertFalse(result.success)
        self.assertEqual("network", result.error_category)
        self.assertEqual("gpt-upstream", result.model)
        self.assertTrue(result.capability_verified)

    def test_admin_mutation_rejects_a_business_error_envelope(self) -> None:
        transport = BusinessErrorTransport()
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=False,
            transport=transport,
        )

        with self.assertRaises(APIError):
            api.apply(Mutation(42, "schedule", {"priority": 2, "load_factor": 8}))

    def test_transport_does_not_convert_round_deadline_to_upstream_timeout(self) -> None:
        transport = UrllibTransport("http://sub2api:8080")

        with patch(
            "optimizer.api.urllib.request.urlopen",
            side_effect=RoundTimeoutError("round deadline"),
        ):
            with self.assertRaises(RoundTimeoutError):
                transport.request("GET", "/health", None, {})

    def test_transport_records_http_429_retry_after(self) -> None:
        transport = UrllibTransport("http://sub2api:8080")
        headers = Message()
        headers["Retry-After"] = "120"
        error = urllib.error.HTTPError(
            "http://sub2api:8080/test",
            429,
            "rate limited",
            headers,
            io.BytesIO(),
        )
        before = datetime.now(timezone.utc)

        with patch("optimizer.api.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(APIError) as raised:
                transport.request("GET", "/test", None, {})

        self.assertIsNotNone(raised.exception.reset_at)
        self.assertGreaterEqual(raised.exception.reset_at, before + timedelta(seconds=119))

    def test_transport_parses_duration_style_rate_limit_reset(self) -> None:
        transport = UrllibTransport("http://sub2api:8080")
        headers = Message()
        headers["x-ratelimit-reset"] = "1s"
        error = urllib.error.HTTPError(
            "http://sub2api:8080/test",
            429,
            "rate limited",
            headers,
            io.BytesIO(),
        )
        before = datetime.now(timezone.utc)

        with patch("optimizer.api.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(APIError) as raised:
                transport.request("GET", "/test", None, {})

        self.assertIsNotNone(raised.exception.reset_at)
        self.assertGreaterEqual(raised.exception.reset_at, before + timedelta(milliseconds=900))

    def test_sse_rate_limit_event_records_reset_time(self) -> None:
        api = AdminAPI(
            "http://sub2api:8080",
            auth_headers=lambda: {"x-api-key": "not-logged"},
            targeted_test_safe=True,
            transport=RateLimitEventTransport(),
        )

        result = api.probe_account(42, "gpt-5.6-terra")

        self.assertEqual("rate_limit", result.error_category)
        self.assertEqual(datetime(2026, 7, 19, 19, 0, tzinfo=timezone.utc), result.reset_at)

    def test_json_store_is_atomic_and_sanitizes_sensitive_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JsonStore(Path(temp_dir))
            store.write_latest(
                {
                    "generated_at": datetime.now(timezone.utc),
                    "password": "secret",
                    "nested": {"cookie": "secret", "account_id": 7},
                }
            )
            payload = json.loads(
                (Path(temp_dir) / "logs" / "latest.json").read_text("utf-8")
            )

        self.assertEqual("[REDACTED]", payload["password"])
        self.assertEqual("[REDACTED]", payload["nested"]["cookie"])
        self.assertEqual(7, payload["nested"]["account_id"])
        self.assertEqual(
            "[REDACTED]", sanitize({"access_token": "secret"})["access_token"]
        )


if __name__ == "__main__":
    unittest.main()
